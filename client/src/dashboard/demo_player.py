"""Demo playback engine with absolute-time scheduling.

Reads a recorded demo directory (session.json + frames/) and replays
messages through the DashboardBroadcaster at their original pace.
Supports phase-level and step-level skipping with timer resync.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from client.src.dashboard.broadcaster import DashboardBroadcaster

logger = logging.getLogger(__name__)

# Phases that can be skipped at the phase level
PHASE_SKIP_PHASES = {"search"}
# Phases that skip one step at a time
STEP_SKIP_PHASES = {"approach", "inspect"}

# Target playback FPS — thin recorded frames (~10 FPS) down to this rate
PLAYBACK_FPS = 5

# Message types that are "interesting" — triggers end of idle preamble scan
_INTERESTING_TYPES = {"transcript", "log", "ai_activity", "ai_result"}

# Message types broadcast during skip (everything except frame/telemetry)
_SKIP_BROADCAST_TYPES = {
    "status", "log", "transcript", "ai_activity", "ai_result",
    "perception", "report_data",
}


class DemoPlayer:
    """Plays back a recorded demo session through the broadcaster.

    Uses absolute-time scheduling: playback_start = time.monotonic().
    For each message, compute target_time = playback_start + message.t,
    then wait the delta. Supports skip, pause, resume, and stop.
    """

    def __init__(
        self,
        demo_dir: str | Path,
        broadcaster: DashboardBroadcaster,
    ) -> None:
        self._demo_dir = Path(demo_dir)
        self._frames_dir = self._demo_dir / "frames"
        self._broadcaster = broadcaster

        self._messages: list[dict] = []
        self._metadata: dict = {}
        self._playing = False
        self._paused = False
        self._skip_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused initially

        # Tracking for synthetic report
        self._last_frame_b64: str | None = None
        self._acquisition_frame: str | None = None
        self._last_inspection_result: dict | None = None
        self._current_phase: str | None = None

        # Frame thinning: track last broadcast time
        self._last_frame_time: float = 0.0
        self._frame_interval: float = 1.0 / PLAYBACK_FPS

    def _load(self) -> None:
        """Load the session.json file and parse messages."""
        session_path = self._demo_dir / "session.json"
        lines = session_path.read_text().strip().splitlines()
        if not lines:
            raise ValueError(f"Empty session.json in {self._demo_dir}")

        self._metadata = json.loads(lines[0])
        self._messages = []
        for line in lines[1:]:
            if line.strip():
                self._messages.append(json.loads(line))

        logger.info(
            "Loaded demo: %s — %d messages, %.1fs",
            self._metadata.get("target", "unknown"),
            len(self._messages),
            self._metadata.get("duration_sec", 0),
        )

    def _find_first_interesting_index(self) -> int:
        """Scan messages to find the first 'interesting' event.

        Returns the index of 2s before that event, or 0 if nothing found.
        """
        for i, msg in enumerate(self._messages):
            msg_type = msg.get("type", "")
            if msg_type in _INTERESTING_TYPES:
                # For log messages, skip IDLE-state logs
                if msg_type == "log":
                    data = msg.get("data", {})
                    text = data.get("message", "") if isinstance(data, dict) else str(data)
                    if "idle" in text.lower():
                        continue
                # Also treat non-IDLE status as interesting
                target_t = msg.get("t", 0) - 2.0
                if target_t <= 0:
                    return 0
                # Find the message index closest to target_t
                for j in range(i - 1, -1, -1):
                    if self._messages[j].get("t", 0) <= target_t:
                        return j
                return 0
            if msg_type == "status":
                data = msg.get("data", {})
                state = (data.get("state") or "").upper()
                if state not in ("IDLE", "READY", "CONNECTED", ""):
                    target_t = msg.get("t", 0) - 2.0
                    if target_t <= 0:
                        return 0
                    for j in range(i - 1, -1, -1):
                        if self._messages[j].get("t", 0) <= target_t:
                            return j
                    return 0
        return 0

    async def _auto_skip_preamble(self) -> tuple[int, float]:
        """Skip idle preamble, broadcasting only last telemetry + last frame.

        Returns (start_index, playback_start) for the main loop.
        """
        first_idx = self._find_first_interesting_index()
        if first_idx <= 1:
            return 0, time.monotonic()

        # Scan through preamble to find last telemetry and last frame
        last_telemetry: dict | None = None
        last_frame_msg: dict | None = None
        for i in range(first_idx):
            msg = self._messages[i]
            msg_type = msg.get("type", "")
            if msg_type == "telemetry":
                last_telemetry = msg
            elif msg_type == "frame":
                last_frame_msg = msg
            elif msg_type == "status":
                data = msg.get("data", {})
                phase = data.get("phase")
                if phase:
                    self._current_phase = phase

        # Broadcast last telemetry and frame to initialize UI
        if last_telemetry:
            await self._broadcast_message(last_telemetry)
        if last_frame_msg:
            self._last_frame_b64 = await self._load_frame_data(last_frame_msg)
            if self._last_frame_b64:
                await self._broadcaster.broadcast_frame(self._last_frame_b64)

        skipped_t = self._messages[first_idx].get("t", 0)
        playback_start = time.monotonic() - skipped_t
        logger.info(
            "Auto-skipped %.1fs of idle preamble (%d messages)",
            skipped_t, first_idx,
        )
        return first_idx, playback_start

    async def play(self) -> None:
        """Play the demo from start to finish."""
        self._load()

        if not self._messages:
            await self._broadcaster.broadcast_log(
                "WARNING", "Demo recording is empty"
            )
            return

        self._playing = True
        self._stop_event.clear()

        # Auto-skip idle preamble
        i, playback_start = await self._auto_skip_preamble()

        logger.info("Playback started: %s", self._metadata.get("target", ""))

        while i < len(self._messages) and not self._stop_event.is_set():
            msg = self._messages[i]
            t = msg.get("t", 0)
            msg_type = msg.get("type", "")

            # Wait until it's time to play this message
            target_time = playback_start + t
            now = time.monotonic()
            delay = target_time - now

            if delay > 0:
                try:
                    # Interruptible wait — stop, skip, or pause can break it
                    await asyncio.wait_for(
                        self._wait_interruptible(delay),
                        timeout=delay + 0.1,
                    )
                except asyncio.TimeoutError:
                    pass

            if self._stop_event.is_set():
                break

            # Handle pause
            await self._pause_event.wait()

            if self._stop_event.is_set():
                break

            # Handle skip — fast-forward inline, broadcasting important messages
            if self._skip_event.is_set():
                self._skip_event.clear()
                i, playback_start = await self._handle_skip(i, playback_start)
                continue

            # Track phase from status messages
            if msg_type == "status":
                data = msg.get("data", {})
                phase = data.get("phase")
                if phase:
                    self._current_phase = phase

            # Track frames for synthetic report
            if msg_type == "frame":
                self._last_frame_b64 = await self._load_frame_data(msg)
                if self._current_phase == "approach" and self._acquisition_frame is None:
                    self._acquisition_frame = self._last_frame_b64

                # Frame thinning: only broadcast at PLAYBACK_FPS
                now_real = time.monotonic()
                if now_real - self._last_frame_time < self._frame_interval:
                    i += 1
                    continue
                self._last_frame_time = now_real

            # Track AI results for report
            if msg_type == "ai_result":
                data = msg.get("data", {})
                if data.get("result_type") == "inspection":
                    self._last_inspection_result = data

            # Broadcast the message
            await self._broadcast_message(msg)
            i += 1

        # Playback complete — send synthetic report if applicable
        if not self._stop_event.is_set():
            await self._send_synthetic_report()
            await self._broadcaster.broadcast_status({
                "state": "COMPLETE",
                "phase": "complete",
                "target": self._metadata.get("target", ""),
            })

        self._playing = False
        logger.info("Playback finished")

    async def stop(self) -> None:
        """Stop playback."""
        self._stop_event.set()
        self._pause_event.set()  # Unpause to allow loop to exit
        self._skip_event.set()
        logger.info("Playback stopped by user")

    def skip(self) -> None:
        """Skip current phase or step."""
        self._skip_event.set()

    def toggle_pause(self) -> None:
        """Toggle pause/resume."""
        if self._paused:
            self._paused = False
            self._pause_event.set()
            logger.info("Playback resumed")
        else:
            self._paused = True
            self._pause_event.clear()
            logger.info("Playback paused")

    async def _wait_interruptible(self, delay: float) -> None:
        """Wait for delay seconds, interruptible by stop or skip."""
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(asyncio.sleep(delay)),
                asyncio.create_task(self._stop_event.wait()),
                asyncio.create_task(self._skip_event.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _handle_skip(
        self, current_idx: int, playback_start: float
    ) -> tuple[int, float]:
        """Skip to the next phase or step, broadcasting important messages inline."""
        current_phase = self._current_phase

        if current_phase in PHASE_SKIP_PHASES:
            return await self._skip_phase(current_idx, current_phase, playback_start)
        elif current_phase in STEP_SKIP_PHASES:
            return await self._skip_step(current_idx, playback_start)
        else:
            return current_idx, playback_start

    async def _skip_phase(
        self, start_idx: int, phase: str | None, playback_start: float
    ) -> tuple[int, float]:
        """Skip to the end of the current phase, broadcasting important messages."""
        last_frame_msg: dict | None = None
        i = start_idx

        while i < len(self._messages):
            msg = self._messages[i]
            msg_type = msg.get("type", "")

            # Track phase from status messages
            if msg_type == "status":
                data = msg.get("data", {})
                new_phase = data.get("phase")
                if new_phase and new_phase != phase:
                    # Phase changed — broadcast last frame, send skip_sync, resync
                    skipped_seconds = msg["t"] - self._messages[start_idx].get("t", 0)
                    if last_frame_msg:
                        frame_b64 = await self._load_frame_data(last_frame_msg)
                        if frame_b64:
                            self._last_frame_b64 = frame_b64
                            await self._broadcaster.broadcast_frame(frame_b64)
                    new_start = time.monotonic() - msg["t"]
                    await self._broadcaster.broadcast_skip_sync(skipped_seconds)
                    return i, new_start

            # Track last frame during skip
            if msg_type == "frame":
                last_frame_msg = msg
                self._last_frame_b64 = await self._load_frame_data(msg)
                if self._current_phase == "approach" and self._acquisition_frame is None:
                    self._acquisition_frame = self._last_frame_b64

            # Track AI results for report
            if msg_type == "ai_result":
                data = msg.get("data", {})
                if data.get("result_type") == "inspection":
                    self._last_inspection_result = data

            # Broadcast non-frame/telemetry messages during skip
            if msg_type in _SKIP_BROADCAST_TYPES:
                await self._broadcast_message(msg)

            i += 1

        # Reached end without phase change
        if last_frame_msg:
            frame_b64 = await self._load_frame_data(last_frame_msg)
            if frame_b64:
                self._last_frame_b64 = frame_b64
                await self._broadcaster.broadcast_frame(frame_b64)

        skipped_seconds = (
            self._messages[-1].get("t", 0) - self._messages[start_idx].get("t", 0)
            if self._messages else 0
        )
        await self._broadcaster.broadcast_skip_sync(skipped_seconds)
        return i, playback_start

    async def _skip_step(
        self, start_idx: int, playback_start: float
    ) -> tuple[int, float]:
        """Skip to the next approach step or inspection angle."""
        current_step = None
        last_frame_msg: dict | None = None
        i = start_idx

        # Find current step from recent status
        for j in range(start_idx, max(start_idx - 20, -1), -1):
            if j < len(self._messages) and self._messages[j].get("type") == "status":
                current_step = self._messages[j].get("data", {}).get("step")
                if current_step is not None:
                    break

        # Advance to next step, broadcasting important messages
        while i < len(self._messages):
            msg = self._messages[i]
            msg_type = msg.get("type", "")

            if msg_type == "status":
                data = msg.get("data", {})
                new_step = data.get("step")
                new_phase = data.get("phase")

                if new_phase != self._current_phase:
                    # Phase changed — broadcast last frame, send skip_sync, resync
                    skipped_seconds = msg["t"] - self._messages[start_idx].get("t", 0)
                    if last_frame_msg:
                        frame_b64 = await self._load_frame_data(last_frame_msg)
                        if frame_b64:
                            self._last_frame_b64 = frame_b64
                            await self._broadcaster.broadcast_frame(frame_b64)
                    new_start = time.monotonic() - msg["t"]
                    await self._broadcaster.broadcast_skip_sync(skipped_seconds)
                    return i, new_start

                if new_step is not None and new_step != current_step:
                    skipped_seconds = msg["t"] - self._messages[start_idx].get("t", 0)
                    if last_frame_msg:
                        frame_b64 = await self._load_frame_data(last_frame_msg)
                        if frame_b64:
                            self._last_frame_b64 = frame_b64
                            await self._broadcaster.broadcast_frame(frame_b64)
                    new_start = time.monotonic() - msg["t"]
                    await self._broadcaster.broadcast_skip_sync(skipped_seconds)
                    return i, new_start

            # Track last frame during skip
            if msg_type == "frame":
                last_frame_msg = msg
                self._last_frame_b64 = await self._load_frame_data(msg)

            # Track AI results for report
            if msg_type == "ai_result":
                data = msg.get("data", {})
                if data.get("result_type") == "inspection":
                    self._last_inspection_result = data

            # Broadcast non-frame/telemetry messages during skip
            if msg_type in _SKIP_BROADCAST_TYPES:
                await self._broadcast_message(msg)

            i += 1

        return i, playback_start

    async def _load_frame_data(self, msg: dict) -> str | None:
        """Load frame data — either inline base64 or from file reference."""
        data = msg.get("data")
        if not isinstance(data, str):
            return None

        # If data looks like a filename (e.g., "0.1001.jpg"), load from disk
        if data.endswith(".jpg"):
            frame_path = self._frames_dir / data
            if frame_path.exists():
                jpeg_bytes = frame_path.read_bytes()
                return base64.b64encode(jpeg_bytes).decode("ascii")
            return None

        # Already base64
        return data

    async def _broadcast_message(self, msg: dict) -> None:
        """Broadcast a recorded message through the dashboard broadcaster."""
        msg_type = msg.get("type", "")
        data = msg.get("data")

        if msg_type == "frame":
            frame_b64 = await self._load_frame_data(msg)
            if frame_b64:
                await self._broadcaster.broadcast_frame(frame_b64)
        elif msg_type == "telemetry":
            await self._broadcaster.broadcast_telemetry(data or {})
        elif msg_type == "status":
            await self._broadcaster.broadcast_status(data or {})
        elif msg_type == "perception":
            await self._broadcaster.broadcast_perception(data or {})
        elif msg_type == "log":
            if isinstance(data, dict):
                await self._broadcaster.broadcast_log(
                    data.get("level", "INFO"),
                    data.get("message", ""),
                )
            elif isinstance(data, str):
                await self._broadcaster.broadcast_log("INFO", data)
        elif msg_type == "transcript":
            if isinstance(data, dict):
                await self._broadcaster.broadcast_transcript(
                    data.get("speaker", "SYSTEM"),
                    data.get("text", ""),
                )
        elif msg_type == "ai_activity":
            await self._broadcaster.broadcast_ai_activity(data or {})
        elif msg_type == "ai_result":
            await self._broadcaster.broadcast_ai_result(data or {})
        elif msg_type == "report_data":
            await self._broadcaster.broadcast_report_data(data or {})

    async def _send_synthetic_report(self) -> None:
        """Synthesize and broadcast report_data at playback end."""
        report_data: dict[str, Any] = {
            "metadata": {
                "target": self._metadata.get("target", ""),
                "mode": self._metadata.get("mode", "exploration"),
                "duration_seconds": self._metadata.get("duration_sec", 0),
                "phases_completed": ["search", "approach", "inspect"],
            },
        }

        if self._acquisition_frame:
            report_data["acquisition_frame"] = self._acquisition_frame
        if self._last_frame_b64:
            report_data["inspection_frame"] = self._last_frame_b64
        if self._last_inspection_result:
            report_data["inspection_result"] = self._last_inspection_result

        await self._broadcaster.broadcast_report_data(report_data)

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
        playback_start = time.monotonic()

        logger.info("Playback started: %s", self._metadata.get("target", ""))

        i = 0
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

            # Handle skip
            if self._skip_event.is_set():
                self._skip_event.clear()
                i, playback_start = self._handle_skip(i, playback_start)
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

    def _handle_skip(self, current_idx: int, playback_start: float) -> tuple[int, float]:
        """Skip to the next phase or step. Returns (new_index, new_playback_start)."""
        current_phase = self._current_phase

        if current_phase in PHASE_SKIP_PHASES:
            # Phase-level skip: fast-forward all messages in current phase
            # Still broadcast status/log/ai_result messages, skip frame/telemetry
            return self._skip_phase(current_idx, current_phase, playback_start)
        elif current_phase in STEP_SKIP_PHASES:
            # Step-level skip: skip to next step within phase
            return self._skip_step(current_idx, playback_start)
        else:
            # No skip possible
            return current_idx, playback_start

    def _skip_phase(
        self, start_idx: int, phase: str | None, playback_start: float
    ) -> tuple[int, float]:
        """Skip to the end of the current phase, broadcasting important messages."""
        i = start_idx
        while i < len(self._messages):
            msg = self._messages[i]
            msg_type = msg.get("type", "")

            # Check if we've moved to a new phase
            if msg_type == "status":
                data = msg.get("data", {})
                new_phase = data.get("phase")
                if new_phase and new_phase != phase:
                    # Resync timer
                    new_start = time.monotonic() - msg["t"]
                    return i, new_start

            i += 1

        return i, playback_start

    def _skip_step(self, start_idx: int, playback_start: float) -> tuple[int, float]:
        """Skip to the next approach step or inspection angle."""
        current_step = None
        i = start_idx

        # Find current step from recent status
        for j in range(start_idx, max(start_idx - 20, -1), -1):
            if j < len(self._messages) and self._messages[j].get("type") == "status":
                current_step = self._messages[j].get("data", {}).get("step")
                if current_step is not None:
                    break

        # Advance to next step
        while i < len(self._messages):
            msg = self._messages[i]
            if msg.get("type") == "status":
                data = msg.get("data", {})
                new_step = data.get("step")
                new_phase = data.get("phase")

                if new_phase != self._current_phase:
                    # Phase changed — resync and return
                    new_start = time.monotonic() - msg["t"]
                    return i, new_start

                if new_step is not None and new_step != current_step:
                    new_start = time.monotonic() - msg["t"]
                    return i, new_start
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

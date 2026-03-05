"""Demo recording middleware — transparent capture of dashboard messages.

Attaches to DashboardBroadcaster as middleware. Every broadcast_json() call
also writes the message with a relative timestamp to a JSONL file. Frame
data is written to disk as JPEG files to keep the session.json manageable.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DemoRecorder:
    """Records dashboard messages to a directory for later playback.

    Output structure:
        <output_dir>/
            session.json    — JSONL: line 1 = metadata, lines 2+ = timestamped messages
            frames/         — JPEG files named <relative_timestamp>.jpg
    """

    def __init__(self, output_dir: str | Path) -> None:
        self._output_dir = Path(output_dir)
        self._frames_dir = self._output_dir / "frames"
        self._session_file: Any = None
        self._start_time: float = 0.0
        self._message_count: int = 0
        self._recording: bool = False
        self._target: str = ""
        self._mode: str = ""

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self, target: str = "", mode: str = "exploration") -> None:
        """Begin recording. Writes metadata header to session.json."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._frames_dir.mkdir(exist_ok=True)

        self._target = target
        self._mode = mode
        self._start_time = time.monotonic()
        self._message_count = 0

        session_path = self._output_dir / "session.json"
        self._session_file = open(session_path, "w")

        # Write metadata as first line
        metadata = {
            "_meta": True,
            "version": 1,
            "target": target,
            "mode": mode,
            "recorded_at": time.time(),
            "duration_sec": 0,
            "message_count": 0,
        }
        self._session_file.write(json.dumps(metadata) + "\n")
        self._session_file.flush()
        self._recording = True
        logger.info("Demo recording started: %s", self._output_dir)

    def record(self, message: dict) -> None:
        """Record a single broadcast message with relative timestamp.

        Called by DashboardBroadcaster's broadcast_json() hook.
        """
        if not self._recording or self._session_file is None:
            return

        t = time.monotonic() - self._start_time
        msg_type = message.get("type", "")

        # For frame messages, write JPEG to disk instead of embedding in JSON
        if msg_type == "frame" and isinstance(message.get("data"), str):
            frame_filename = f"{t:.4f}.jpg"
            frame_path = self._frames_dir / frame_filename
            try:
                jpeg_bytes = base64.b64decode(message["data"])
                frame_path.write_bytes(jpeg_bytes)
            except Exception:
                logger.debug("Failed to write frame %s", frame_filename, exc_info=True)
                return

            # Write a reference entry instead of full base64
            entry = {"t": round(t, 4), "type": "frame", "data": frame_filename}
        else:
            entry = {"t": round(t, 4), "type": msg_type, "data": message.get("data")}

        try:
            self._session_file.write(json.dumps(entry) + "\n")
            self._session_file.flush()
            self._message_count += 1
        except Exception:
            logger.debug("Failed to write recording entry", exc_info=True)

    def stop(self) -> None:
        """Stop recording and finalize metadata."""
        if not self._recording:
            return

        self._recording = False
        duration = time.monotonic() - self._start_time

        if self._session_file is not None:
            self._session_file.close()
            self._session_file = None

        # Rewrite the first line with final metadata
        session_path = self._output_dir / "session.json"
        try:
            lines = session_path.read_text().splitlines()
            if lines:
                metadata = json.loads(lines[0])
                metadata["duration_sec"] = round(duration, 2)
                metadata["message_count"] = self._message_count
                lines[0] = json.dumps(metadata)
                session_path.write_text("\n".join(lines) + "\n")
        except Exception:
            logger.debug("Failed to update recording metadata", exc_info=True)

        logger.info(
            "Demo recording stopped: %d messages, %.1fs duration",
            self._message_count, duration,
        )

"""Thread-safe video frame capture with validation."""

import logging
import threading
import time
from typing import Any

import cv2
import numpy as np

from client.src.config import ClientConfig

logger = logging.getLogger(__name__)


class FrameCapture:
    """Captures and validates frames from drone video stream.

    Runs drone.get_frame_read() and provides thread-safe access
    to the latest validated frame with copy semantics (FR-011).
    """

    def __init__(self, drone: Any, config: ClientConfig) -> None:
        self._drone = drone
        self._config = config
        self._frame_read = None
        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._frame_counter: int = 0
        self._flush_event = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self._consecutive_failures: int = 0
        self._restart_count: int = 0

    def start(self) -> None:
        """Start video stream and background capture thread."""
        self._drone.streamon()
        self._frame_read = self._drone.get_frame_read()
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Frame capture started")

    def stop(self) -> None:
        """Stop frame capture."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self._drone.streamoff()
        except Exception:
            pass
        logger.info("Frame capture stopped")

    def _capture_loop(self) -> None:
        """Background thread that continuously captures and validates frames."""
        while self._running:
            try:
                if self._frame_read is None:
                    self._consecutive_failures += 1
                    self._check_stream_health()
                    time.sleep(0.03)
                    continue
                frame = self._frame_read.frame
                if frame is None:
                    self._consecutive_failures += 1
                    self._check_stream_health()
                    time.sleep(0.03)
                    continue

                validated = self._validate_frame(frame)
                if validated is not None:
                    self._consecutive_failures = 0
                    with self._lock:
                        self._latest_frame = validated
                        self._frame_counter += 1
                    self._flush_event.set()
                else:
                    self._consecutive_failures += 1
                    self._check_stream_health()
                    time.sleep(0.03)
            except Exception:
                logger.warning("Frame capture error", exc_info=True)
                self._consecutive_failures += 1
                self._check_stream_health()
                time.sleep(0.03)

    def _check_stream_health(self) -> None:
        """Trigger stream restart if consecutive failures exceed threshold."""
        if self._consecutive_failures < self._config.VIDEO_STREAM_FAIL_THRESHOLD:
            return
        if self._restart_count >= self._config.VIDEO_STREAM_MAX_RESTARTS:
            if self._consecutive_failures == self._config.VIDEO_STREAM_FAIL_THRESHOLD:
                logger.error(
                    "Video stream dead — max restarts (%d) exhausted",
                    self._config.VIDEO_STREAM_MAX_RESTARTS,
                )
            return
        self._attempt_stream_restart()

    def _attempt_stream_restart(self) -> None:
        """Restart the video stream (streamoff → delay → streamon)."""
        self._restart_count += 1
        logger.warning(
            "Attempting video stream restart (%d/%d)",
            self._restart_count,
            self._config.VIDEO_STREAM_MAX_RESTARTS,
        )
        try:
            self._drone.streamoff()
            time.sleep(self._config.VIDEO_STREAM_RESTART_DELAY)
            self._drone.streamon()
            self._frame_read = self._drone.get_frame_read()
            time.sleep(1.0)  # Let decoder fill pipeline
            self._consecutive_failures = 0
            logger.info("Video stream restart succeeded")
        except Exception:
            logger.warning("Video stream restart failed", exc_info=True)

    def _validate_frame(self, frame: np.ndarray) -> np.ndarray | None:
        """Validate frame: dimensions, black frame check, color conversion.

        Tello streams RGB but OpenCV imencode expects BGR (lesson G1).
        """
        if frame is None:
            return None

        h, w = frame.shape[:2]
        if w < self._config.MIN_FRAME_WIDTH or h < self._config.MIN_FRAME_HEIGHT:
            return None

        # Reject black frames (lesson L1)
        if np.mean(frame) < self._config.BLACK_FRAME_THRESHOLD:
            return None

        # Convert RGB to BGR for OpenCV (lesson G1)
        bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return bgr_frame

    def flush_and_wait(
        self, min_new_frames: int = 3, timeout: float = 3.0,
    ) -> np.ndarray | None:
        """Discard current frame and wait for fresh frames from the decode pipeline.

        Ensures the returned frame was captured *after* this call, which is
        critical after rotation/movement where the H264 decode pipeline may
        still be outputting stale pre-action frames.

        Returns the fresh frame or None on timeout.
        """
        with self._lock:
            start_counter = self._frame_counter
            self._latest_frame = None

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._flush_event.clear()
            with self._lock:
                if self._frame_counter - start_counter >= min_new_frames:
                    return self._latest_frame.copy() if self._latest_frame is not None else None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._flush_event.wait(timeout=min(remaining, 0.5))

        logger.warning(
            "flush_and_wait timed out after %.1fs (got %d/%d frames)",
            timeout, self._frame_counter - start_counter, min_new_frames,
        )
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def get_frame(self) -> np.ndarray | None:
        """Get latest validated frame (copy semantics for thread safety)."""
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

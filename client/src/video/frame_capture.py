"""Thread-safe video frame capture with validation."""

import logging
import threading
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
        self._running = False
        self._thread: threading.Thread | None = None

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
                    continue
                frame = self._frame_read.frame
                if frame is None:
                    continue

                validated = self._validate_frame(frame)
                if validated is not None:
                    with self._lock:
                        self._latest_frame = validated
            except Exception:
                logger.warning("Frame capture error", exc_info=True)

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

    def get_frame(self) -> np.ndarray | None:
        """Get latest validated frame (copy semantics for thread safety)."""
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

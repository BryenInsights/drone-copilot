"""Video frame encoding and streaming for backend and dashboard."""

import base64
import logging
import time

import cv2
import numpy as np

from client.src.config import ClientConfig
from client.src.video.frame_capture import FrameCapture

logger = logging.getLogger(__name__)


class FrameStreamer:
    """Encodes and streams video frames for backend (Gemini) and dashboard.

    - Perception frames: 768px wide JPEG for Gemini at 1 FPS
    - Dashboard frames: 960x720 JPEG at 10 FPS
    """

    def __init__(self, capture: FrameCapture, config: ClientConfig) -> None:
        self._capture = capture
        self._config = config
        self._last_perception_time: float = 0.0

    def _resize_frame(self, frame: np.ndarray, target_width: int) -> np.ndarray:
        """Resize frame maintaining aspect ratio."""
        h, w = frame.shape[:2]
        scale = target_width / w
        new_h = int(h * scale)
        return cv2.resize(frame, (target_width, new_h), interpolation=cv2.INTER_AREA)

    def _encode_jpeg(self, frame: np.ndarray) -> bytes | None:
        """Encode frame as JPEG bytes."""
        success, buffer = cv2.imencode(".jpg", frame)
        if not success:
            return None
        jpeg_bytes = buffer.tobytes()
        if len(jpeg_bytes) < self._config.MIN_FRAME_BYTES:
            return None
        return jpeg_bytes

    def get_perception_frame(self) -> str | None:
        """Get base64-encoded JPEG frame for backend (768px wide, rate-limited to 1 FPS)."""
        now = time.time()
        interval = 1.0  # 1 FPS default for perception frames sent to Gemini
        if now - self._last_perception_time < interval:
            return None

        frame = self._capture.get_frame()
        if frame is None:
            return None

        resized = self._resize_frame(frame, self._config.PERCEPTION_FRAME_WIDTH)
        jpeg_bytes = self._encode_jpeg(resized)
        if jpeg_bytes is None:
            return None

        self._last_perception_time = now
        return base64.b64encode(jpeg_bytes).decode("ascii")

    def get_perception_frame_bytes(self) -> bytes | None:
        """Get raw JPEG bytes for backend (768px wide, rate-limited)."""
        now = time.time()
        interval = 1.0  # 1 FPS default for perception frames sent to Gemini
        if now - self._last_perception_time < interval:
            return None

        frame = self._capture.get_frame()
        if frame is None:
            return None

        resized = self._resize_frame(frame, self._config.PERCEPTION_FRAME_WIDTH)
        jpeg_bytes = self._encode_jpeg(resized)
        if jpeg_bytes is None:
            return None

        self._last_perception_time = now
        return jpeg_bytes

    def get_dashboard_frame(self) -> bytes | None:
        """Get 960x720 JPEG bytes for dashboard display."""
        frame = self._capture.get_frame()
        if frame is None:
            return None

        resized = cv2.resize(frame, (960, 720), interpolation=cv2.INTER_AREA)
        return self._encode_jpeg(resized)

    def get_dashboard_frame_base64(self) -> str | None:
        """Get base64-encoded dashboard frame."""
        jpeg_bytes = self.get_dashboard_frame()
        if jpeg_bytes is None:
            return None
        return base64.b64encode(jpeg_bytes).decode("ascii")

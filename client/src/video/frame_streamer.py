"""Video frame encoding and streaming for backend and dashboard."""

import base64
import logging
import threading
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
        self._video_gate = threading.Event()
        self._video_gate.set()  # Open by default — video loop sends normally

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

    def pause_perception_stream(self) -> None:
        """Pause continuous perception stream. Mission takes exclusive frame control."""
        self._video_gate.clear()
        logger.debug("Perception stream paused — mission has exclusive frame control")

    def resume_perception_stream(self) -> None:
        """Resume continuous perception stream. Resets rate limit for immediate send."""
        self._video_gate.set()
        self._last_perception_time = 0.0
        logger.debug("Perception stream resumed")

    def reset_rate_limit(self) -> None:
        """Reset perception rate limit so the next frame goes through immediately.

        Called after rotation/movement to ensure Gemini sees a fresh post-action frame.
        """
        self._last_perception_time = 0.0

    def get_perception_frame(self) -> str | None:
        """Get base64-encoded JPEG frame for backend (768px wide, rate-limited to 1 FPS)."""
        if not self._video_gate.is_set():
            return None  # Mission has exclusive control
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
        if not self._video_gate.is_set():
            return None  # Mission has exclusive control
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

    def get_fresh_perception_frame(self, timeout: float = 3.0) -> str | None:
        """Flush stale frames and return a guaranteed-fresh perception frame.

        Used after rotation/movement to ensure Gemini sees a post-action frame.
        Bypasses the 1 FPS rate limit and updates _last_perception_time so the
        regular streaming loop doesn't immediately re-send.
        """
        frame = self._capture.flush_and_wait(timeout=timeout)
        if frame is None:
            return None

        resized = self._resize_frame(frame, self._config.PERCEPTION_FRAME_WIDTH)
        jpeg_bytes = self._encode_jpeg(resized)
        if jpeg_bytes is None:
            return None

        self._last_perception_time = time.time()
        return base64.b64encode(jpeg_bytes).decode("ascii")

    def get_fresh_perception_frame_bytes(self, timeout: float = 3.0) -> bytes | None:
        """Flush stale frames and return fresh perception JPEG as raw bytes."""
        frame = self._capture.flush_and_wait(timeout=timeout)
        if frame is None:
            return None
        resized = self._resize_frame(frame, self._config.PERCEPTION_FRAME_WIDTH)
        jpeg_bytes = self._encode_jpeg(resized)
        if jpeg_bytes is None:
            return None
        self._last_perception_time = time.time()
        return jpeg_bytes

    def get_fresh_dashboard_frame(self, timeout: float = 3.0) -> bytes | None:
        """Flush stale H264 frames and return a guaranteed-fresh 960x720 JPEG.

        Used by the inspection phase after strafe movements to ensure
        captured frames reflect the current camera view, not pre-movement
        frames still in the H264 decode pipeline.
        """
        frame = self._capture.flush_and_wait(min_new_frames=3, timeout=timeout)
        if frame is None:
            return None
        resized = cv2.resize(frame, (960, 720), interpolation=cv2.INTER_AREA)
        return self._encode_jpeg(resized)

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

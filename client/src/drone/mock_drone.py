"""Mock implementation of djitellopy.Tello for testing without hardware."""

from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)


class MockFrameRead:
    """Simulates Tello frame_read object with synthetic video frames."""

    def __init__(self) -> None:
        self.stopped = False
        self._frame: np.ndarray | None = None
        self._generate_frame("MOCK DRONE - GROUNDED")

    def _generate_frame(self, text: str = "MOCK DRONE") -> None:
        """Generate a synthetic 960x720 RGB frame with text overlay."""
        frame = np.zeros((720, 960, 3), dtype=np.uint8)
        frame[:, :, 0] = 40  # slight blue tint
        frame[:, :, 1] = 60
        frame[:, :, 2] = 80
        # We'd use cv2.putText if available, but keep it simple for mock
        self._frame = frame
        self._text = text

    @property
    def frame(self) -> np.ndarray | None:
        if self.stopped:
            return None
        return self._frame.copy() if self._frame is not None else None

    def stop(self) -> None:
        self.stopped = True


class MockDrone:
    """Simulates djitellopy.Tello interface for testing without hardware.

    Maintains simulated state: battery drains over time, altitude changes
    with takeoff/land/move, temperature stays stable.
    """

    def __init__(self) -> None:
        self._battery = 100
        self._height = 0  # cm
        self._temperature = 35
        self._flight_time = 0
        self._speed = 10
        self._is_flying = False
        self._stream_on = False
        self._frame_read: MockFrameRead | None = None
        self._takeoff_time: float | None = None
        self._connected = False
        self.retry_count = 1
        logger.info("MockDrone initialized")

    def connect(self) -> None:
        self._connected = True
        logger.info("MockDrone connected")

    def takeoff(self) -> None:
        if self._is_flying:
            logger.warning("MockDrone: already flying, ignoring takeoff")
            return
        self._is_flying = True
        self._height = 80  # default takeoff height ~80cm
        self._takeoff_time = time.time()
        self._drain_battery(2)
        logger.info("MockDrone: takeoff complete, altitude=%dcm", self._height)

    def land(self) -> None:
        self._is_flying = False
        self._height = 0
        self._takeoff_time = None
        self._drain_battery(1)
        logger.info("MockDrone: landed")

    def emergency(self) -> None:
        """Emergency motor stop."""
        self._is_flying = False
        self._height = 0
        self._takeoff_time = None
        logger.warning("MockDrone: EMERGENCY motor stop")

    def move_forward(self, distance: int) -> None:
        self._validate_flying("move_forward")
        self._drain_battery(1)
        logger.info("MockDrone: move_forward %dcm", distance)

    def move_back(self, distance: int) -> None:
        self._validate_flying("move_back")
        self._drain_battery(1)
        logger.info("MockDrone: move_back %dcm", distance)

    def move_left(self, distance: int) -> None:
        self._validate_flying("move_left")
        self._drain_battery(1)
        logger.info("MockDrone: move_left %dcm", distance)

    def move_right(self, distance: int) -> None:
        self._validate_flying("move_right")
        self._drain_battery(1)
        logger.info("MockDrone: move_right %dcm", distance)

    def move_up(self, distance: int) -> None:
        self._validate_flying("move_up")
        self._height = min(300, self._height + distance)
        self._drain_battery(1)
        logger.info("MockDrone: move_up %dcm, altitude=%dcm", distance, self._height)

    def move_down(self, distance: int) -> None:
        self._validate_flying("move_down")
        self._height = max(20, self._height - distance)
        self._drain_battery(1)
        logger.info("MockDrone: move_down %dcm, altitude=%dcm", distance, self._height)

    def curve_xyz_speed(
        self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, speed: int,
    ) -> None:
        self._validate_flying("curve_xyz_speed")
        self._drain_battery(2)
        logger.info(
            "MockDrone: curve_xyz_speed mid=(%d,%d,%d) end=(%d,%d,%d) speed=%d",
            x1, y1, z1, x2, y2, z2, speed,
        )

    def rotate_clockwise(self, degrees: int) -> None:
        self._validate_flying("rotate_clockwise")
        self._drain_battery(1)
        logger.info("MockDrone: rotate_clockwise %d°", degrees)

    def rotate_counter_clockwise(self, degrees: int) -> None:
        self._validate_flying("rotate_counter_clockwise")
        self._drain_battery(1)
        logger.info("MockDrone: rotate_counter_clockwise %d°", degrees)

    def set_speed(self, speed: int) -> None:
        self._speed = max(10, min(100, speed))
        logger.info("MockDrone: speed set to %d cm/s", self._speed)

    def send_rc_control(self, lr: int, fb: int, ud: int, yaw: int) -> None:
        """RC control — used for hover (all zeros = stop)."""
        logger.debug("MockDrone: rc_control lr=%d fb=%d ud=%d yaw=%d", lr, fb, ud, yaw)

    def query_battery(self) -> int:
        """Query battery (SDK command used by heartbeat)."""
        return self._battery

    def get_battery(self) -> int:
        return self._battery

    def get_height(self) -> int:
        return self._height

    def get_temperature(self) -> int:
        return self._temperature

    def get_flight_time(self) -> int:
        if self._takeoff_time is not None:
            return int(time.time() - self._takeoff_time)
        return 0

    def get_speed_x(self) -> int:
        return 0

    def streamon(self) -> None:
        self._stream_on = True
        self._frame_read = MockFrameRead()
        logger.info("MockDrone: video stream started")

    def streamoff(self) -> None:
        self._stream_on = False
        if self._frame_read:
            self._frame_read.stop()
        logger.info("MockDrone: video stream stopped")

    def get_frame_read(self) -> MockFrameRead:
        if self._frame_read is None:
            self._frame_read = MockFrameRead()
        return self._frame_read

    def end(self) -> None:
        """Clean shutdown."""
        if self._is_flying:
            self.land()
        if self._stream_on:
            self.streamoff()
        self._connected = False
        logger.info("MockDrone: shutdown complete")

    def _validate_flying(self, cmd: str) -> None:
        if not self._is_flying:
            raise RuntimeError(f"MockDrone: cannot {cmd} — not flying")

    def _drain_battery(self, amount: int) -> None:
        self._battery = max(0, self._battery - amount)

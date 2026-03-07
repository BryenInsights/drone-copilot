"""Client configuration for the drone copilot.

Loads settings from environment variables and .env file using pydantic-settings.
All drone hardware thresholds, API parameters, and environment settings are
defined here with sensible defaults.
"""

import logging
import sys

from pydantic import field_validator
from pydantic_settings import BaseSettings


class ClientConfig(BaseSettings):
    """Central configuration for the drone copilot client."""

    model_config = {"env_file": ".env", "extra": "ignore"}

    # ── Drone Hardware Thresholds ────────────────────────────────────────
    MIN_MOVE_DISTANCE: int = 20
    MAX_MOVE_DISTANCE: int = 200
    MIN_ROTATION: int = 10
    MAX_ROTATION: int = 360
    POST_TAKEOFF_STABILIZATION: float = 4.0
    INTER_COMMAND_MOVE_DELAY: float = 2.0
    INTER_COMMAND_ROTATE_DELAY: float = 2.5
    APPROACH_MOVE_DELAY: float = 1.0
    APPROACH_ROTATE_DELAY: float = 1.5
    INSPECTION_MAX_APPROACH_STEPS: int = 25
    INSPECTION_APPROACH_SIZE_THRESHOLD: float = 0.45
    INSPECTION_PERCEPTION_TIMEOUT: float = 8.0
    INSPECTION_SCAN_STEP_DEGREES: int = 90
    INSPECTION_STAGNATION_LIMIT: int = 6
    INSPECTION_STAGNATION_THRESHOLD: float = 0.008
    SEARCH_ROTATION_STEP: int = 45
    SEARCH_PERCEPTION_TIMEOUT: float = 8.0
    SEARCH_MIN_CONFIDENCE: float = 0.5
    SEARCH_MAX_POSITIONS: int = 8
    INSPECTION_FORWARD_FAR: int = 40
    INSPECTION_FORWARD_MEDIUM: int = 30
    INSPECTION_FORWARD_CLOSE: int = 20
    INSPECTION_CENTER_THRESHOLD: float = 0.25
    INSPECTION_ROTATION_GAIN: float = 25.0
    INSPECTION_STRAFE_ZONE_THRESHOLD: float = 0.15
    INSPECTION_KP_LATERAL: float = 40.0
    INSPECTION_MIN_STRAFE: int = 20
    INSPECTION_MAX_STRAFE: int = 50
    INSPECTION_POST_MOVE_CLAMP: int = 20
    INSPECTION_ORBIT_RADIUS: int = 100
    INSPECTION_ORBIT_ANGLE: int = 45
    INSPECTION_ORBIT_SPEED: int = 20
    INSPECTION_ORBIT_STABILIZE: float = 1.5
    INSPECTION_H_DEADBAND: float = 0.08
    INSPECTION_SEARCH_RECOVERY_DEG: int = 30
    INSPECTION_V_DEADBAND: float = 0.15
    INSPECTION_KP_VERTICAL: float = 30.0
    INSPECTION_SKIP_VERTICAL_CM: int = 10
    INSPECTION_MIN_VERTICAL: int = 20
    INSPECTION_MAX_VERTICAL: int = 40
    INSPECTION_MAX_BLIND_STEPS: int = 3
    INSPECTION_APPROACH_WATCHDOG_S: float = 120.0
    HEARTBEAT_INTERVAL: int = 5
    BATTERY_MIN_CONTINUE: int = 20
    BATTERY_MIN_TAKEOFF: int = 25
    TEMPERATURE_MAX: int = 80
    FRAME_EXPECTED_WIDTH: int = 960
    FRAME_EXPECTED_HEIGHT: int = 720
    MIN_FRAME_WIDTH: int = 640
    MIN_FRAME_HEIGHT: int = 480
    BLACK_FRAME_THRESHOLD: float = 5.0
    MIN_FRAME_BYTES: int = 1000
    DJITELLOPY_RETRY_COUNT: int = 1
    VIDEO_STREAM_FAIL_THRESHOLD: int = 30  # ~1s at 30fps
    VIDEO_STREAM_MAX_RESTARTS: int = 3
    VIDEO_STREAM_RESTART_DELAY: float = 2.0  # seconds between off/on

    # ── Cost Reduction ─────────────────────────────────────────────────
    IDLE_FRAME_INTERVAL: float = 5.0  # Seconds between perception frames when idle
    VAD_ENABLED: bool = True
    VAD_AGGRESSIVENESS: int = 2  # 0-3, higher = more aggressive filtering
    VAD_HANGOVER_CHUNKS: int = 10  # 100ms chunks to keep sending after speech stops

    @field_validator("IDLE_FRAME_INTERVAL")
    @classmethod
    def _validate_idle_frame_interval(cls, v: float) -> float:
        if v <= 0:
            return 5.0
        return v

    @field_validator("VAD_AGGRESSIVENESS")
    @classmethod
    def _validate_vad_aggressiveness(cls, v: int) -> int:
        return max(0, min(3, v))

    @field_validator("VAD_HANGOVER_CHUNKS")
    @classmethod
    def _validate_vad_hangover_chunks(cls, v: int) -> int:
        return max(0, v)

    # ── Gemini API ───────────────────────────────────────────────────────
    API_TIMEOUT_MS: int = 60000
    MAX_API_RETRIES: int = 2
    PERCEPTION_FRAME_WIDTH: int = 768

    # ── Debug ─────────────────────────────────────────────────────────────
    DEBUG_SAVE_FRAMES: bool = False  # Save perception frames to debug_frames/

    # ── Environment ──────────────────────────────────────────────────────
    USE_MOCK_DRONE: bool = True
    BACKEND_URL: str = "ws://localhost:8080/ws"
    DASHBOARD_PORT: int = 8081
    LOG_LEVEL: str = "INFO"


def setup_logging(config: ClientConfig | None = None) -> None:
    """Configure Python logging with ISO timestamps and module names."""
    if config is None:
        config = ClientConfig()

    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%dT%H:%M:%S%z"

    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format=log_format,
        datefmt=date_format,
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )

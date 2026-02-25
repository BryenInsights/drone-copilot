"""Client configuration for the drone copilot.

Loads settings from environment variables and .env file using pydantic-settings.
All drone hardware thresholds, API parameters, approach controller gains,
and environment settings are defined here with sensible defaults.
"""

import logging
import sys

from pydantic_settings import BaseSettings


class ClientConfig(BaseSettings):
    """Central configuration for the drone copilot client."""

    model_config = {"env_file": ".env"}

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
    HEARTBEAT_INTERVAL: int = 10
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

    # ── Gemini API ───────────────────────────────────────────────────────
    API_TIMEOUT_MS: int = 60000
    MULTI_FRAME_TIMEOUT_MS: int = 120000
    PERCEPTION_TIMEOUT_S: int = 45
    MAX_API_RETRIES: int = 2
    PERCEPTION_FRAME_WIDTH: int = 768

    # ── Approach Controller ──────────────────────────────────────────────
    KP_ROTATION: float = 25.0
    KP_FORWARD: float = 50.0
    KP_VERTICAL: float = 40.0
    KP_LATERAL: float = 50.0
    HORIZONTAL_DEADBAND: float = 0.08
    VERTICAL_DEADBAND: float = 0.06
    SKIP_ROTATION_THRESHOLD: float = 3.0
    SKIP_VERTICAL_THRESHOLD: float = 5.0
    SKIP_LATERAL_THRESHOLD: float = 15.0
    COMPLETION_SIZE: float = 0.20
    CENTERING_THRESHOLD: float = 0.30
    STRAFE_ZONE_THRESHOLD: float = 0.15
    EMA_ALPHA: float = 0.50
    MAX_FORWARD_FAR: int = 40
    MAX_FORWARD_MEDIUM: int = 30
    MAX_FORWARD_CLOSE: int = 20
    MAX_APPROACH_STEPS: int = 15
    APPROACH_WATCHDOG_S: int = 120

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

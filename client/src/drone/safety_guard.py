"""Safety guard for drone operations — validates all commands before execution."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from client.src.config import ClientConfig
from client.src.models.drone_state import DroneState

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of a safety validation check."""

    safe: bool
    reason: str = ""


class SafetyGuard:
    """Validates drone commands against safety constraints.

    All thresholds sourced from ClientConfig (LESSONS_LEARNED.md).
    """

    def __init__(self, config: ClientConfig, drone_state: DroneState) -> None:
        self.config = config
        self.state = drone_state

    def validate_takeoff(self) -> ValidationResult:
        """Check if takeoff is safe."""
        if self.state.is_flying:
            return ValidationResult(False, "Already flying")
        if self.state.battery < self.config.BATTERY_MIN_TAKEOFF:
            return ValidationResult(
                False,
                f"Battery {self.state.battery}% below takeoff minimum "
                f"{self.config.BATTERY_MIN_TAKEOFF}%",
            )
        if self.state.temperature >= self.config.TEMPERATURE_MAX:
            return ValidationResult(
                False,
                f"Temperature {self.state.temperature}°C exceeds max "
                f"{self.config.TEMPERATURE_MAX}°C",
            )
        return ValidationResult(True)

    def validate_command(self) -> ValidationResult:
        """Check if a flight command can be executed."""
        if not self.state.is_flying:
            return ValidationResult(False, "Not flying")
        if self.state.battery < self.config.BATTERY_MIN_CONTINUE:
            return ValidationResult(
                False,
                f"Battery {self.state.battery}% below continue minimum "
                f"{self.config.BATTERY_MIN_CONTINUE}%",
            )
        if self.state.temperature >= self.config.TEMPERATURE_MAX:
            return ValidationResult(
                False,
                f"Temperature {self.state.temperature}°C exceeds max "
                f"{self.config.TEMPERATURE_MAX}°C",
            )
        # Check post-takeoff stabilization
        if self.state.takeoff_time is not None:
            elapsed = time.time() - self.state.takeoff_time
            if elapsed < self.config.POST_TAKEOFF_STABILIZATION:
                remaining = self.config.POST_TAKEOFF_STABILIZATION - elapsed
                return ValidationResult(
                    False,
                    f"Post-takeoff stabilization: {remaining:.1f}s remaining",
                )
        return ValidationResult(True)

    def clamp_move_distance(self, distance_cm: int, direction: str) -> int:
        """Clamp movement distance to safe range.

        For 'down' movements, additionally clamp to avoid ground collision.
        """
        clamped = max(
            self.config.MIN_MOVE_DISTANCE,
            min(self.config.MAX_MOVE_DISTANCE, distance_cm),
        )

        if direction == "down":
            # Don't descend below ~20cm altitude
            max_descent = max(0, int(self.state.altitude) - 20)
            clamped = min(clamped, max_descent)
            clamped = max(self.config.MIN_MOVE_DISTANCE, clamped)

        if clamped != distance_cm:
            logger.info(
                "SafetyGuard: clamped %s distance %dcm → %dcm",
                direction,
                distance_cm,
                clamped,
            )
        return clamped

    def clamp_rotation(self, degrees: int) -> int:
        """Clamp rotation to safe range [10, 360]."""
        clamped = max(self.config.MIN_ROTATION, min(self.config.MAX_ROTATION, degrees))
        if clamped != degrees:
            logger.info("SafetyGuard: clamped rotation %d° → %d°", degrees, clamped)
        return clamped

    def check_battery_critical(self) -> bool:
        """Check if battery is critically low — triggers auto-land."""
        if self.state.battery < self.config.BATTERY_MIN_CONTINUE:
            logger.warning(
                "SafetyGuard: CRITICAL battery %d%% < %d%% — auto-land required",
                self.state.battery,
                self.config.BATTERY_MIN_CONTINUE,
            )
            return True
        return False

    def check_temperature_critical(self) -> bool:
        """Check if temperature is critically high — triggers auto-land."""
        if self.state.temperature >= self.config.TEMPERATURE_MAX:
            logger.warning(
                "SafetyGuard: CRITICAL temperature %d°C >= %d°C — auto-land required",
                self.state.temperature,
                self.config.TEMPERATURE_MAX,
            )
            return True
        return False

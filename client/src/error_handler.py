"""Error handling framework with category-based recovery strategies."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from client.src.drone.controller import DroneController

logger = logging.getLogger(__name__)


class ErrorCategory(StrEnum):
    CONNECTION = "CONNECTION"
    COMMAND = "COMMAND"
    AI = "AI"
    SAFETY = "SAFETY"
    HARDWARE = "HARDWARE"
    UNKNOWN = "UNKNOWN"


# Keyword-based error categorization (lesson J1)
_CATEGORY_KEYWORDS: dict[ErrorCategory, list[str]] = {
    ErrorCategory.CONNECTION: [
        "connection", "websocket", "timeout", "refused", "reset",
        "disconnect", "network", "unreachable", "dns", "ssl",
    ],
    ErrorCategory.COMMAND: [
        "command", "not joystick", "motor", "move", "rotate", "tello",
    ],
    ErrorCategory.AI: [
        "gemini", "genai", "api", "rate_limit", "quota", "model",
        "resource_exhausted", "invalid_argument",
    ],
    ErrorCategory.SAFETY: [
        "battery", "temperature", "altitude", "stabilization", "safety",
        "emergency", "critical",
    ],
    ErrorCategory.HARDWARE: [
        "hardware", "sensor", "camera", "video", "frame", "stream",
        "wifi", "signal",
    ],
}


def categorize_error(error: Exception) -> ErrorCategory:
    """Categorize an exception based on its message and type."""
    msg = str(error).lower()
    error_type = type(error).__name__.lower()
    combined = f"{error_type} {msg}"

    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return category

    return ErrorCategory.UNKNOWN


class ErrorHandler:
    """Handles errors with category-specific recovery strategies.

    Integrates with DroneController for landing on critical errors.
    """

    def __init__(self, controller: DroneController | None = None) -> None:
        self._controller = controller
        self._ai_retry_counts: dict[str, int] = {}

    @property
    def controller(self) -> DroneController | None:
        return self._controller

    @controller.setter
    def controller(self, value: DroneController) -> None:
        self._controller = value

    def handle(self, error: Exception, context: str = "") -> str:
        """Handle an error with category-appropriate recovery.

        Returns a string describing the recovery action taken.
        """
        category = categorize_error(error)
        logger.error(
            "Error [%s] in %s: %s — %s",
            category,
            context or "unknown",
            type(error).__name__,
            error,
        )

        if category == ErrorCategory.SAFETY:
            return self._handle_safety(error, context)
        elif category == ErrorCategory.HARDWARE:
            return self._handle_hardware(error, context)
        elif category == ErrorCategory.CONNECTION:
            return self._handle_connection(error, context)
        elif category == ErrorCategory.AI:
            return self._handle_ai(error, context)
        elif category == ErrorCategory.COMMAND:
            return self._handle_command(error, context)
        else:
            return self._handle_unknown(error, context)

    def _handle_safety(self, error: Exception, context: str) -> str:
        """SAFETY → always land immediately."""
        logger.warning("Safety error — initiating emergency land")
        if self._controller:
            self._controller.emergency_land()
        return "emergency_land"

    def _handle_hardware(self, error: Exception, context: str) -> str:
        """HARDWARE → abort mission and land."""
        logger.warning("Hardware error — aborting mission and landing")
        if self._controller:
            self._controller.emergency_land()
        return "abort_and_land"

    def _handle_connection(self, error: Exception, context: str) -> str:
        """CONNECTION + in-flight → land; on-ground → retry connect."""
        if self._controller and self._controller.state.is_flying:
            logger.warning("Connection error while flying — landing")
            self._controller.emergency_land()
            return "emergency_land"
        logger.info("Connection error on ground — will retry")
        return "retry_connect"

    def _handle_ai(self, error: Exception, context: str) -> str:
        """AI → retry up to 2 times, then skip."""
        key = context or "default"
        count = self._ai_retry_counts.get(key, 0)
        if count < 2:
            self._ai_retry_counts[key] = count + 1
            logger.info("AI error — retry %d/2 for %s", count + 1, key)
            return "retry"
        logger.warning("AI error — max retries reached for %s, skipping", key)
        self._ai_retry_counts.pop(key, None)
        return "skip"

    def _handle_command(self, error: Exception, context: str) -> str:
        """COMMAND → log and continue."""
        logger.warning("Command error — continuing: %s", error)
        return "continue"

    def _handle_unknown(self, error: Exception, context: str) -> str:
        """UNKNOWN → log and continue."""
        logger.warning("Unknown error — continuing: %s", error)
        return "continue"

    def reset_ai_retries(self, context: str = "") -> None:
        """Reset AI retry counter for a given context."""
        key = context or "default"
        self._ai_retry_counts.pop(key, None)

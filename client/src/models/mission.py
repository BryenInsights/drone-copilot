"""Mission and MissionStatus Pydantic models."""

from __future__ import annotations

import logging
import threading
import uuid
from enum import StrEnum

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MissionType(StrEnum):
    EXPLORE = "explore"
    INSPECT = "inspect"
    FREEFORM = "freeform"


class MissionStatus(StrEnum):
    IDLE = "idle"
    SEARCHING = "searching"
    APPROACHING = "approaching"
    REPOSITIONING = "repositioning"
    INSPECTING = "inspecting"
    COMPLETE = "complete"
    ABORTED = "aborted"


class Mission(BaseModel):
    """High-level user objective. One active mission at a time."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: MissionType = MissionType.FREEFORM
    status: MissionStatus = MissionStatus.IDLE
    target_description: str | None = None
    started_at: float | None = None
    final_relative_size: float | None = None


class InvalidTransitionError(Exception):
    """Raised when a mission status transition is not allowed."""


class MissionStateMachine:
    """Thread-safe state machine guarding Mission.status transitions.

    Prevents race conditions between the mission thread and voice-abort
    commands by locking all status reads/writes.
    """

    _ALLOWED_TRANSITIONS: dict[MissionStatus, set[MissionStatus]] = {
        MissionStatus.IDLE: {
            MissionStatus.SEARCHING,
            MissionStatus.APPROACHING,
            MissionStatus.ABORTED,
        },
        MissionStatus.SEARCHING: {
            MissionStatus.APPROACHING,
            MissionStatus.ABORTED,
            MissionStatus.COMPLETE,
        },
        MissionStatus.APPROACHING: {
            MissionStatus.REPOSITIONING,
            MissionStatus.INSPECTING,
            MissionStatus.ABORTED,
            MissionStatus.COMPLETE,
        },
        MissionStatus.REPOSITIONING: {
            MissionStatus.INSPECTING,
            MissionStatus.ABORTED,
        },
        MissionStatus.INSPECTING: {
            MissionStatus.COMPLETE,
            MissionStatus.ABORTED,
        },
        MissionStatus.COMPLETE: set(),
        MissionStatus.ABORTED: set(),
    }

    def __init__(self, mission: Mission) -> None:
        self._mission = mission
        self._lock = threading.Lock()
        self.history: list[MissionStatus] = [mission.status]

    @property
    def status(self) -> MissionStatus:
        with self._lock:
            return self._mission.status

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self._mission.status in (MissionStatus.COMPLETE, MissionStatus.ABORTED)

    def transition(self, new_status: MissionStatus) -> None:
        """Transition to a new status. Raises InvalidTransitionError if not allowed."""
        with self._lock:
            current = self._mission.status
            allowed = self._ALLOWED_TRANSITIONS.get(current, set())
            if new_status not in allowed:
                raise InvalidTransitionError(
                    f"Cannot transition from {current} to {new_status}"
                )
            self._mission.status = new_status
            self.history.append(new_status)
            logger.info("Mission %s: %s → %s", self._mission.id[:8], current, new_status)

    def try_transition(self, new_status: MissionStatus) -> bool:
        """Attempt a transition, returning False instead of raising on failure."""
        try:
            self.transition(new_status)
            return True
        except InvalidTransitionError:
            logger.debug(
                "Mission %s: transition to %s skipped (current=%s)",
                self._mission.id[:8],
                new_status,
                self._mission.status,
            )
            return False

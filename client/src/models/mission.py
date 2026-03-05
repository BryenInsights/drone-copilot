"""Mission and MissionStatus Pydantic models."""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field


class MissionType(StrEnum):
    EXPLORE = "explore"
    INSPECT = "inspect"
    FREEFORM = "freeform"


class MissionStatus(StrEnum):
    IDLE = "idle"
    SEARCHING = "searching"
    APPROACHING = "approaching"
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

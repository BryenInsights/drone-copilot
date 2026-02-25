"""Mission and MissionStatus Pydantic models."""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field

from client.src.models.perception import ScanFrame


class MissionType(StrEnum):
    EXPLORE = "explore"
    INSPECT = "inspect"
    FREEFORM = "freeform"


class MissionStatus(StrEnum):
    IDLE = "idle"
    SCANNING = "scanning"
    ANALYZING = "analyzing"
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
    refined_label: str | None = None
    approach_step: int = Field(default=0, ge=0, le=15)
    max_approach_steps: int = 15
    started_at: float | None = None
    scan_frames: list[ScanFrame] = Field(default_factory=list, max_length=8)
    best_scan_index: int | None = Field(default=None, ge=0, le=7)

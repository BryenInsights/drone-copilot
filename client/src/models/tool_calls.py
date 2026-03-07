"""Tool call parameter Pydantic models for all Gemini Live API tools."""

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class MoveDirection(StrEnum):
    FORWARD = "forward"
    BACK = "back"
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"


class RotateDirection(StrEnum):
    CLOCKWISE = "clockwise"
    COUNTER_CLOCKWISE = "counter_clockwise"


class TakeoffParams(BaseModel):
    """No parameters needed for takeoff."""


class LandParams(BaseModel):
    """No parameters needed for landing."""


class HoverParams(BaseModel):
    """No parameters needed for hover/stop."""


class MoveDroneParams(BaseModel):
    """Parameters for move_drone tool call."""

    direction: MoveDirection
    distance_cm: int = Field(ge=20, le=200)

    @field_validator("distance_cm", mode="before")
    @classmethod
    def clamp_distance(cls, v: int) -> int:
        return max(20, min(200, int(v)))


class RotateDroneParams(BaseModel):
    """Parameters for rotate_drone tool call."""

    direction: RotateDirection
    degrees: int = Field(ge=10, le=360)

    @field_validator("degrees", mode="before")
    @classmethod
    def clamp_degrees(cls, v: int) -> int:
        return max(10, min(360, int(v)))


class SetSpeedParams(BaseModel):
    """Parameters for set_speed tool call."""

    speed_cm_per_sec: int = Field(ge=10, le=100)


class StartInspectionParams(BaseModel):
    """Parameters for start_inspection tool call."""

    target_description: str = Field(min_length=1)
    aspects: str | None = None
    needs_search: bool = False


class ReportPerceptionParams(BaseModel):
    """Parameters for report_perception tool call."""

    target_visible: bool
    horizontal_offset: float = Field(ge=-1.0, le=1.0)
    vertical_offset: float = Field(ge=-1.0, le=1.0)
    relative_size: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("horizontal_offset", "vertical_offset", mode="before")
    @classmethod
    def clamp_offset(cls, v: float) -> float:
        return max(-1.0, min(1.0, float(v)))

    @field_validator("relative_size", "confidence", mode="before")
    @classmethod
    def clamp_positive(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))



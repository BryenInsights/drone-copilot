"""PerceptionResult Pydantic model."""

from pydantic import BaseModel, Field, field_validator


class PerceptionResult(BaseModel):
    """AI's analysis of a video frame during autonomous missions."""

    target_visible: bool = False
    horizontal_offset: float = Field(default=0.0, ge=-1.0, le=1.0)
    vertical_offset: float = Field(default=0.0, ge=-1.0, le=1.0)
    relative_size: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("horizontal_offset", "vertical_offset", mode="before")
    @classmethod
    def clamp_offset(cls, v: float) -> float:
        return max(-1.0, min(1.0, float(v)))

    @field_validator("relative_size", "confidence", mode="before")
    @classmethod
    def clamp_positive(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))

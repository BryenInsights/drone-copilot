"""Client-side WebSocket message types per contracts/backend-websocket.md."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# ── Client → Backend ─────────────────────────────────────────────────────────


class AudioInMsg(BaseModel):
    """Microphone audio chunk sent to backend."""

    type: str = "audio_in"
    data: str  # base64-encoded PCM 16-bit 16kHz mono


class VideoFrameMsg(BaseModel):
    """Drone camera frame sent to backend."""

    type: str = "video_frame"
    data: str  # base64-encoded JPEG, 768px wide
    timestamp: float


class ToolResponseMsg(BaseModel):
    """Result of executing a tool call, sent to backend."""

    type: str = "tool_response"
    id: str
    name: str
    response: dict


# ── Backend → Client ─────────────────────────────────────────────────────────


class AudioOutMsg(BaseModel):
    """AI voice response chunk from backend."""

    type: str = "audio_out"
    data: str  # base64-encoded PCM 16-bit 24kHz mono


class ToolCallEntry(BaseModel):
    """A single tool call within a tool_call message."""

    id: str
    name: str
    args: dict
    validated: bool = False
    rejected_reason: str | None = None


class ToolCallMsg(BaseModel):
    """AI tool call request from backend. May contain multiple calls."""

    type: str = "tool_call"
    calls: list[ToolCallEntry]


class Speaker(StrEnum):
    USER = "user"
    COPILOT = "copilot"
    SYSTEM = "system"


class TranscriptMsg(BaseModel):
    """Audio transcription from backend."""

    type: str = "transcript"
    speaker: Speaker
    text: str
    timestamp: float


class TranscriptEntry(BaseModel):
    """A single utterance in the conversation for dashboard display."""

    speaker: Speaker
    text: str
    timestamp: float


class SessionStatusMsg(BaseModel):
    """Session lifecycle event from backend."""

    type: str = "session_status"
    status: str  # connecting, connected, reconnecting, disconnected, error
    metadata: dict = Field(default_factory=dict)


class InterruptedMsg(BaseModel):
    """AI speech was interrupted by user (barge-in)."""

    type: str = "interrupted"


class ErrorMsg(BaseModel):
    """Error notification."""

    type: str = "error"
    code: str
    message: str
    recoverable: bool = True

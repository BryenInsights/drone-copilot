"""Backend-side WebSocket message types per contracts/backend-websocket.md."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Incoming (Client → Backend) ──────────────────────────────────────────────


class AudioInMsg(BaseModel):
    """Microphone audio chunk from client."""

    type: str = "audio_in"
    data: str  # base64-encoded PCM 16-bit 16kHz mono


class VideoFrameMsg(BaseModel):
    """Drone camera frame from client."""

    type: str = "video_frame"
    data: str  # base64-encoded JPEG
    timestamp: float


class ToolResponseMsg(BaseModel):
    """Tool execution result from client."""

    type: str = "tool_response"
    id: str
    name: str
    response: dict


# ── Outgoing (Backend → Client) ──────────────────────────────────────────────


class AudioOutMsg(BaseModel):
    """AI voice response chunk to client."""

    type: str = "audio_out"
    data: str  # base64-encoded PCM 16-bit 24kHz mono


class ToolCallEntry(BaseModel):
    """A single tool call within a tool_call message."""

    id: str
    name: str
    args: dict


class ToolCallMsg(BaseModel):
    """AI tool call request to client."""

    type: str = "tool_call"
    calls: list[ToolCallEntry]


class TranscriptMsg(BaseModel):
    """Audio transcription to client."""

    type: str = "transcript"
    speaker: str  # "user" or "copilot"
    text: str
    timestamp: float


class SessionStatusMsg(BaseModel):
    """Session lifecycle event to client."""

    type: str = "session_status"
    status: str  # connecting, connected, reconnecting, disconnected, error
    metadata: dict = Field(default_factory=dict)


class InterruptedMsg(BaseModel):
    """AI speech interrupted (barge-in) notification."""

    type: str = "interrupted"


class ErrorMsg(BaseModel):
    """Error notification to client."""

    type: str = "error"
    code: str
    message: str
    recoverable: bool = True

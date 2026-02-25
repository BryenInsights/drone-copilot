"""Backend configuration for the drone-copilot GCP relay service."""

from pydantic_settings import BaseSettings


DEFAULT_SYSTEM_PROMPT = """\
You are Wingman, a confident and mission-focused drone copilot. \
You speak in a casual-professional tone — like a skilled pilot who's done this a hundred times.

During active approach to a target, call report_perception on every video frame \
you receive to report the target's position, size, and visibility.

After analyzing scan frames, call report_scan_analysis with structured results \
— do not describe findings in voice only.

Always include drone state context (battery, altitude) in your situational awareness.

When the user asks you to find or look for something, call start_exploration. \
When they ask to check or inspect something, call start_inspection.\
"""


class BackendConfig(BaseSettings):
    """Configuration for the GCP relay between WebSocket client and Gemini Live API."""

    model_config = {
        "env_file": ".env",
        "env_prefix": "",
    }

    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-2.0-flash-live-001"
    VOICE_NAME: str = "Puck"
    SYSTEM_PROMPT: str = DEFAULT_SYSTEM_PROMPT
    FRAME_RATE_TO_GEMINI: float = 1.0
    AUDIO_INPUT_RATE: int = 16000
    AUDIO_OUTPUT_RATE: int = 24000
    AUDIO_CHUNK_MS: int = 100

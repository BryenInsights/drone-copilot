"""Gemini Live API session manager with reconnection and session resumption."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import types

from backend.src.config import BackendConfig
from backend.src.models.tools import build_tool_declarations

logger = logging.getLogger(__name__)


class GeminiSession:
    """Manages a Gemini Live API session with resumption and GoAway handling.

    Wraps ``client.aio.live.connect()`` and exposes helpers for sending audio,
    video, tool responses, and text.  The ``receive()`` async iterator yields
    raw server messages and transparently tracks session-resumption handles and
    GoAway signals so the relay can reconnect when needed.
    """

    def __init__(self, config: BackendConfig) -> None:
        self._config = config
        if config.USE_VERTEX_AI:
            self._client = genai.Client(
                vertexai=True,
                project=config.GCP_PROJECT,
                location=config.GCP_LOCATION,
            )
        else:
            self._client = genai.Client(api_key=config.GEMINI_API_KEY)
        self._session: Any | None = None
        self._cm: Any | None = None
        self._session_handle: str | None = None
        self._go_away: bool = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _build_live_config(self) -> types.LiveConnectConfig:
        """Build the LiveConnectConfig, including resumption handle if available."""
        resumption_cfg = types.SessionResumptionConfig(
            handle=self._session_handle,
        )

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part.from_text(text=self._config.SYSTEM_PROMPT)],
            ),
            tools=build_tool_declarations(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._config.VOICE_NAME,
                    ),
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=40000,
                sliding_window=types.SlidingWindow(target_tokens=25000),
            ),
            session_resumption=resumption_cfg,
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    prefix_padding_ms=100,
                    silence_duration_ms=500,
                ),
            ),
        )

    async def connect(self) -> None:
        """Open (or re-open) the Gemini Live API session."""
        live_config = self._build_live_config()
        self._go_away = False
        logger.info(
            "Connecting to Gemini Live API (model=%s, resumption=%s)",
            self._config.GEMINI_MODEL,
            self._session_handle is not None,
        )
        self._cm = self._client.aio.live.connect(
            model=self._config.GEMINI_MODEL,
            config=live_config,
        )
        self._session = await self._cm.__aenter__()
        logger.info("Gemini session connected")

    async def close(self) -> None:
        """Gracefully close the current session, if open."""
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                logger.debug("Ignoring error during session close", exc_info=True)
            finally:
                self._session = None
                self._cm = None
            logger.info("Gemini session closed")

    @property
    def go_away(self) -> bool:
        """Whether a GoAway message has been received, signalling reconnect."""
        return self._go_away

    @property
    def session_handle(self) -> str | None:
        """Most recent session-resumption handle, if any."""
        return self._session_handle

    # ------------------------------------------------------------------
    # Sending data
    # ------------------------------------------------------------------

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send a PCM audio chunk to the session."""
        if self._session is None:
            logger.warning("send_audio: session is None, dropping data")
            return
        await self._session.send_realtime_input(
            audio=types.Blob(
                data=pcm_bytes,
                mime_type=f"audio/pcm;rate={self._config.AUDIO_INPUT_RATE}",
            ),
        )

    async def send_video(self, jpeg_bytes: bytes) -> None:
        """Send a JPEG video frame to the session."""
        if self._session is None:
            logger.warning("send_video: session is None, dropping frame")
            return
        await self._session.send_realtime_input(
            video=types.Blob(data=jpeg_bytes, mime_type="image/jpeg"),
        )

    async def send_tool_response(
        self,
        id: str,  # noqa: A002
        name: str,
        result: dict,
        scheduling: str | None = None,
    ) -> None:
        """Send a function-call response back to Gemini."""
        if self._session is None:
            logger.warning("send_tool_response: session is None, dropping response for %s", name)
            return

        sched_enum = None
        if scheduling is not None:
            try:
                sched_enum = types.FunctionResponseScheduling(scheduling)
            except ValueError:
                logger.warning("Unknown scheduling value: %s", scheduling)

        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(
                    id=id,
                    name=name,
                    response=result,
                    scheduling=sched_enum,
                ),
            ],
        )

    async def send_text(self, text: str) -> None:
        """Inject a text message into the session via ``send_client_content``."""
        if self._session is None:
            logger.warning("send_text: session is None, dropping text")
            return
        await self._session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part.from_text(text=text)],
            ),
            turn_complete=True,
        )

    async def send_frames_with_prompt(
        self, frames_jpeg: list[bytes], prompt: str
    ) -> None:
        """Send multiple JPEG frames with a text prompt via ``send_client_content``.

        Used to send captured frames (e.g. inspection angles) for Gemini
        to analyze and provide a verbal assessment.

        NOTE: Currently unused — inspection reports are generated via the Flash
        API (VisualPerceptionClient). Reserved for future Live API verbal analysis.
        """
        if self._session is None:
            logger.warning(
                "send_frames_with_prompt: session is None, dropping %d frames",
                len(frames_jpeg),
            )
            return
        parts: list[types.Part] = []
        for i, jpeg in enumerate(frames_jpeg):
            parts.append(types.Part.from_text(text=f"[Scan frame {i}]"))
            parts.append(
                types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")
            )
        parts.append(types.Part.from_text(text=prompt))
        await self._session.send_client_content(
            turns=types.Content(role="user", parts=parts),
            turn_complete=True,
        )

    # ------------------------------------------------------------------
    # Receiving data
    # ------------------------------------------------------------------

    async def receive(self) -> AsyncIterator:
        """Async iterator that yields server messages.

        Transparently tracks session-resumption handles and sets the
        ``go_away`` flag when a GoAway message arrives so the relay can
        trigger reconnection.
        """
        if self._session is None:
            return

        async for message in self._session.receive():
            # Track session resumption handles
            if message.session_resumption_update:
                update = message.session_resumption_update
                if update.resumable and update.new_handle:
                    self._session_handle = update.new_handle
                    logger.debug(
                        "Session resumption handle updated: %s...",
                        self._session_handle[:20],
                    )

            # Detect GoAway — the server is asking us to reconnect
            if message.go_away:
                self._go_away = True
                logger.warning("Received GoAway from Gemini — reconnection required")

            yield message

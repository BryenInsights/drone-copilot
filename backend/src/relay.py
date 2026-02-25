"""WebSocket relay between the browser/client and Gemini Live API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from backend.src.config import BackendConfig
from backend.src.gemini_session import GeminiSession
from backend.src.models.messages import (
    AudioOutMsg,
    ErrorMsg,
    InterruptedMsg,
    SessionStatusMsg,
    ToolCallEntry,
    ToolCallMsg,
    TranscriptMsg,
)

logger = logging.getLogger(__name__)

MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY_S = 1.0


class Relay:
    """Bidirectional relay between a client WebSocket and a GeminiSession.

    * **Send loop** -- reads JSON messages from the client WebSocket and
      forwards audio, video, tool responses, and text to the Gemini session.
    * **Receive loop** -- iterates the Gemini session's async stream and
      dispatches audio, tool calls, transcriptions, and interruptions back
      to the client WebSocket.

    Both loops run concurrently via ``asyncio.gather`` inside ``run()``.
    """

    def __init__(
        self, ws: WebSocket, session: GeminiSession, config: BackendConfig
    ) -> None:
        self._ws = ws
        self._session = session
        self._config = config
        self._running = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to Gemini and run the send/receive loops concurrently."""
        self._running = True
        await self._connect_session()

        try:
            await asyncio.gather(
                self._send_loop(),
                self._receive_loop(),
            )
        finally:
            self._running = False

    # ------------------------------------------------------------------
    # Connection / reconnection helpers
    # ------------------------------------------------------------------

    async def _connect_session(self) -> None:
        """Open (or re-open) the Gemini session and notify the client."""
        await self._ws_send(SessionStatusMsg(status="connecting"))
        await self._session.connect()
        await self._ws_send(SessionStatusMsg(status="connected"))

    async def _reconnect(self) -> None:
        """Attempt to reconnect to Gemini with back-off retries."""
        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            logger.info(
                "Reconnection attempt %d/%d (handle=%s)",
                attempt,
                MAX_RECONNECT_ATTEMPTS,
                self._session.session_handle is not None,
            )
            await self._ws_send(
                SessionStatusMsg(
                    status="reconnecting",
                    metadata={"attempt": attempt},
                )
            )
            try:
                await self._session.close()
                await self._session.connect()
                await self._ws_send(SessionStatusMsg(status="connected"))
                logger.info("Reconnection succeeded on attempt %d", attempt)
                return
            except Exception:
                logger.exception("Reconnection attempt %d failed", attempt)
                if attempt < MAX_RECONNECT_ATTEMPTS:
                    await asyncio.sleep(RECONNECT_DELAY_S * attempt)

        # All retries exhausted
        await self._ws_send(
            ErrorMsg(
                code="reconnect_failed",
                message="Failed to reconnect to Gemini after multiple attempts",
                recoverable=False,
            )
        )
        self._running = False
        raise RuntimeError("Gemini reconnection failed")

    # ------------------------------------------------------------------
    # Send loop: client WS -> Gemini
    # ------------------------------------------------------------------

    async def _send_loop(self) -> None:
        """Read JSON messages from the client WebSocket and forward to Gemini."""
        while self._running:
            try:
                raw = await self._ws.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "audio_in":
                    pcm_bytes = base64.b64decode(msg["data"])
                    await self._session.send_audio(pcm_bytes)

                elif msg_type == "video_frame":
                    jpeg_bytes = base64.b64decode(msg["data"])
                    await self._session.send_video(jpeg_bytes)

                elif msg_type == "tool_response":
                    await self._session.send_tool_response(
                        id=msg["id"],
                        name=msg["name"],
                        result=msg["response"],
                    )

                elif msg_type == "text":
                    await self._session.send_text(msg.get("text", ""))

                elif msg_type == "scan_frames":
                    frames_b64: list[str] = msg.get("frames", [])
                    frames_bytes = [base64.b64decode(f) for f in frames_b64]
                    prompt = msg.get("prompt", "")
                    await self._session.send_scan_frames(frames_bytes, prompt)

                else:
                    logger.warning("Unknown client message type: %s", msg_type)

            except WebSocketDisconnect:
                logger.info("Client WebSocket disconnected in send loop")
                self._running = False
                raise
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message from client")
            except Exception:
                logger.exception("Error in send loop")
                await self._ws_send(
                    ErrorMsg(
                        code="send_error",
                        message="Error processing client message",
                        recoverable=True,
                    )
                )

    # ------------------------------------------------------------------
    # Receive loop: Gemini -> client WS
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Iterate Gemini server messages and dispatch to the client WS."""
        while self._running:
            try:
                async for message in self._session.receive():
                    if not self._running:
                        break

                    # --- Audio data ---
                    if message.data:
                        audio_b64 = base64.b64encode(message.data).decode("ascii")
                        await self._ws_send(AudioOutMsg(data=audio_b64))

                    # --- Tool calls ---
                    if message.tool_call and message.tool_call.function_calls:
                        entries = [
                            ToolCallEntry(
                                id=fc.id,
                                name=fc.name,
                                args=dict(fc.args) if fc.args else {},
                            )
                            for fc in message.tool_call.function_calls
                        ]
                        await self._ws_send(ToolCallMsg(calls=entries))

                    # --- Transcriptions and interruption ---
                    if message.server_content:
                        sc = message.server_content

                        if sc.input_transcription and sc.input_transcription.text:
                            await self._ws_send(
                                TranscriptMsg(
                                    speaker="user",
                                    text=sc.input_transcription.text,
                                    timestamp=time.time(),
                                )
                            )

                        if sc.output_transcription and sc.output_transcription.text:
                            await self._ws_send(
                                TranscriptMsg(
                                    speaker="copilot",
                                    text=sc.output_transcription.text,
                                    timestamp=time.time(),
                                )
                            )

                        if sc.interrupted:
                            await self._ws_send(InterruptedMsg())

                    # --- GoAway → reconnect ---
                    if message.go_away:
                        logger.warning("GoAway received — initiating reconnection")
                        await self._reconnect()
                        break  # restart the receive loop with the new session

                # If we exit the async-for naturally (session ended) while
                # still running, attempt reconnection.
                if self._running and not self._session.go_away:
                    logger.warning("Gemini stream ended unexpectedly — reconnecting")
                    await self._reconnect()

            except WebSocketDisconnect:
                logger.info("Client WebSocket disconnected in receive loop")
                self._running = False
                raise
            except RuntimeError:
                # Raised by _reconnect when all retries are exhausted
                self._running = False
                raise
            except Exception:
                logger.exception("Error in receive loop")
                await self._ws_send(
                    ErrorMsg(
                        code="receive_error",
                        message="Error receiving from Gemini",
                        recoverable=True,
                    )
                )
                # Try to reconnect after an unexpected error
                if self._running:
                    try:
                        await self._reconnect()
                    except RuntimeError:
                        self._running = False
                        raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ws_send(self, msg) -> None:
        """Serialize a Pydantic model and send it over the client WebSocket."""
        try:
            await self._ws.send_text(msg.model_dump_json())
        except Exception:
            logger.debug("Failed to send message to client WS", exc_info=True)

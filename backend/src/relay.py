"""WebSocket relay between the browser/client and Gemini Live API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import errors as genai_errors

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
MAX_RECONNECT_CYCLES = 5
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

        # Context tracking for post-reconnect injection
        self._last_user_text: str | None = None
        self._last_tool_calls: list[str] = []
        self._active_task: str | None = None

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
                await self._inject_reconnect_context()
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

    async def _inject_reconnect_context(self) -> None:
        """Inject a brief context summary so Gemini can continue the task."""
        parts = ["[SYSTEM] Session was interrupted and reconnected."]
        if self._active_task:
            parts.append(f"Active task before interruption: {self._active_task}")
        if self._last_tool_calls:
            recent = ", ".join(self._last_tool_calls[-5:])
            parts.append(f"Recent tool calls: {recent}")
        if self._last_user_text:
            parts.append(f"Last user request: {self._last_user_text}")
        parts.append("Continue from where you left off.")
        context = " ".join(parts)
        try:
            await self._session.send_text(context)
            logger.info("Injected reconnect context: %s", context[:100])
        except Exception:
            logger.warning("Failed to inject reconnect context", exc_info=True)

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
                    tool_id = msg.get("id") or ""
                    await self._session.send_tool_response(
                        id=tool_id,
                        name=msg.get("name", ""),
                        result=msg.get("response", {}),
                        scheduling=msg.get("scheduling"),
                    )

                elif msg_type == "text":
                    await self._session.send_text(msg.get("text", ""))

                elif msg_type == "frames_with_prompt":
                    frames_b64: list[str] = msg.get("frames", [])
                    frames_bytes = [base64.b64decode(f) for f in frames_b64]
                    prompt = msg.get("prompt", "")
                    await self._session.send_frames_with_prompt(frames_bytes, prompt)

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
        reconnect_cycles = 0
        consecutive_empties = 0
        while self._running:
            try:
                got_message = False
                async for message in self._session.receive():
                    if not self._running:
                        break
                    got_message = True

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
                        # Track tool calls for reconnect context
                        self._last_tool_calls.extend(
                            fc.name for fc in message.tool_call.function_calls
                        )
                        self._last_tool_calls = self._last_tool_calls[-5:]

                    # --- Transcriptions and interruption ---
                    if message.server_content:
                        sc = message.server_content

                        if sc.input_transcription and sc.input_transcription.text:
                            self._last_user_text = sc.input_transcription.text
                            await self._ws_send(
                                TranscriptMsg(
                                    speaker="user",
                                    text=sc.input_transcription.text,
                                    timestamp=time.time(),
                                )
                            )

                        if sc.output_transcription and sc.output_transcription.text:
                            out_text = sc.output_transcription.text
                            # Track task-like phrases for reconnect context
                            lower = out_text.lower()
                            for kw in ("search for", "find", "inspect", "looking for"):
                                if kw in lower:
                                    self._active_task = out_text.strip()
                                    break
                            await self._ws_send(
                                TranscriptMsg(
                                    speaker="copilot",
                                    text=out_text,
                                    timestamp=time.time(),
                                )
                            )

                        if sc.interrupted:
                            await self._ws_send(InterruptedMsg())

                    # --- GoAway → reconnect ---
                    if message.go_away:
                        logger.warning("GoAway received — initiating reconnection")
                        reconnect_cycles = 0  # GoAway is expected; reset counter
                        await self._reconnect()
                        break  # restart the receive loop with the new session

                # Reset cycle counter if we got real messages
                if got_message:
                    reconnect_cycles = 0
                    consecutive_empties = 0
                    continue  # Turn completed normally; re-enter receive() for next turn

                # receive() returned with no messages. This can happen
                # normally with thinking-model responses (text/thought
                # parts that the SDK doesn't surface as data).  Only
                # reconnect after many consecutive empties.
                consecutive_empties += 1
                if consecutive_empties <= MAX_RECONNECT_CYCLES:
                    if consecutive_empties > 1:
                        logger.debug(
                            "Empty receive turn %d — retrying",
                            consecutive_empties,
                        )
                    await asyncio.sleep(0.2)
                    continue  # Re-enter receive(); session is likely fine

                # Truly stuck — attempt reconnection.
                if self._running and not self._session.go_away:
                    reconnect_cycles += 1
                    consecutive_empties = 0
                    if reconnect_cycles > MAX_RECONNECT_CYCLES:
                        logger.error(
                            "Gemini stream ended immediately %d times — giving up",
                            reconnect_cycles,
                        )
                        await self._ws_send(
                            ErrorMsg(
                                code="reconnect_storm",
                                message="Session keeps closing immediately; stopping reconnect",
                                recoverable=False,
                            )
                        )
                        self._running = False
                        return
                    delay = RECONNECT_DELAY_S * (2 ** (reconnect_cycles - 1))
                    logger.warning(
                        "Gemini stream ended unexpectedly (cycle %d/%d) "
                        "— reconnecting in %.1fs",
                        reconnect_cycles,
                        MAX_RECONNECT_CYCLES,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    await self._reconnect()

            except WebSocketDisconnect:
                logger.info("Client WebSocket disconnected in receive loop")
                self._running = False
                raise
            except RuntimeError:
                # Raised by _reconnect when all retries are exhausted
                self._running = False
                raise
            except genai_errors.APIError as e:
                # Code 1000 = clean WebSocket close (client disconnected)
                if e.code == 1000:
                    logger.info("Gemini session closed cleanly (1000)")
                    self._running = False
                    return
                logger.exception("Gemini API error in receive loop")
                # Fall through to reconnect for other API errors
                if self._running:
                    reconnect_cycles += 1
                    if reconnect_cycles > MAX_RECONNECT_CYCLES:
                        self._running = False
                        return
                    delay = RECONNECT_DELAY_S * (2 ** (reconnect_cycles - 1))
                    await asyncio.sleep(delay)
                    try:
                        await self._reconnect()
                    except RuntimeError:
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
                # Try to reconnect after an unexpected error with backoff
                if self._running:
                    reconnect_cycles += 1
                    if reconnect_cycles > MAX_RECONNECT_CYCLES:
                        self._running = False
                        return
                    delay = RECONNECT_DELAY_S * (2 ** (reconnect_cycles - 1))
                    await asyncio.sleep(delay)
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

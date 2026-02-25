"""WebSocket client for GCP backend relay."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from client.src.config import ClientConfig

logger = logging.getLogger(__name__)


class BackendClient:
    """WebSocket client connecting to the GCP backend relay.

    Sends audio, video, and tool responses to the backend.
    Receives audio, tool calls, transcripts, and status events.
    Auto-reconnects with exponential backoff on connection loss.
    """

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        self._ws: ClientConnection | None = None
        self._connected = False
        self._handlers: dict[str, Callable] = {}
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._should_run = True

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Handler Registration ────────────────────────────────────────────

    def on_audio_out(self, handler: Callable[[bytes], Any]) -> None:
        self._handlers["audio_out"] = handler

    def on_tool_call(self, handler: Callable[[list[dict]], Any]) -> None:
        self._handlers["tool_call"] = handler

    def on_transcript(self, handler: Callable[[str, str, float], Any]) -> None:
        self._handlers["transcript"] = handler

    def on_interrupted(self, handler: Callable[[], Any]) -> None:
        self._handlers["interrupted"] = handler

    def on_session_status(self, handler: Callable[[str, dict], Any]) -> None:
        self._handlers["session_status"] = handler

    def on_error(self, handler: Callable[[str, str, bool], Any]) -> None:
        self._handlers["error"] = handler

    # ── Send Methods ────────────────────────────────────────────────────

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send microphone audio to backend."""
        if not self._connected or self._ws is None:
            return
        msg = {
            "type": "audio_in",
            "data": base64.b64encode(pcm_bytes).decode("ascii"),
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            logger.warning("Failed to send audio", exc_info=True)

    async def send_video(self, jpeg_base64: str, timestamp: float | None = None) -> None:
        """Send drone camera frame to backend."""
        if not self._connected or self._ws is None:
            return
        msg = {
            "type": "video_frame",
            "data": jpeg_base64,
            "timestamp": timestamp or time.time(),
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            logger.warning("Failed to send video frame", exc_info=True)

    async def send_tool_response(self, tool_id: str, name: str, response: dict) -> None:
        """Send tool execution result to backend."""
        if not self._connected or self._ws is None:
            return
        msg = {
            "type": "tool_response",
            "id": tool_id,
            "name": name,
            "response": response,
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            logger.warning("Failed to send tool response", exc_info=True)

    async def send_text(self, text: str) -> None:
        """Inject text into the Gemini session via the backend relay."""
        if not self._connected or self._ws is None:
            return
        msg = {"type": "text", "text": text}
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            logger.warning("Failed to send text", exc_info=True)

    async def send_scan_frames(
        self, frames_jpeg: list[bytes], prompt: str
    ) -> None:
        """Send scan frames with a prompt for Gemini analysis."""
        if not self._connected or self._ws is None:
            return
        frames_b64 = [base64.b64encode(f).decode("ascii") for f in frames_jpeg]
        msg = {
            "type": "scan_frames",
            "frames": frames_b64,
            "prompt": prompt,
        }
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            logger.warning("Failed to send scan frames", exc_info=True)

    # ── Connection Management ───────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to backend WebSocket with auto-reconnect."""
        self._should_run = True
        while self._should_run:
            try:
                logger.info("Connecting to backend: %s", self._config.BACKEND_URL)
                self._ws = await websockets.connect(self._config.BACKEND_URL)
                self._connected = True
                self._reconnect_delay = 1.0
                logger.info("Connected to backend")
                return
            except Exception as e:
                logger.warning(
                    "Backend connection failed: %s. Retrying in %.1fs",
                    e,
                    self._reconnect_delay,
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def receive_loop(self) -> None:
        """Receive and dispatch messages from backend. Reconnects on failure."""
        while self._should_run:
            try:
                if not self._connected or self._ws is None:
                    await self.connect()

                async for raw in self._ws:
                    try:
                        msg = json.loads(raw)
                        await self._dispatch(msg)
                    except json.JSONDecodeError:
                        logger.warning("Received non-JSON message from backend")

            except websockets.ConnectionClosed:
                logger.warning("Backend connection closed")
                self._connected = False
                if self._should_run:
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2, self._max_reconnect_delay
                    )
                    await self.connect()

            except Exception:
                logger.exception("Backend receive error")
                self._connected = False
                if self._should_run:
                    await asyncio.sleep(self._reconnect_delay)
                    await self.connect()

    async def _dispatch(self, msg: dict) -> None:
        """Route incoming message to registered handler."""
        msg_type = msg.get("type")

        if msg_type == "audio_out" and "audio_out" in self._handlers:
            pcm_bytes = base64.b64decode(msg["data"])
            handler = self._handlers["audio_out"]
            result = handler(pcm_bytes)
            if asyncio.iscoroutine(result):
                await result

        elif msg_type == "tool_call" and "tool_call" in self._handlers:
            handler = self._handlers["tool_call"]
            result = handler(msg.get("calls", []))
            if asyncio.iscoroutine(result):
                await result

        elif msg_type == "transcript" and "transcript" in self._handlers:
            handler = self._handlers["transcript"]
            result = handler(
                msg.get("speaker", "system"),
                msg.get("text", ""),
                msg.get("timestamp", time.time()),
            )
            if asyncio.iscoroutine(result):
                await result

        elif msg_type == "interrupted" and "interrupted" in self._handlers:
            handler = self._handlers["interrupted"]
            result = handler()
            if asyncio.iscoroutine(result):
                await result

        elif msg_type == "session_status" and "session_status" in self._handlers:
            handler = self._handlers["session_status"]
            result = handler(msg.get("status", ""), msg.get("metadata", {}))
            if asyncio.iscoroutine(result):
                await result

        elif msg_type == "error" and "error" in self._handlers:
            handler = self._handlers["error"]
            result = handler(
                msg.get("code", "unknown"),
                msg.get("message", ""),
                msg.get("recoverable", True),
            )
            if asyncio.iscoroutine(result):
                await result

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._should_run = False
        self._connected = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        logger.info("Backend client closed")

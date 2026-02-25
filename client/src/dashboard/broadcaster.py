"""DashboardBroadcaster — single broadcast point for all dashboard messages.

Both live and demo modes feed through the same broadcast_json() method.
Bridges synchronous drone control threads to async dashboard WebSocket
using fire-and-forget via asyncio.run_coroutine_threadsafe().
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and message broadcasting."""

    def __init__(self) -> None:
        self._connections: set[Any] = set()
        self._last_status: str | None = None  # Cached for late-joiner replay

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: Any) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self._connections.add(websocket)
        logger.info("Dashboard client connected (total=%d)", len(self._connections))

        # Replay last status for late joiners
        if self._last_status is not None:
            try:
                await websocket.send_text(self._last_status)
            except Exception:
                logger.debug("Failed to replay status to new client", exc_info=True)

    def disconnect(self, websocket: Any) -> None:
        """Remove a WebSocket connection."""
        self._connections.discard(websocket)
        logger.info("Dashboard client disconnected (total=%d)", len(self._connections))

    async def broadcast_json(self, message: dict) -> None:
        """Broadcast a JSON message to all connected clients.

        Also caches status messages for late-joiner replay and hooks into
        the recorder if one is attached.
        """
        msg_type = message.get("type")

        # Cache status messages for late-joiner replay
        if msg_type == "status":
            self._last_status = json.dumps(message)

        text = json.dumps(message)
        dead: list[Any] = []

        for ws in self._connections:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self._connections.discard(ws)


class DashboardBroadcaster:
    """Single broadcast point for all dashboard message types.

    Both live and demo modes feed through broadcast_json(). Provides typed
    convenience methods for each message type. Supports fire-and-forget
    sync-to-async bridge for mission threads.
    """

    def __init__(self, manager: ConnectionManager) -> None:
        self._manager = manager
        self._loop: asyncio.AbstractEventLoop | None = None
        self._recorder: Any = None  # DemoRecorder if attached

    @property
    def manager(self) -> ConnectionManager:
        return self._manager

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the event loop for sync-to-async bridging."""
        self._loop = loop

    def set_recorder(self, recorder: Any) -> None:
        """Attach a DemoRecorder for transparent recording."""
        self._recorder = recorder

    async def broadcast_json(self, message: dict) -> None:
        """Broadcast to all clients and optionally record."""
        # Record if a recorder is attached
        if self._recorder is not None:
            try:
                self._recorder.record(message)
            except Exception:
                logger.debug("Recorder error", exc_info=True)

        await self._manager.broadcast_json(message)

    def _broadcast_fire_and_forget(self, message: dict) -> None:
        """Fire-and-forget broadcast from sync thread."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.broadcast_json(message), self._loop
            )
        except RuntimeError:
            pass  # Loop closed

    # -- Typed broadcast methods --

    async def broadcast_frame(self, jpeg_base64: str) -> None:
        """Broadcast a video frame."""
        await self.broadcast_json({
            "type": "frame",
            "data": jpeg_base64,
            "timestamp": time.time(),
        })

    async def broadcast_telemetry(self, data: dict) -> None:
        """Broadcast drone telemetry."""
        await self.broadcast_json({
            "type": "telemetry",
            "data": data,
            "timestamp": time.time(),
        })

    async def broadcast_status(self, data: dict) -> None:
        """Broadcast mission status update."""
        await self.broadcast_json({
            "type": "status",
            "data": data,
            "timestamp": time.time(),
        })

    async def broadcast_perception(self, data: dict) -> None:
        """Broadcast perception/object detection data."""
        await self.broadcast_json({
            "type": "perception",
            "data": data,
            "timestamp": time.time(),
        })

    async def broadcast_log(self, level: str, message: str) -> None:
        """Broadcast a log entry."""
        await self.broadcast_json({
            "type": "log",
            "data": {"level": level, "message": message},
            "timestamp": time.time(),
        })

    async def broadcast_ai_activity(self, data: dict) -> None:
        """Broadcast AI call lifecycle event."""
        await self.broadcast_json({
            "type": "ai_activity",
            "data": data,
            "timestamp": time.time(),
        })

    async def broadcast_ai_result(self, data: dict) -> None:
        """Broadcast structured AI output."""
        await self.broadcast_json({
            "type": "ai_result",
            "data": data,
            "timestamp": time.time(),
        })

    async def broadcast_report_data(self, data: dict) -> None:
        """Broadcast final mission report data."""
        await self.broadcast_json({
            "type": "report_data",
            "data": data,
            "timestamp": time.time(),
        })

    # -- Sync wrappers for mission threads --

    def send_status_sync(self, data: dict) -> None:
        """Fire-and-forget status broadcast from sync thread."""
        self._broadcast_fire_and_forget({
            "type": "status",
            "data": data,
            "timestamp": time.time(),
        })

    def send_log_sync(self, level: str, message: str) -> None:
        """Fire-and-forget log broadcast from sync thread."""
        self._broadcast_fire_and_forget({
            "type": "log",
            "data": {"level": level, "message": message},
            "timestamp": time.time(),
        })

    def send_perception_sync(self, data: dict) -> None:
        """Fire-and-forget perception broadcast from sync thread."""
        self._broadcast_fire_and_forget({
            "type": "perception",
            "data": data,
            "timestamp": time.time(),
        })

    def send_ai_activity_sync(self, data: dict) -> None:
        """Fire-and-forget AI activity broadcast from sync thread."""
        self._broadcast_fire_and_forget({
            "type": "ai_activity",
            "data": data,
            "timestamp": time.time(),
        })

    def send_ai_result_sync(self, data: dict) -> None:
        """Fire-and-forget AI result broadcast from sync thread."""
        self._broadcast_fire_and_forget({
            "type": "ai_result",
            "data": data,
            "timestamp": time.time(),
        })

    def send_frame_sync(self, jpeg_base64: str) -> None:
        """Fire-and-forget frame broadcast from sync thread."""
        self._broadcast_fire_and_forget({
            "type": "frame",
            "data": jpeg_base64,
            "timestamp": time.time(),
        })

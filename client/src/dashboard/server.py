"""Dashboard FastAPI server with WebSocket and REST endpoints.

Serves the web dashboard, streams video frames and telemetry to connected
browsers, and provides REST endpoints for health and demo info.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from client.src.dashboard.broadcaster import DashboardBroadcaster

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_dashboard_app(
    broadcaster: DashboardBroadcaster,
    frame_adapter: Any = None,
    telemetry_adapter: Any = None,
    demo_mode: bool = False,
    demo_info: list[dict] | None = None,
    backend_ws_url: str = "ws://localhost:8080/ws",
) -> FastAPI:
    """Create the dashboard FastAPI application.

    Args:
        broadcaster: The shared DashboardBroadcaster instance.
        frame_adapter: Callable returning (jpeg_bytes | None). Used by frame streaming task.
        telemetry_adapter: Callable returning telemetry dict. Used by telemetry streaming task.
        demo_mode: If True, disables live frame/telemetry streaming tasks.
        demo_info: List of demo recordings metadata for /api/demo-info endpoint.
    """
    _streaming_active = not demo_mode
    _demo_mode = demo_mode
    _demo_info = demo_info or []

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start background streaming tasks at server startup."""
        tasks: list[asyncio.Task] = []

        if _streaming_active and frame_adapter is not None:
            tasks.append(
                asyncio.create_task(
                    _frame_streaming_loop(broadcaster, frame_adapter),
                    name="frame_streaming",
                )
            )

        if _streaming_active and telemetry_adapter is not None:
            tasks.append(
                asyncio.create_task(
                    _telemetry_streaming_loop(broadcaster, telemetry_adapter),
                    name="telemetry_streaming",
                )
            )

        logger.info(
            "Dashboard server started (demo_mode=%s, bg_tasks=%d)",
            _demo_mode, len(tasks),
        )
        yield

        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info("Dashboard server stopped")

    app = FastAPI(title="Drone Copilot Dashboard", lifespan=lifespan)

    # Serve static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- REST Endpoints ---

    @app.get("/")
    async def index():
        """Serve the dashboard HTML."""
        index_path = STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return JSONResponse({"error": "index.html not found"}, status_code=404)

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {
            "connections": broadcaster.manager.connection_count,
            "streaming": _streaming_active,
            "demo_mode": _demo_mode,
        }

    @app.get("/api/demo-info")
    async def demo_info_endpoint():
        """Return demo mode flag and available recordings."""
        return {
            "demo_mode": _demo_mode,
            "demos": _demo_info,
        }

    @app.get("/api/backend-url")
    async def backend_url_endpoint():
        """Return the backend WebSocket URL for browser voice chat."""
        return {"url": backend_ws_url}

    # --- WebSocket Endpoint ---

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """Dashboard WebSocket — broadcasts all data to connected browsers."""
        await broadcaster.manager.connect(websocket)
        try:
            while True:
                # Dashboard is mostly read-only but accept commands for demo mode
                data = await websocket.receive_text()
                try:
                    import json
                    msg = json.loads(data)
                    msg_type = msg.get("type")
                    if msg_type == "command":
                        # Forward commands to a handler if registered
                        if hasattr(app.state, "command_handler"):
                            await app.state.command_handler(msg)
                except Exception:
                    logger.debug("Invalid WebSocket message from client", exc_info=True)
        except WebSocketDisconnect:
            broadcaster.manager.disconnect(websocket)
        except Exception:
            broadcaster.manager.disconnect(websocket)

    return app


async def _frame_streaming_loop(
    broadcaster: DashboardBroadcaster,
    frame_adapter: Any,
) -> None:
    """Stream video frames at ~10 FPS."""
    while True:
        try:
            jpeg_bytes = frame_adapter()
            if jpeg_bytes is not None:
                b64 = base64.b64encode(jpeg_bytes).decode("ascii")
                await broadcaster.broadcast_frame(b64)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.debug("Frame streaming error", exc_info=True)
        await asyncio.sleep(0.1)  # ~10 FPS


async def _telemetry_streaming_loop(
    broadcaster: DashboardBroadcaster,
    telemetry_adapter: Any,
) -> None:
    """Stream telemetry data at 1 Hz."""
    while True:
        try:
            telemetry = telemetry_adapter()
            if telemetry is not None:
                await broadcaster.broadcast_telemetry(telemetry)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.debug("Telemetry streaming error", exc_info=True)
        await asyncio.sleep(1.0)

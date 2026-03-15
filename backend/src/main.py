"""FastAPI application with WebSocket endpoint for drone copilot relay."""

import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.src.config import BackendConfig
from backend.src.gemini_session import GeminiSession
from backend.src.relay import Relay

logger = logging.getLogger(__name__)

app = FastAPI(title="Drone Copilot Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

config = BackendConfig()


@app.get("/health")
async def health():
    """Health check endpoint for load balancer / Cloud Run."""
    return {"status": "healthy"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Main WebSocket endpoint that relays between the client and Gemini Live API."""
    await ws.accept()
    logger.info("Client WebSocket connected")

    session = GeminiSession(config)
    relay = Relay(ws, session, config)

    try:
        await relay.run()
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception:
        logger.exception("WebSocket session error")
    finally:
        await session.close()
        logger.info("Session cleaned up")

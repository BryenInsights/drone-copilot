"""Client entry point — orchestrates drone, audio, video, backend, and dashboard."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
import time

import uvicorn

from client.src.config import ClientConfig, setup_logging

logger = logging.getLogger(__name__)


def _create_drone(config: ClientConfig):
    """Create real Tello or MockDrone based on config."""
    if config.USE_MOCK_DRONE:
        from client.src.drone.mock_drone import MockDrone

        logger.info("Using MockDrone")
        return MockDrone()
    else:
        from djitellopy import Tello

        logger.info(
            "Connecting to real Tello drone (retry_count=%d)",
            config.DJITELLOPY_RETRY_COUNT,
        )
        drone = Tello()
        drone.RETRY_COUNT = config.DJITELLOPY_RETRY_COUNT
        return drone


async def _audio_send_loop(
    audio_capture,
    backend_client,
) -> None:
    """Continuously send captured audio to backend."""
    while True:
        try:
            pcm_bytes = await audio_capture.queue.get()
            await backend_client.send_audio(pcm_bytes)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("Audio send error", exc_info=True)


async def _video_send_loop(
    frame_streamer,
    backend_client,
) -> None:
    """Send video frames to backend at configured rate (~1 FPS)."""
    while True:
        try:
            frame_b64 = frame_streamer.get_perception_frame()
            if frame_b64 is not None:
                await backend_client.send_video(frame_b64, time.time())
            await asyncio.sleep(0.1)  # Check 10x/sec, rate limiting is in streamer
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("Video send error", exc_info=True)


async def main() -> None:
    config = ClientConfig()
    setup_logging(config)

    logger.info("=== Drone Copilot Client Starting ===")

    # Initialize drone
    drone = _create_drone(config)
    try:
        drone.connect()
        logger.info("Drone connected, battery=%d%%", drone.get_battery())
    except Exception:
        logger.exception("Failed to connect to drone")
        sys.exit(1)

    # Import components
    from client.src.audio.capture import AudioCapture
    from client.src.audio.playback import AudioPlayback
    from client.src.backend_client import BackendClient
    from client.src.dashboard.broadcaster import ConnectionManager, DashboardBroadcaster
    from client.src.dashboard.server import create_dashboard_app
    from client.src.drone.controller import DroneController
    from client.src.error_handler import ErrorHandler
    from client.src.tool_handler import ToolHandler
    from client.src.video.frame_capture import FrameCapture
    from client.src.video.frame_streamer import FrameStreamer

    # Create components
    controller = DroneController(drone, config)
    ErrorHandler(controller)  # Registers controller for error recovery
    backend_client = BackendClient(config)
    audio_capture = AudioCapture(sample_rate=16000)
    audio_playback = AudioPlayback(sample_rate=24000)
    frame_capture = FrameCapture(drone, config)
    frame_streamer = FrameStreamer(frame_capture, config)
    tool_handler = ToolHandler(controller, backend_client, frame_streamer)

    # Set event loop on tool handler for mission thread async calls
    loop = asyncio.get_running_loop()
    tool_handler.set_event_loop(loop)

    # ── Dashboard Setup ──────────────────────────────────────────────
    conn_manager = ConnectionManager()
    broadcaster = DashboardBroadcaster(conn_manager)
    broadcaster.set_event_loop(loop)

    # Create dashboard app with frame and telemetry adapters
    dashboard_app = create_dashboard_app(
        broadcaster=broadcaster,
        frame_adapter=frame_streamer.get_dashboard_frame,
        telemetry_adapter=lambda: controller.get_telemetry().model_dump(),
    )

    # Start dashboard server in background thread
    dashboard_server = uvicorn.Server(
        uvicorn.Config(
            dashboard_app,
            host="0.0.0.0",
            port=config.DASHBOARD_PORT,
            log_level="warning",
        )
    )
    dashboard_thread = threading.Thread(
        target=dashboard_server.run,
        name="dashboard-server",
        daemon=True,
    )
    dashboard_thread.start()
    logger.info("Dashboard server started on port %d", config.DASHBOARD_PORT)

    # Hook broadcaster into tool handler for tool activity and mission status
    tool_handler.add_tool_activity_listener(broadcaster.send_ai_activity_sync)
    tool_handler.add_status_change_listener(
        lambda mission: broadcaster.send_status_sync({
            "mission_id": str(mission.id),
            "type": mission.type.value if hasattr(mission.type, "value") else str(mission.type),
            "status": mission.status.value
            if hasattr(mission.status, "value")
            else str(mission.status),
            "target": mission.target_description or "",
            "approach_step": mission.approach_step,
        })
    )

    # Emergency landing function
    def emergency_shutdown(reason: str = "signal") -> None:
        logger.warning("Emergency shutdown: %s", reason)
        try:
            controller.emergency_land()
        except Exception:
            logger.exception("Emergency land failed during shutdown")

    # Signal handlers (FR-008)
    def signal_handler(sig: signal.Signals) -> None:
        logger.warning("Received signal %s — initiating emergency shutdown", sig.name)
        emergency_shutdown(sig.name)
        # Schedule graceful exit
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler, sig)

    # Try adding SIGHUP handler (not available on all platforms)
    try:
        loop.add_signal_handler(signal.SIGHUP, signal_handler, signal.SIGHUP)
    except (ValueError, OSError):
        pass

    # Register handlers on backend client
    backend_client.on_audio_out(audio_playback.enqueue)
    backend_client.on_tool_call(tool_handler.handle_tool_calls)
    backend_client.on_interrupted(audio_playback.clear_queue)

    def on_transcript(speaker: str, text: str, timestamp: float) -> None:
        logger.info("[%s] %s", speaker.upper(), text)
        broadcaster.send_log_sync("info", f"[{speaker.upper()}] {text}")

    backend_client.on_transcript(on_transcript)

    def on_session_status(status: str, metadata: dict) -> None:
        logger.info("Session status: %s %s", status, metadata)

    backend_client.on_session_status(on_session_status)

    def on_error(code: str, message: str, recoverable: bool) -> None:
        logger.error("Backend error: %s — %s (recoverable=%s)", code, message, recoverable)

    backend_client.on_error(on_error)

    # Start all components
    controller.start()
    frame_capture.start()
    audio_capture.start(loop)
    audio_playback.start()

    logger.info("All components started. Connecting to backend...")

    try:
        # Connect to backend
        await backend_client.connect()

        # Launch concurrent tasks
        tasks = [
            asyncio.create_task(_audio_send_loop(audio_capture, backend_client), name="audio_send"),
            asyncio.create_task(
                _video_send_loop(frame_streamer, backend_client), name="video_send",
            ),
            asyncio.create_task(backend_client.receive_loop(), name="backend_receive"),
        ]

        logger.info("=== Voice session active. Speak to your drone! ===")

        # Wait for any task to fail or cancel
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)

        for task in done:
            if task.exception():
                logger.error("Task %s failed: %s", task.get_name(), task.exception())

        # Cancel remaining tasks
        for task in pending:
            task.cancel()

    except asyncio.CancelledError:
        logger.info("Main loop cancelled")
    except Exception:
        logger.exception("Unexpected error in main loop")
        emergency_shutdown("exception")
    finally:
        # Graceful shutdown
        logger.info("Shutting down...")

        dashboard_server.should_exit = True
        audio_capture.stop()
        audio_playback.stop()
        frame_capture.stop()
        controller.stop()
        await backend_client.close()

        logger.info("=== Drone Copilot Client Stopped ===")


if __name__ == "__main__":
    asyncio.run(main())

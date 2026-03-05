"""Zero-dependency entry point for demo mode.

Run pre-recorded drone sessions without any hardware, API keys, or drone.

Usage:
    python -m client.src.dashboard.demo_main                     # Scan demos/
    python -m client.src.dashboard.demo_main path/to/recording/  # Specific recording
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import uvicorn

from client.src.dashboard.broadcaster import ConnectionManager, DashboardBroadcaster
from client.src.dashboard.demo_player import DemoPlayer
from client.src.dashboard.server import create_dashboard_app

logger = logging.getLogger(__name__)

DEMOS_DIR = Path(__file__).parent.parent.parent / "demos"


def _scan_demos(demos_dir: Path) -> list[dict]:
    """Scan a directory for available demo recordings."""
    demos = []
    if not demos_dir.exists():
        return demos

    for subdir in sorted(demos_dir.iterdir()):
        if not subdir.is_dir():
            continue
        session_file = subdir / "session.json"
        if not session_file.exists():
            continue
        try:
            first_line = session_file.read_text().splitlines()[0]
            metadata = json.loads(first_line)
            if not metadata.get("_meta"):
                continue
            # Skip placeholder files with 0 messages
            if metadata.get("message_count", 0) == 0:
                continue
            demos.append({
                "id": subdir.name,
                "label": metadata.get("target", subdir.name),
                "target": metadata.get("target", ""),
                "mode": metadata.get("mode", "exploration"),
                "duration_sec": metadata.get("duration_sec", 0),
                "path": str(subdir),
            })
        except Exception:
            logger.debug("Skipping invalid demo: %s", subdir, exc_info=True)

    return demos


def main() -> None:
    """Run the demo mode dashboard server."""
    parser = argparse.ArgumentParser(description="Gemi-fly Demo Mode Dashboard")
    parser.add_argument(
        "recording_path",
        nargs="?",
        default=None,
        help="Path to a specific demo recording directory",
    )
    parser.add_argument(
        "--port", type=int, default=8081,
        help="Dashboard server port (default: 8081)",
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="Dashboard server host (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Scan for available demos
    if args.recording_path:
        recording_path = Path(args.recording_path)
        if not recording_path.exists():
            logger.error("Recording path does not exist: %s", recording_path)
            sys.exit(1)
        demos = _scan_demos(recording_path.parent)
        # Ensure the specified recording is included
        specified = {
            "id": recording_path.name,
            "label": recording_path.name,
            "target": "",
            "mode": "exploration",
            "duration_sec": 0,
            "path": str(recording_path),
        }
        try:
            first_line = (recording_path / "session.json").read_text().splitlines()[0]
            meta = json.loads(first_line)
            specified["label"] = meta.get("target", recording_path.name)
            specified["target"] = meta.get("target", "")
            specified["mode"] = meta.get("mode", "exploration")
            specified["duration_sec"] = meta.get("duration_sec", 0)
        except Exception:
            pass
        if not any(d["id"] == specified["id"] for d in demos):
            demos.insert(0, specified)
    else:
        demos = _scan_demos(DEMOS_DIR)

    logger.info("Found %d demo recording(s)", len(demos))
    for d in demos:
        logger.info("  - %s: %s (%.0fs)", d["id"], d["label"], d["duration_sec"])

    # Build demo path lookup
    demo_paths: dict[str, Path] = {}
    for d in demos:
        demo_paths[d["id"]] = Path(d["path"])

    # Create broadcaster and app
    manager = ConnectionManager()
    broadcaster = DashboardBroadcaster(manager)

    demo_info = [
        {k: v for k, v in d.items() if k != "path"}
        for d in demos
    ]

    app = create_dashboard_app(
        broadcaster=broadcaster,
        demo_mode=True,
        demo_info=demo_info,
    )

    # Active player state
    active_player: dict[str, DemoPlayer | None] = {"player": None}

    async def handle_command(msg: dict) -> None:
        """Handle commands from the dashboard WebSocket."""
        action = msg.get("action", "")

        if action == "start":
            demo_id = msg.get("demo_id", "")
            demo_path = demo_paths.get(demo_id)
            if demo_path is None and demos:
                # Fall back to first demo
                demo_path = Path(demos[0]["path"])

            if demo_path is None:
                await broadcaster.broadcast_log("WARNING", "No demo recording available")
                return

            # Stop existing player
            if active_player["player"] is not None:
                await active_player["player"].stop()

            player = DemoPlayer(demo_path, broadcaster)
            active_player["player"] = player
            asyncio.create_task(player.play())

        elif action == "land":
            if active_player["player"] is not None:
                await active_player["player"].stop()
                active_player["player"] = None

        elif action == "skip_phase":
            if active_player["player"] is not None:
                active_player["player"].skip()

        elif action == "pause":
            if active_player["player"] is not None:
                active_player["player"].toggle_pause()

        elif action == "emergency_land":
            if active_player["player"] is not None:
                await active_player["player"].stop()
                active_player["player"] = None

    app.state.command_handler = handle_command

    # Set event loop for broadcaster
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    broadcaster.set_event_loop(loop)

    print("\n  Gemi-fly Demo Mode")
    print(f"  Dashboard: http://localhost:{args.port}")
    print(f"  Demos available: {len(demos)}")
    if not demos:
        print("  No recordings found. See client/demos/README.md to record demos.")
    print()

    # Run uvicorn
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())


if __name__ == "__main__":
    main()

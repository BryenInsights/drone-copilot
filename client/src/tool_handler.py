"""Tool call handler — receives tool calls from backend, validates, dispatches to drone."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from client.src.mission.inspection import InspectionMission
from client.src.mission.perception_bridge import PerceptionBridge
from client.src.models.mission import MissionStatus
from client.src.models.tool_calls import (
    HoverParams,
    LandParams,
    MoveDroneParams,
    ReportPerceptionParams,
    RotateDroneParams,
    SetSpeedParams,
    StartInspectionParams,
    TakeoffParams,
)

if TYPE_CHECKING:
    from client.src.backend_client import BackendClient
    from client.src.drone.controller import DroneController
    from client.src.video.frame_streamer import FrameStreamer

logger = logging.getLogger(__name__)

# Map tool names to their Pydantic parameter models
# Tools declared with Behavior.NON_BLOCKING in the backend
_NON_BLOCKING_TOOLS = {"move_drone", "rotate_drone"}

# Map tool names to their Pydantic parameter models
_TOOL_MODELS: dict[str, type] = {
    "takeoff": TakeoffParams,
    "land": LandParams,
    "hover": HoverParams,
    "move_drone": MoveDroneParams,
    "rotate_drone": RotateDroneParams,
    "set_speed": SetSpeedParams,
    "start_inspection": StartInspectionParams,
    "report_perception": ReportPerceptionParams,
}


class ToolHandler:
    """Handles tool calls from the Gemini Live API.

    Validates arguments with Pydantic, dispatches to DroneController,
    and sends responses back through the BackendClient.
    """

    def __init__(
        self,
        controller: DroneController,
        backend_client: BackendClient,
        frame_streamer: FrameStreamer | None = None,
    ) -> None:
        self._controller = controller
        self._backend = backend_client
        self._streamer = frame_streamer
        self._config = controller.config

        # Mission state
        self._mission_thread: threading.Thread | None = None
        self._inspection: InspectionMission | None = None
        self._perception_bridge = PerceptionBridge()

        # Event loop for calling async functions from mission thread
        self._loop: asyncio.AbstractEventLoop | None = None

        # Dashboard listeners
        self._on_tool_activity: list[Any] = []
        self._on_status_change: list[Any] = []
        self._on_command_log: list[Any] = []
        self._on_perception_change: list[Any] = []

        # Watchdog state reference (set by main.py)
        self._watchdog_state: dict | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set the asyncio event loop for mission thread async calls."""
        self._loop = loop

    def set_watchdog_state(self, state: dict) -> None:
        """Store reference to the watchdog state dict from main.py."""
        self._watchdog_state = state

    def add_tool_activity_listener(self, callback: Any) -> None:
        """Register a callback for tool activity events (for dashboard)."""
        self._on_tool_activity.append(callback)

    def add_status_change_listener(self, callback: Any) -> None:
        """Register a callback for mission status changes (for dashboard)."""
        self._on_status_change.append(callback)

    def add_command_log_listener(self, callback: Any) -> None:
        """Register a callback for drone command log entries (for dashboard)."""
        self._on_command_log.append(callback)

    def add_perception_listener(self, callback: Any) -> None:
        """Register a callback for perception updates (for dashboard)."""
        self._on_perception_change.append(callback)

    @property
    def is_mission_active(self) -> bool:
        """Whether a mission is currently running."""
        return (
            self._mission_thread is not None
            and self._mission_thread.is_alive()
        )

    async def handle_tool_calls(self, calls: list[dict]) -> None:
        """Process a batch of tool calls from the backend."""
        for call in calls:
            tool_id = call.get("id", "")
            tool_name = call.get("name", "")
            tool_args = call.get("args", {})

            logger.info("Tool call received: %s (id=%s)", tool_name, tool_id)

            # Validate arguments
            model_cls = _TOOL_MODELS.get(tool_name)
            if model_cls is None:
                response = {
                    "success": False,
                    "error": "unknown_tool",
                    "message": f"Unknown tool: {tool_name}",
                }
                await self._send_response(tool_id, tool_name, response)
                continue

            try:
                params = model_cls.model_validate(tool_args)
            except ValidationError as e:
                response = {
                    "success": False,
                    "error": "validation_failed",
                    "message": str(e),
                }
                logger.warning(
                    "Tool validation failed for %s: %s", tool_name, e,
                )
                await self._send_response(tool_id, tool_name, response)
                continue

            # Dispatch to handler
            if self._watchdog_state is not None:
                self._watchdog_state["tool_in_progress"] = True
            try:
                response = await self._dispatch(tool_name, params)
            finally:
                if self._watchdog_state is not None:
                    self._watchdog_state["tool_in_progress"] = False
                    self._watchdog_state["last_copilot_ts"] = time.time()

            # Broadcast tool activity
            self._broadcast_activity(tool_name, tool_args, response)

            await self._send_response(tool_id, tool_name, response)

    async def _dispatch(self, name: str, params: Any) -> dict:
        """Dispatch validated tool call to the appropriate handler."""
        try:
            if name == "takeoff":
                return self._controller.takeoff()

            elif name == "land":
                # Abort active mission on land
                if self.is_mission_active:
                    self._abort_mission("User requested landing")
                return self._controller.land()

            elif name == "hover":
                # Abort active mission on hover/stop
                if self.is_mission_active:
                    self._abort_mission("User requested stop")
                return self._controller.hover()

            elif name == "move_drone":
                if self.is_mission_active:
                    return {
                        "success": False,
                        "error": "mission_active",
                        "message": "Inspection mission is controlling the drone. "
                        "Use report_perception to guide the approach, "
                        "or say 'stop' to cancel the mission.",
                    }
                # Clamp forward distance after recent inspection completion
                if (
                    params.direction == "forward"
                    and self._inspection
                    and self._inspection.mission
                    and self._inspection.mission.status == MissionStatus.COMPLETE
                    and self._inspection.mission.final_relative_size is not None
                    and self._inspection.mission.final_relative_size >= 0.30
                ):
                    max_fwd = self._config.INSPECTION_POST_MOVE_CLAMP
                    if params.distance_cm > max_fwd:
                        logger.info(
                            "Clamping post-inspection forward from %dcm to %dcm",
                            params.distance_cm, max_fwd,
                        )
                        params.distance_cm = max_fwd
                result = self._controller.move(
                    params.direction, params.distance_cm,
                )
                if result.get("success") and self._streamer:
                    await self._send_fresh_frame_after_action()
                return result

            elif name == "rotate_drone":
                if self.is_mission_active:
                    return {
                        "success": False,
                        "error": "mission_active",
                        "message": "Inspection mission is controlling the drone. "
                        "Use report_perception to guide the approach, "
                        "or say 'stop' to cancel the mission.",
                    }
                result = self._controller.rotate(
                    params.direction, params.degrees,
                )
                if result.get("success") and self._streamer:
                    await self._send_fresh_frame_after_action()
                return result

            elif name == "set_speed":
                return self._controller.set_speed(params.speed_cm_per_sec)

            elif name == "start_inspection":
                return self._handle_start_inspection(params)

            elif name == "report_perception":
                return self._handle_report_perception(params)

            else:
                return {
                    "success": False,
                    "error": "not_implemented",
                    "message": f"{name} not implemented",
                }

        except Exception as e:
            logger.exception("Tool dispatch error for %s", name)
            return {
                "success": False,
                "error": "execution_error",
                "message": str(e),
            }

    # ------------------------------------------------------------------
    # Post-action fresh frame
    # ------------------------------------------------------------------

    async def _send_fresh_frame_after_action(self) -> None:
        """Flush stale frames and send a guaranteed-fresh frame to Gemini."""
        if not self._streamer:
            return
        b64_frame = await asyncio.to_thread(
            self._streamer.get_fresh_perception_frame, 3.0,
        )
        if b64_frame:
            await self._backend.send_video(b64_frame)
            logger.info("Sent fresh post-action frame to Gemini")
        else:
            logger.warning("No fresh frame available after action")

    # ------------------------------------------------------------------
    # Inspection mission
    # ------------------------------------------------------------------

    def _handle_start_inspection(
        self, params: StartInspectionParams,
    ) -> dict:
        """Launch inspection mission in background thread."""
        if self.is_mission_active:
            return {
                "success": False,
                "error": "mission_active",
                "message": "A mission is already running. "
                "Say 'stop' to cancel it first.",
            }

        if self._streamer is None:
            return {
                "success": False,
                "error": "no_video",
                "message": "Frame streamer not available",
            }

        logger.info(
            "Starting inspection mission: %s (aspects=%s)",
            params.target_description,
            params.aspects,
        )

        # Create async helpers for the mission thread
        async def send_text(text: str) -> None:
            await self._backend.send_text(text)

        async def send_video(frame_b64: str) -> None:
            await self._backend.send_video(frame_b64)

        async def send_frame_with_prompt(frames: list[bytes], prompt: str) -> None:
            await self._backend.send_frames_with_prompt(frames, prompt)

        async def send_inspection_frames(
            frames: list[bytes],
            labels: list[str],
            target: str,
            aspects: str | None,
        ) -> None:
            prompt = (
                f"I just captured {len(frames)} inspection frames of "
                f"'{target}' from different angles: "
                f"{', '.join(labels)}. "
                f"Please provide a detailed verbal assessment of what you see."
            )
            if aspects:
                prompt += f" Focus especially on: {aspects}."
            prompt += (
                " Describe the object's condition, notable features, "
                "and any issues or observations."
            )
            await self._backend.send_frames_with_prompt(
                frames, prompt,
            )

        # Create mission
        self._inspection = InspectionMission(
            controller=self._controller,
            frame_streamer=self._streamer,
            config=self._config,
            perception_bridge=self._perception_bridge,
            send_text_fn=send_text,
            send_frames_fn=send_inspection_frames,
            send_video_fn=send_video,
            send_frame_with_prompt_fn=send_frame_with_prompt,
            on_status_change=self._notify_status_change,
            on_command_log=self._broadcast_command_log,
        )
        if self._loop:
            self._inspection.set_event_loop(self._loop)

        # Launch in background thread (daemon=False per lesson C2)
        self._mission_thread = threading.Thread(
            target=self._run_inspection,
            args=(params.target_description, params.aspects, params.needs_search),
            name="inspection-mission",
            daemon=False,
        )
        self._mission_thread.start()

        # Notify backend of active mission for reconnect context
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._backend.send_mission_context({
                    "target": params.target_description,
                    "phase": "searching" if params.needs_search else "approaching",
                }),
                self._loop,
            )

        return {
            "success": True,
            "result": f"inspection_started_for_{params.target_description}",
        }

    def _run_inspection(
        self,
        target_description: str,
        aspects: str | None,
        needs_search: bool = False,
    ) -> None:
        """Run inspection mission in background thread."""
        try:
            if self._inspection:
                mission = self._inspection.run(
                    target_description, aspects, needs_search=needs_search,
                )
                logger.info(
                    "Inspection mission completed: %s", mission.status,
                )
        except Exception:
            logger.exception("Inspection mission thread error")
            if self._controller.state.is_flying:
                self._controller.emergency_land()
        finally:
            # Clear mission context regardless of outcome
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._backend.send_mission_context(None),
                    self._loop,
                )

    def _abort_mission(self, reason: str) -> None:
        """Abort the active mission."""
        logger.warning("Aborting mission: %s", reason)
        if self._inspection:
            self._inspection.abort()
        if self._mission_thread and self._mission_thread.is_alive():
            self._mission_thread.join(timeout=15.0)
            if self._mission_thread.is_alive():
                logger.error(
                    "Mission thread did not stop within 15s timeout",
                )
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._backend.send_mission_context(None),
                self._loop,
            )

    # ------------------------------------------------------------------
    # Perception
    # ------------------------------------------------------------------

    def _handle_report_perception(
        self, params: ReportPerceptionParams,
    ) -> dict:
        """Feed perception result to perception bridge for active missions."""
        self._broadcast_perception(params)

        if not self._perception_bridge.active and not self.is_mission_active:
            return {
                "success": True,
                "mission_active": False,
                "message": "No active mission. Use move_drone/rotate_drone for manual control.",
            }

        self._perception_bridge.feed(params)

        logger.debug(
            "Perception: visible=%s h=%.2f v=%.2f size=%.3f conf=%.2f",
            params.target_visible,
            params.horizontal_offset,
            params.vertical_offset,
            params.relative_size,
            params.confidence,
        )
        return {"success": True, "result": "perception_recorded"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_response(
        self, tool_id: str, name: str, response: dict,
    ) -> None:
        """Send tool response with drone state back to backend."""
        response["drone_state"] = self._controller.get_state_dict()

        # NON_BLOCKING tools: deliver on success when idle, interrupt on failure
        scheduling: str | None = None
        if name in _NON_BLOCKING_TOOLS:
            scheduling = "WHEN_IDLE" if response.get("success") else "INTERRUPT"

        await self._backend.send_tool_response(
            tool_id, name, response, scheduling=scheduling,
        )
        logger.info(
            "Tool response sent: %s success=%s",
            name,
            response.get("success"),
        )

    def _broadcast_activity(
        self, name: str, args: dict, result: dict,
    ) -> None:
        """Notify listeners about tool activity (for dashboard)."""
        activity = {
            "name": name,
            "args": args,
            "result": result,
            "timestamp": time.time(),
        }
        for cb in self._on_tool_activity:
            try:
                cb(activity)
            except Exception:
                logger.warning("Tool activity listener error", exc_info=True)

    def _broadcast_perception(self, params: ReportPerceptionParams) -> None:
        """Notify listeners about perception data (for dashboard overlay)."""
        data = params.model_dump()
        for cb in self._on_perception_change:
            try:
                cb(data)
            except Exception:
                logger.warning("Perception listener error", exc_info=True)

    def _broadcast_command_log(self, message: str) -> None:
        """Notify listeners about a drone command (for dashboard mission log)."""
        for cb in self._on_command_log:
            try:
                cb(message)
            except Exception:
                logger.warning("Command log listener error", exc_info=True)

    def _notify_status_change(self, mission: Any) -> None:
        """Notify listeners about mission status changes."""
        for cb in self._on_status_change:
            try:
                cb(mission)
            except Exception:
                logger.warning(
                    "Status change listener error", exc_info=True,
                )

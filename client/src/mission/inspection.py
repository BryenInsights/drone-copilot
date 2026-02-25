"""Inspection mission — multi-angle observation and detailed verbal assessment.

Implements User Story 3: "Check that plant for issues" — the drone approaches
the target, observes from multiple angles, and delivers a detailed verbal
assessment.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from client.src.config import ClientConfig
from client.src.mission.exploration import ApproachController, PerceptionBridge
from client.src.models.mission import Mission, MissionStatus, MissionType

if TYPE_CHECKING:
    from client.src.drone.controller import DroneController
    from client.src.video.frame_streamer import FrameStreamer

logger = logging.getLogger(__name__)

# Inspection angle offsets from the initial heading (degrees clockwise)
INSPECTION_ANGLES = [0, 45, -45]  # front, right-45, left-45
INSPECTION_ANGLE_LABELS = ["front", "right 45°", "left 45°"]


class InspectionMission:
    """Multi-angle inspection mission: approach → capture angles → land → report.

    Runs in a background thread. Optionally reuses ApproachController from
    exploration.py if the target needs to be approached first.
    """

    def __init__(
        self,
        controller: DroneController,
        frame_streamer: FrameStreamer,
        config: ClientConfig,
        perception_bridge: PerceptionBridge,
        send_text_fn: Any = None,
        send_frames_fn: Any = None,
        on_status_change: Any = None,
    ) -> None:
        self._controller = controller
        self._streamer = frame_streamer
        self._config = config
        self._perception = perception_bridge
        self._send_text = send_text_fn  # async fn to inject text into Gemini session
        self._send_frames = send_frames_fn  # async fn to send frames for analysis
        self._on_status_change = on_status_change
        self._abort_event = threading.Event()
        self._mission: Mission | None = None
        self._loop: Any = None

    @property
    def mission(self) -> Mission | None:
        return self._mission

    def abort(self) -> None:
        """Abort the mission."""
        self._abort_event.set()
        self._perception.deactivate()

    def set_event_loop(self, loop: Any) -> None:
        """Set the asyncio event loop for calling async functions from the mission thread."""
        self._loop = loop

    def _notify_status(self) -> None:
        """Notify status change listeners."""
        if self._on_status_change and self._mission:
            try:
                self._on_status_change(self._mission)
            except Exception:
                logger.warning("Status change callback error", exc_info=True)

    def _send_text_sync(self, text: str) -> None:
        """Send text to Gemini session from synchronous mission thread."""
        if self._send_text and self._loop:
            import asyncio
            future = asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop)
            try:
                future.result(timeout=5.0)
            except Exception:
                logger.warning("Failed to send text to Gemini", exc_info=True)

    def _send_inspection_frames_sync(
        self,
        frames: list[bytes],
        labels: list[str],
        target: str,
        aspects: str | None,
    ) -> None:
        """Send captured inspection frames to Gemini for analysis."""
        if self._send_frames and self._loop:
            import asyncio
            future = asyncio.run_coroutine_threadsafe(
                self._send_frames(frames, labels, target, aspects), self._loop
            )
            try:
                future.result(timeout=30.0)
            except Exception:
                logger.warning("Failed to send inspection frames to Gemini", exc_info=True)

    def run(self, target_description: str, aspects: str | None = None) -> Mission:
        """Execute the full inspection mission (called from background thread).

        Phases: approaching (if needed) → inspecting → complete/aborted
        """
        self._mission = Mission(
            type=MissionType.INSPECT,
            status=MissionStatus.APPROACHING,
            target_description=target_description,
            started_at=time.time(),
        )
        self._abort_event.clear()

        try:
            self._notify_status()
            logger.info(
                "Inspection started: '%s' (aspects=%s)",
                target_description, aspects,
            )

            # Ensure drone is flying
            if not self._controller.state.is_flying:
                logger.info("Drone not flying — taking off for inspection")
                result = self._controller.takeoff()
                if not result.get("success"):
                    logger.error("Takeoff failed: %s", result)
                    self._mission.status = MissionStatus.ABORTED
                    self._notify_status()
                    return self._mission

            if self._abort_event.is_set():
                raise RuntimeError("Inspection aborted by user")

            # Notify Gemini about the inspection
            self._send_text_sync(
                f"Starting inspection of: {target_description}. "
                f"I will capture frames from multiple angles for a detailed assessment."
                + (f" Focus on: {aspects}." if aspects else "")
            )

            # --- Multi-angle capture ---
            self._mission.status = MissionStatus.INSPECTING
            self._notify_status()

            captured_frames: list[bytes] = []
            captured_labels: list[str] = []

            for i, (angle, label) in enumerate(
                zip(INSPECTION_ANGLES, INSPECTION_ANGLE_LABELS)
            ):
                if self._abort_event.is_set():
                    raise RuntimeError("Inspection aborted by user")

                # Rotate to inspection angle (relative from current heading)
                if angle != 0:
                    direction = "clockwise" if angle > 0 else "counter_clockwise"
                    degrees = abs(angle)
                    logger.info(
                        "Rotating %s %d° for %s view",
                        direction, degrees, label,
                    )
                    self._controller.rotate(direction, degrees)
                    time.sleep(self._config.INTER_COMMAND_ROTATE_DELAY)

                # Wait stabilization
                time.sleep(0.5)

                # Capture high-res frame
                jpeg_bytes = self._streamer.get_dashboard_frame()
                if jpeg_bytes is not None:
                    captured_frames.append(jpeg_bytes)
                    captured_labels.append(label)
                    logger.info(
                        "Inspection frame %d/%d captured (%s, %d bytes)",
                        i + 1, len(INSPECTION_ANGLES), label, len(jpeg_bytes),
                    )
                else:
                    logger.warning("No frame captured for %s view", label)

                # Return to original heading if we rotated
                if angle != 0:
                    # Rotate back
                    back_dir = "counter_clockwise" if angle > 0 else "clockwise"
                    self._controller.rotate(back_dir, abs(angle))
                    time.sleep(self._config.INTER_COMMAND_ROTATE_DELAY)

            if not captured_frames:
                logger.error("No frames captured during inspection")
                self._mission.status = MissionStatus.ABORTED
                self._notify_status()
                return self._mission

            # --- Land and generate report ---
            logger.info(
                "Inspection capture complete (%d frames). Landing for analysis.",
                len(captured_frames),
            )
            self._controller.land()

            if self._abort_event.is_set():
                raise RuntimeError("Inspection aborted by user")

            # Send all captured frames to Gemini for detailed analysis
            self._send_inspection_frames_sync(
                captured_frames, captured_labels, target_description, aspects,
            )

            # The AI will respond verbally with its assessment — no need to
            # parse the response. The verbal report IS the output of the
            # inspection mission. Gemini will describe findings based on the
            # frames and aspects provided.

            # Give Gemini time to generate the verbal report
            logger.info("Waiting for Gemini verbal report...")
            time.sleep(2.0)  # Brief wait for report generation to begin

            self._mission.status = MissionStatus.COMPLETE
            self._notify_status()
            logger.info("Inspection mission completed successfully")
            return self._mission

        except RuntimeError as e:
            logger.warning("Inspection mission error: %s", e)
            self._mission.status = MissionStatus.ABORTED
            self._perception.deactivate()
            self._notify_status()
            return self._mission

        except Exception:
            logger.exception("Unexpected error in inspection mission")
            self._mission.status = MissionStatus.ABORTED
            self._perception.deactivate()
            self._notify_status()
            if self._controller.state.is_flying:
                self._controller.emergency_land()
            return self._mission

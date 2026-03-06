"""Inspection mission — approach target with perception loop + lateral strafe capture.

Two phases:
1. APPROACHING: Use perception bridge to center on and approach the target
   until it fills enough of the frame (or max steps / blind limit reached).
2. INSPECTING: Lateral strafe to capture 3 perspectives (front, left, right)
   then send batch to Gemini for comprehensive verbal summary.

Does NOT auto-land — stays hovering for follow-up commands.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from client.src.config import ClientConfig
from client.src.mission.perception_bridge import PerceptionBridge
from client.src.models.mission import Mission, MissionStateMachine, MissionStatus, MissionType
from client.src.models.tool_calls import ReportPerceptionParams

if TYPE_CHECKING:
    from client.src.drone.controller import DroneController
    from client.src.video.frame_streamer import FrameStreamer

logger = logging.getLogger(__name__)


class InspectionMission:
    """Two-phase inspection: perception-guided approach + lateral strafe capture.

    Runs in a background thread. After inspection completes, the drone stays
    hovering for follow-up commands (no auto-land).
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
        self._send_text = send_text_fn
        self._send_frames = send_frames_fn
        self._on_status_change = on_status_change
        self._abort_event = threading.Event()
        self._mission: Mission | None = None
        self._sm: MissionStateMachine | None = None
        self._loop: Any = None

    @property
    def mission(self) -> Mission | None:
        return self._mission

    def abort(self) -> None:
        """Abort the mission."""
        self._abort_event.set()
        self._perception.deactivate()
        if self._sm is not None:
            self._sm.try_transition(MissionStatus.ABORTED)

    def set_event_loop(self, loop: Any) -> None:
        """Set the asyncio event loop for calling async functions from the mission thread."""
        self._loop = loop

    def _notify_status(self) -> None:
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
                self._send_frames(frames, labels, target, aspects), self._loop,
            )
            try:
                future.result(timeout=30.0)
            except Exception:
                logger.warning("Failed to send inspection frames to Gemini", exc_info=True)

    def _check_abort(self) -> None:
        """Raise if abort requested."""
        if self._abort_event.is_set():
            raise RuntimeError("Inspection aborted by user")

    def _post_rotation_flush(self) -> None:
        """Flush stale H264 frames after any rotation.

        The H264 decode pipeline continues outputting pre-rotation frames
        for several frames after a rotation completes. This ensures the
        next perception report is based on what the camera actually sees now.
        """
        if self._streamer:
            self._streamer.reset_rate_limit()
        capture = getattr(self._streamer, "_capture", None)
        if capture:
            capture.flush_and_wait(min_new_frames=3, timeout=3.0)

    def run(self, target_description: str, aspects: str | None = None) -> Mission:
        """Execute the full inspection mission (called from background thread).

        Phases: approaching → inspecting → complete/aborted
        """
        self._mission = Mission(
            type=MissionType.INSPECT,
            status=MissionStatus.IDLE,
            target_description=target_description,
            started_at=time.time(),
        )
        sm = MissionStateMachine(self._mission)
        self._sm = sm
        self._abort_event.clear()

        try:
            sm.transition(MissionStatus.APPROACHING)
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
                    sm.try_transition(MissionStatus.ABORTED)
                    self._notify_status()
                    return self._mission

            self._check_abort()

            # Notify Gemini about the inspection
            self._send_text_sync(
                f"Starting inspection of: {target_description}. "
                f"I will approach the target and then capture frames from multiple "
                f"angles for a detailed assessment."
                + (f" Focus on: {aspects}." if aspects else ""),
            )

            # --- Phase 1: Approach ---
            # Skip scan phase — Gemini already confirmed the target is visible
            # before calling start_inspection. The approach phase handles
            # target-not-visible with search rotations as a safety net.
            self._send_text_sync(
                "Starting approach phase. "
                "Use report_perception to tell me EXACTLY where the target "
                "is in the current frame. This is required after every movement.",
            )
            self._run_approach_phase(target_description)

            self._check_abort()

            # --- Phase 2: Lateral strafe inspection ---
            sm.transition(MissionStatus.INSPECTING)
            self._notify_status()

            self._run_inspection_phase(target_description, aspects)

            sm.transition(MissionStatus.COMPLETE)
            self._notify_status()
            logger.info("Inspection mission completed — drone hovering for follow-up")
            return self._mission

        except RuntimeError as e:
            logger.warning("Inspection mission error: %s", e)
            sm.try_transition(MissionStatus.ABORTED)
            self._perception.deactivate()
            self._notify_status()
            return self._mission

        except Exception:
            logger.exception("Unexpected error in inspection mission")
            sm.try_transition(MissionStatus.ABORTED)
            self._perception.deactivate()
            self._notify_status()
            if self._controller.state.is_flying:
                self._controller.emergency_land()
            return self._mission

    # ------------------------------------------------------------------
    # Phase 1: Perception-guided approach
    # ------------------------------------------------------------------

    @staticmethod
    def _is_stale(
        current: ReportPerceptionParams,
        previous: ReportPerceptionParams | None,
    ) -> bool:
        """Detect stale data — identical values from consecutive perceptions."""
        if previous is None:
            return False
        return (
            abs(current.horizontal_offset - previous.horizontal_offset) < 0.01
            and abs(current.vertical_offset - previous.vertical_offset) < 0.01
            and abs(current.relative_size - previous.relative_size) < 0.01
            and current.confidence == previous.confidence
        )

    def _run_approach_phase(self, target_description: str) -> None:
        """Approach the target using perception feedback from Gemini.

        Uses EMA smoothing, stale-data rejection with self-correcting recovery,
        size-adaptive forward distance, and centering gate.
        """
        cfg = self._config
        self._perception.activate()

        try:
            # Ask Gemini to start reporting perception
            self._send_text_sync(PerceptionBridge.build_nudge_text(target_description))

            consecutive_blind = 0
            watchdog_start = time.monotonic()

            # Stale detection state
            last_raw_perception: ReportPerceptionParams | None = None
            consecutive_stale = 0
            stale_recovery_offset = 0  # Track cumulative heading drift

            # EMA smoothing state
            smoothed_size: float | None = None
            smoothed_h_off: float | None = None
            smoothed_v_off: float | None = None
            alpha = 0.5

            for step in range(cfg.INSPECTION_MAX_APPROACH_STEPS):
                self._check_abort()

                # Watchdog: abort approach if total time exceeded
                elapsed = time.monotonic() - watchdog_start
                if elapsed >= cfg.INSPECTION_APPROACH_WATCHDOG_S:
                    logger.warning(
                        "Approach watchdog timeout (%.0fs). "
                        "Proceeding to inspection at current distance.",
                        elapsed,
                    )
                    break

                # Wait for perception
                perception = self._perception.wait_for_perception(
                    timeout=cfg.INSPECTION_PERCEPTION_TIMEOUT,
                )

                if perception is None:
                    # Nudge Gemini and retry once
                    logger.info("No perception received — nudging Gemini (step %d)", step)
                    self._send_text_sync(PerceptionBridge.build_nudge_text(target_description))
                    perception = self._perception.wait_for_perception(timeout=5.0)

                if perception is None:
                    consecutive_blind += 1
                    logger.warning(
                        "Perception timeout (consecutive_blind=%d/%d)",
                        consecutive_blind, cfg.INSPECTION_MAX_BLIND_STEPS,
                    )
                    if consecutive_blind >= cfg.INSPECTION_MAX_BLIND_STEPS:
                        logger.warning(
                            "Max blind steps reached. "
                            "Proceeding to inspection at current distance.",
                        )
                        break
                    continue  # Do NOT move — just try next step

                consecutive_blind = 0

                logger.info(
                    "Approach step %d: visible=%s h=%.2f v=%.2f size=%.3f conf=%.2f",
                    step, perception.target_visible,
                    perception.horizontal_offset, perception.vertical_offset,
                    perception.relative_size, perception.confidence,
                )

                # Target not visible or low confidence — search rotation
                if not perception.target_visible or perception.confidence < 0.3:
                    logger.info("Target not visible/low confidence — small search rotation")
                    self._controller.rotate("clockwise", 30)
                    time.sleep(cfg.APPROACH_ROTATE_DELAY)
                    self._post_rotation_flush()
                    continue

                # --- Stale data rejection ---
                if self._is_stale(perception, last_raw_perception):
                    consecutive_stale += 1
                    logger.warning("Stale perception (%d consecutive)", consecutive_stale)
                    if consecutive_stale >= 2:
                        # Alternate direction to bound heading drift
                        direction = (
                            "clockwise" if stale_recovery_offset <= 0
                            else "counter_clockwise"
                        )
                        self._controller.rotate(direction, 10)
                        stale_recovery_offset += 10 if direction == "clockwise" else -10
                        time.sleep(cfg.APPROACH_ROTATE_DELAY)
                        self._post_rotation_flush()
                        consecutive_stale = 0
                    continue  # Don't move forward on stale data

                # --- Fresh data — reset stale state and undo heading drift ---
                consecutive_stale = 0
                last_raw_perception = perception

                if stale_recovery_offset != 0:
                    undo_dir = (
                        "counter_clockwise" if stale_recovery_offset > 0
                        else "clockwise"
                    )
                    self._controller.rotate(undo_dir, abs(stale_recovery_offset))
                    stale_recovery_offset = 0
                    time.sleep(cfg.APPROACH_ROTATE_DELAY)
                    self._post_rotation_flush()

                # --- EMA smoothing (all three axes, fresh data only) ---
                if smoothed_size is None:
                    smoothed_size = perception.relative_size
                    smoothed_h_off = perception.horizontal_offset
                    smoothed_v_off = perception.vertical_offset
                else:
                    smoothed_size = (
                        alpha * perception.relative_size + (1 - alpha) * smoothed_size
                    )
                    smoothed_h_off = (
                        alpha * perception.horizontal_offset + (1 - alpha) * smoothed_h_off
                    )
                    smoothed_v_off = (
                        alpha * perception.vertical_offset + (1 - alpha) * smoothed_v_off
                    )

                logger.info(
                    "EMA: size=%.3f h=%.2f v=%.2f",
                    smoothed_size, smoothed_h_off, smoothed_v_off,
                )

                # --- Centering gate: rotate to center before moving forward ---
                if abs(smoothed_h_off) > cfg.INSPECTION_CENTER_THRESHOLD:
                    # Proportional rotation (raw offset for responsiveness)
                    rotate_deg = int(
                        abs(perception.horizontal_offset) * cfg.INSPECTION_ROTATION_GAIN,
                    )
                    rotate_deg = max(cfg.MIN_ROTATION, min(rotate_deg, 45))
                    direction = (
                        "clockwise" if perception.horizontal_offset > 0
                        else "counter_clockwise"
                    )
                    logger.info(
                        "Centering: rotate %s %d° (smoothed_h=%.2f)",
                        direction, rotate_deg, smoothed_h_off,
                    )
                    self._controller.rotate(direction, rotate_deg)
                    time.sleep(cfg.APPROACH_ROTATE_DELAY)
                    self._post_rotation_flush()
                    continue  # Don't move forward until centered

                # --- Close enough? ---
                if smoothed_size >= cfg.INSPECTION_APPROACH_SIZE_THRESHOLD:
                    logger.info(
                        "Target close enough (smoothed_size=%.3f >= %.3f)",
                        smoothed_size, cfg.INSPECTION_APPROACH_SIZE_THRESHOLD,
                    )
                    break

                # --- Size-adaptive forward distance ---
                if smoothed_size < 0.12:
                    forward_cm = cfg.INSPECTION_FORWARD_FAR
                elif smoothed_size < 0.16:
                    forward_cm = cfg.INSPECTION_FORWARD_MEDIUM
                else:
                    forward_cm = cfg.INSPECTION_FORWARD_CLOSE

                logger.info(
                    "Moving forward %dcm (smoothed_size=%.3f)", forward_cm, smoothed_size,
                )
                result = self._controller.move("forward", forward_cm)
                if not result.get("success"):
                    logger.error("Forward move failed: %s", result)
                    raise RuntimeError(
                        f"Movement failed: {result.get('message', 'unknown')}",
                    )
                time.sleep(cfg.APPROACH_MOVE_DELAY)

                # --- Post-movement nudge (soft optimization) ---
                if self._streamer:
                    self._streamer.reset_rate_limit()
                self._send_text_sync(
                    f"I just moved forward {forward_cm}cm. "
                    f"Call report_perception for '{target_description}' "
                    f"based on what you see now.",
                )

            else:
                logger.info(
                    "Max approach steps (%d) reached. Proceeding to inspection.",
                    cfg.INSPECTION_MAX_APPROACH_STEPS,
                )

        finally:
            self._perception.deactivate()

    # ------------------------------------------------------------------
    # Phase 2: Lateral strafe inspection
    # ------------------------------------------------------------------

    def _run_inspection_phase(
        self, target_description: str, aspects: str | None,
    ) -> None:
        """Capture frames from 3 lateral perspectives using strafe movements."""
        lateral = self._config.INSPECTION_LATERAL_DISTANCE
        captured_frames: list[bytes] = []
        captured_labels: list[str] = []

        # 1. Front close-up — capture at current position (flush for fresh frame)
        self._check_abort()
        time.sleep(1.0)  # stabilization
        frame = self._streamer.get_fresh_dashboard_frame(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append("front close-up")
            logger.info("Captured front close-up (%d bytes)", len(frame))
        else:
            logger.warning("No frame for front close-up")

        # 2. Strafe left — capture (target visible from right side)
        self._check_abort()
        logger.info("Strafing left %dcm", lateral)
        result = self._controller.move("left", lateral)
        if not result.get("success"):
            logger.error("Left strafe failed: %s", result)
            raise RuntimeError(f"Left strafe failed: {result.get('message', 'unknown')}")
        time.sleep(1.0)  # stabilization after strafe

        frame = self._streamer.get_fresh_dashboard_frame(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append("left strafe (right side view)")
            logger.info("Captured left strafe view (%d bytes)", len(frame))
        else:
            logger.warning("No frame for left strafe view")

        # 3. Strafe right (back to center + right) — capture (target from left side)
        self._check_abort()
        right_distance = lateral * 2
        logger.info("Strafing right %dcm", right_distance)
        result = self._controller.move("right", right_distance)
        if not result.get("success"):
            logger.error("Right strafe failed: %s", result)
            raise RuntimeError(f"Right strafe failed: {result.get('message', 'unknown')}")
        time.sleep(1.0)  # stabilization after strafe

        frame = self._streamer.get_fresh_dashboard_frame(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append("right strafe (left side view)")
            logger.info("Captured right strafe view (%d bytes)", len(frame))
        else:
            logger.warning("No frame for right strafe view")

        # 4. Return to center (no capture)
        self._check_abort()
        logger.info("Returning to center — strafing left %dcm", lateral)
        result = self._controller.move("left", lateral)
        if not result.get("success"):
            logger.warning("Return-to-center strafe failed: %s", result)

        if not captured_frames:
            logger.error("No frames captured during inspection")
            if self._sm:
                self._sm.try_transition(MissionStatus.ABORTED)
            self._notify_status()
            return

        # Send batch frames for comprehensive summary
        logger.info(
            "Inspection capture complete (%d frames: %s). Sending for analysis.",
            len(captured_frames), ", ".join(captured_labels),
        )

        self._check_abort()

        self._send_inspection_frames_sync(
            captured_frames, captured_labels, target_description, aspects,
        )

        # Brief wait for Gemini to begin generating the verbal report
        logger.info("Waiting for Gemini to begin verbal report...")
        time.sleep(2.0)

        logger.info("Inspection phase complete — staying airborne for follow-up")

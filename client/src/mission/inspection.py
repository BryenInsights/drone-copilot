"""Inspection mission — approach target with perception loop + 45-degree orbit arcs.

Three phases (when needs_search=True):
1. SEARCHING: Deterministic 360-degree scan using PerceptionBridge to find target.
2. APPROACHING: Use perception bridge to center on and approach the target
   until it fills enough of the frame (or max steps / stagnation / blind limit).
3. INSPECTING: 45-degree orbit arcs to capture 3 perspectives (front, right-angled,
   left-angled) then send batch to Gemini for comprehensive verbal summary.

When needs_search=False, skips phase 1.
Does NOT auto-land — stays hovering for follow-up commands.
"""

from __future__ import annotations

import base64
import logging
import math
import threading
import time
from pathlib import Path
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
    """Three-phase inspection: search → perception-guided approach → orbit arc capture.

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
        send_video_fn: Any = None,
        send_frame_with_prompt_fn: Any = None,
        on_status_change: Any = None,
        on_command_log: Any = None,
    ) -> None:
        self._controller = controller
        self._streamer = frame_streamer
        self._config = config
        self._perception = perception_bridge
        self._send_text = send_text_fn
        self._send_frames = send_frames_fn
        self._send_video = send_video_fn
        self._send_frame_with_prompt = send_frame_with_prompt_fn
        self._on_status_change = on_status_change
        self._on_command_log = on_command_log
        self._abort_event = threading.Event()
        self._mission: Mission | None = None
        self._sm: MissionStateMachine | None = None
        self._loop: Any = None
        self._debug_frame_dir: Path | None = None
        self._debug_frame_counter: int = 0
        self._consecutive_send_failures: int = 0
        self._max_send_failures: int = 3

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

    def _log_command(self, message: str) -> None:
        """Log a drone command to the mission log (dashboard)."""
        if self._on_command_log:
            try:
                self._on_command_log(message)
            except Exception:
                logger.warning("Command log callback error", exc_info=True)

    def _track_send_success(self) -> None:
        self._consecutive_send_failures = 0

    def _track_send_failure(self) -> None:
        self._consecutive_send_failures += 1
        if self._consecutive_send_failures >= self._max_send_failures:
            logger.error(
                "Session appears dead (%d consecutive send failures) — aborting mission",
                self._consecutive_send_failures,
            )
            raise RuntimeError("Session lost — aborting mission for safety")

    def _send_text_sync(self, text: str) -> None:
        """Send text to Gemini session from synchronous mission thread."""
        if self._send_text and self._loop:
            import asyncio

            future = asyncio.run_coroutine_threadsafe(self._send_text(text), self._loop)
            try:
                future.result(timeout=5.0)
                self._track_send_success()
            except Exception:
                logger.warning("Failed to send text to Gemini", exc_info=True)
                self._track_send_failure()

    def _init_debug_dir(self) -> None:
        if not self._config.DEBUG_SAVE_FRAMES:
            return
        target_slug = (self._mission.target_description or "unknown")[:30].replace(" ", "_")
        self._debug_frame_dir = Path("debug_frames") / f"{time.strftime('%H%M%S')}_{target_slug}"
        self._debug_frame_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Debug frames → %s", self._debug_frame_dir)

    def _save_debug_frame(self, frame_bytes: bytes, label: str) -> None:
        if self._debug_frame_dir is None:
            return
        self._debug_frame_counter += 1
        filename = f"{self._debug_frame_counter:03d}_{label}.jpg"
        (self._debug_frame_dir / filename).write_bytes(frame_bytes)
        logger.info("Saved debug frame: %s (%d bytes)", filename, len(frame_bytes))

    def _send_fresh_frame_sync(
        self, target_description: str, debug_label: str = "", preamble: str = "",
    ) -> None:
        """Get a fresh frame and send it with the perception nudge in a single Gemini turn."""
        if not self._streamer:
            return

        frame_bytes = self._streamer.get_fresh_perception_frame_bytes(timeout=3.0)
        if not frame_bytes:
            logger.warning("No fresh frame available — sending text-only nudge")
            self._send_text_sync(PerceptionBridge.build_nudge_text(target_description))
            return

        logger.info("Fresh frame: %d bytes [%s]", len(frame_bytes), debug_label or "perception")

        # Save to disk for debugging
        self._save_debug_frame(frame_bytes, debug_label or "perception")

        # Send frame + nudge together via send_client_content
        nudge = PerceptionBridge.build_nudge_text(target_description)
        if preamble:
            nudge = preamble + "\n\n" + nudge

        if self._send_frame_with_prompt and self._loop:
            import asyncio

            future = asyncio.run_coroutine_threadsafe(
                self._send_frame_with_prompt([frame_bytes], nudge), self._loop,
            )
            try:
                future.result(timeout=5.0)
                self._track_send_success()
            except Exception:
                logger.warning("Failed to send frame+nudge to Gemini", exc_info=True)
                self._track_send_failure()
        else:
            # Fallback: send separately (less reliable)
            if self._send_video and self._loop:
                import asyncio

                frame_b64 = base64.b64encode(frame_bytes).decode("ascii")
                future = asyncio.run_coroutine_threadsafe(
                    self._send_video(frame_b64), self._loop,
                )
                try:
                    future.result(timeout=5.0)
                except Exception:
                    logger.warning("Failed to send fresh frame", exc_info=True)
            time.sleep(0.5)
            self._send_text_sync(nudge)

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

    def run(
        self,
        target_description: str,
        aspects: str | None = None,
        needs_search: bool = False,
    ) -> Mission:
        """Execute the full inspection mission (called from background thread).

        Phases: [searching →] approaching → inspecting → complete/aborted
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
        self._init_debug_dir()

        try:
            # Ensure drone is flying (needed for both search and approach paths)
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

            # --- Phase 1: Search (optional) ---
            if needs_search:
                sm.transition(MissionStatus.SEARCHING)
                self._notify_status()
                logger.info("Starting search phase for '%s'", target_description)

                found = self._run_search_phase(target_description)
                if not found:
                    self._send_text_sync(
                        f"Could not find '{target_description}' after a full 360-degree scan. "
                        f"Manual control restored."
                    )
                    sm.try_transition(MissionStatus.ABORTED)
                    self._notify_status()
                    return self._mission

                self._check_abort()

            # --- Phase 2: Approach ---
            sm.transition(MissionStatus.APPROACHING)
            self._notify_status()
            logger.info(
                "Inspection approach started: '%s' (aspects=%s)",
                target_description, aspects,
            )

            self._send_text_sync(
                "Starting approach phase. "
                "Use report_perception to tell me EXACTLY where the target "
                "is in the current frame. This is required after every movement.",
            )
            self._run_approach_phase(target_description)

            self._check_abort()

            # --- Phase 3: Lateral strafe inspection ---
            sm.transition(MissionStatus.INSPECTING)
            self._notify_status()

            self._run_inspection_phase(target_description, aspects)

            sm.transition(MissionStatus.COMPLETE)
            self._notify_status()
            logger.info("Inspection mission completed — drone hovering for follow-up")
            size_info = ""
            if self._mission and self._mission.final_relative_size:
                size_info = (
                    f" The target filled ~{int(self._mission.final_relative_size * 100)}% of the frame "
                    f"at final approach. Use small movements (20-30cm) for follow-up."
                )
            self._send_text_sync(
                "Inspection complete. You can now use move_drone and rotate_drone freely." + size_info
            )
            return self._mission

        except RuntimeError as e:
            logger.warning("Inspection mission error: %s", e)
            sm.try_transition(MissionStatus.ABORTED)
            self._perception.deactivate()
            self._notify_status()
            self._send_text_sync("Inspection stopped. Manual control restored.")
            return self._mission

        except Exception:
            logger.exception("Unexpected error in inspection mission")
            sm.try_transition(MissionStatus.ABORTED)
            self._perception.deactivate()
            self._notify_status()
            self._send_text_sync("Inspection stopped. Manual control restored.")
            if self._controller.state.is_flying:
                self._controller.emergency_land()
            return self._mission

    # ------------------------------------------------------------------
    # Phase 1: Deterministic search scan
    # ------------------------------------------------------------------

    def _run_search_phase(self, target_description: str) -> bool:
        """Deterministic 360-degree scan to find the target.

        Rotates SEARCH_ROTATION_STEP degrees at each position, sends fresh
        frames, and waits for perception. Returns True if target found.
        """
        cfg = self._config
        self._perception.activate()
        if self._streamer:
            self._streamer.pause_perception_stream()

        try:
            for position in range(cfg.SEARCH_MAX_POSITIONS):
                self._check_abort()

                # Rotate (skip on first position — use current heading)
                if position > 0:
                    self._controller.rotate("clockwise", cfg.SEARCH_ROTATION_STEP)
                    self._log_command(f"Rotate CW {cfg.SEARCH_ROTATION_STEP}° (search scan)")
                    time.sleep(cfg.APPROACH_ROTATE_DELAY)

                # Send fresh frame with narration + perception nudge in single turn
                self._send_fresh_frame_sync(
                    target_description,
                    debug_label=f"search_pos{position + 1}",
                    preamble=f"Scanning position {position + 1}/{cfg.SEARCH_MAX_POSITIONS}...",
                )

                # Wait for perception
                perception = self._perception.wait_for_perception(
                    timeout=cfg.SEARCH_PERCEPTION_TIMEOUT,
                )

                # Retry once with nudge if no response
                if perception is None:
                    logger.info("No perception at position %d — retrying with nudge", position + 1)
                    self._send_fresh_frame_sync(
                        target_description, debug_label=f"search_pos{position + 1}_retry",
                    )
                    perception = self._perception.wait_for_perception(timeout=5.0)

                if perception is None:
                    logger.info("No perception at position %d", position + 1)
                    continue

                logger.info(
                    "Search position %d: visible=%s conf=%.2f size=%.3f",
                    position + 1, perception.target_visible,
                    perception.confidence, perception.relative_size,
                )

                if (
                    perception.target_visible
                    and perception.confidence >= cfg.SEARCH_MIN_CONFIDENCE
                ):
                    self._send_text_sync(
                        f"Target spotted at position {position + 1}! "
                        f"Confidence: {perception.confidence:.0%}. Starting approach."
                    )
                    logger.info("Target found at search position %d", position + 1)
                    return True

            # Full sweep complete, not found
            logger.warning("Target not found after full 360-degree scan")
            return False

        finally:
            self._perception.deactivate()
            if self._streamer:
                self._streamer.resume_perception_stream()

    # ------------------------------------------------------------------
    # Phase 2: Perception-guided approach
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

    def _search_recovery(
        self,
        target_description: str,
        step: int,
        cfg: ClientConfig,
    ) -> bool:
        """3-step search recovery: CCW → CW → CCW (net heading change = 0).

        Returns True if target re-acquired during recovery.
        """
        deg = cfg.INSPECTION_SEARCH_RECOVERY_DEG

        # Step 1: Rotate CCW
        logger.info("Search recovery step 1: CCW %d°", deg)
        self._controller.rotate("counter_clockwise", deg, delay_override=0.0)
        self._log_command(f"Rotate CCW {deg}° (search recovery step 1)")
        time.sleep(cfg.APPROACH_ROTATE_DELAY)
        self._send_fresh_frame_sync(
            target_description, debug_label=f"approach_recovery1_step{step}",
        )
        perception = self._perception.wait_for_perception(timeout=5.0)
        if perception and perception.target_visible and perception.confidence >= 0.3:
            logger.info("Search recovery: target re-acquired after CCW")
            return True

        # Step 2: Rotate CW (double, to go past original heading)
        logger.info("Search recovery step 2: CW %d°", deg * 2)
        self._controller.rotate("clockwise", deg * 2, delay_override=0.0)
        self._log_command(f"Rotate CW {deg * 2}° (search recovery step 2)")
        time.sleep(cfg.APPROACH_ROTATE_DELAY)
        self._send_fresh_frame_sync(
            target_description, debug_label=f"approach_recovery2_step{step}",
        )
        perception = self._perception.wait_for_perception(timeout=5.0)
        if perception and perception.target_visible and perception.confidence >= 0.3:
            logger.info("Search recovery: target re-acquired after CW")
            return True

        # Step 3: Restore original heading (CCW back)
        logger.info("Search recovery step 3: CCW %d° (restore heading)", deg)
        self._controller.rotate("counter_clockwise", deg, delay_override=0.0)
        self._log_command(f"Rotate CCW {deg}° (search recovery step 3, restore heading)")
        time.sleep(cfg.APPROACH_ROTATE_DELAY)
        logger.warning("Search recovery: target NOT re-acquired")
        return False

    def _run_approach_phase(self, target_description: str) -> None:
        """Approach the target using perception feedback from Gemini.

        Uses EMA smoothing, stale-data rejection with self-correcting recovery,
        size-adaptive forward distance, centering gate, and stagnation detection.
        """
        cfg = self._config
        self._perception.activate()
        if self._streamer:
            self._streamer.pause_perception_stream()

        try:
            # Send fresh frame + nudge to start perception
            self._send_fresh_frame_sync(target_description, debug_label="approach_start")

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

            # Stagnation detection
            last_forward_size: float | None = None
            consecutive_no_growth = 0

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
                    # Nudge Gemini with a fresh frame and retry once
                    logger.info("No perception received — nudging Gemini (step %d)", step)
                    self._send_fresh_frame_sync(
                        target_description, debug_label=f"approach_blind_step{step}",
                    )
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

                # Target not visible or low confidence — 3-step search recovery
                if not perception.target_visible or perception.confidence < 0.3:
                    logger.info("Target not visible/low confidence — search recovery")
                    self._search_recovery(target_description, step, cfg)
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
                        self._controller.rotate(direction, 10, delay_override=0.0)
                        dir_label = "CW" if direction == "clockwise" else "CCW"
                        self._log_command(f"Rotate {dir_label} 10° (stale data recovery)")
                        stale_recovery_offset += 10 if direction == "clockwise" else -10
                        time.sleep(cfg.APPROACH_ROTATE_DELAY)
                        consecutive_stale = 0
                    self._send_fresh_frame_sync(
                        target_description, debug_label=f"approach_stale_step{step}",
                    )
                    continue  # Don't move forward on stale data

                # --- Fresh data — reset stale state and undo heading drift ---
                consecutive_stale = 0
                last_raw_perception = perception

                if stale_recovery_offset != 0:
                    undo_dir = (
                        "counter_clockwise" if stale_recovery_offset > 0
                        else "clockwise"
                    )
                    undo_deg = abs(stale_recovery_offset)
                    self._controller.rotate(
                        undo_dir, undo_deg, delay_override=0.0,
                    )
                    undo_label = "CCW" if undo_dir == "counter_clockwise" else "CW"
                    self._log_command(f"Rotate {undo_label} {undo_deg}° (undo stale drift)")
                    stale_recovery_offset = 0
                    time.sleep(cfg.APPROACH_ROTATE_DELAY)

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

                # --- Centering gate: strafe (close) or rotate (far) before moving forward ---
                if abs(smoothed_h_off) > cfg.INSPECTION_H_DEADBAND:
                    strafe_zone = (
                        smoothed_size is not None
                        and smoothed_size >= cfg.INSPECTION_STRAFE_ZONE_THRESHOLD
                    )
                    if strafe_zone:
                        # Close zone: lateral strafe (no heading change)
                        strafe_cm = int(abs(smoothed_h_off) * cfg.INSPECTION_KP_LATERAL)
                        strafe_cm = max(
                            cfg.INSPECTION_MIN_STRAFE,
                            min(strafe_cm, cfg.INSPECTION_MAX_STRAFE),
                        )
                        direction = "right" if smoothed_h_off > 0 else "left"
                        logger.info(
                            "Strafe centering: %s %dcm (smoothed_h=%.2f, size=%.3f)",
                            direction, strafe_cm, smoothed_h_off, smoothed_size,
                        )
                        self._controller.move(direction, strafe_cm, delay_override=0.0)
                        self._log_command(
                            f"Strafe {direction} {strafe_cm}cm "
                            f"(centering, h={smoothed_h_off:.2f})"
                        )
                        time.sleep(cfg.APPROACH_MOVE_DELAY)
                        self._send_fresh_frame_sync(
                            target_description, debug_label=f"approach_strafe_step{step}",
                        )
                        continue
                    else:
                        # Far zone: rotation centering
                        rotate_deg = int(abs(smoothed_h_off) * cfg.INSPECTION_ROTATION_GAIN)
                        rotate_deg = max(cfg.MIN_ROTATION, min(rotate_deg, 45))
                        direction = (
                            "clockwise" if smoothed_h_off > 0
                            else "counter_clockwise"
                        )
                        logger.info(
                            "Rotation centering: %s %d° (smoothed_h=%.2f)",
                            direction, rotate_deg, smoothed_h_off,
                        )
                        self._controller.rotate(direction, rotate_deg, delay_override=0.0)
                        dir_label = "CW" if direction == "clockwise" else "CCW"
                        self._log_command(
                            f"Rotate {dir_label} {rotate_deg}° "
                            f"(centering, h={smoothed_h_off:.2f})"
                        )
                        time.sleep(cfg.APPROACH_ROTATE_DELAY)
                        self._send_fresh_frame_sync(
                            target_description, debug_label=f"approach_rotate_step{step}",
                        )
                        continue

                # --- Vertical correction ---
                if smoothed_v_off is not None and abs(smoothed_v_off) > cfg.INSPECTION_V_DEADBAND:
                    vert_cm = int(abs(smoothed_v_off) * cfg.INSPECTION_KP_VERTICAL)
                    if vert_cm >= cfg.INSPECTION_SKIP_VERTICAL_CM:
                        vert_cm = max(
                            cfg.INSPECTION_MIN_VERTICAL,
                            min(vert_cm, cfg.INSPECTION_MAX_VERTICAL),
                        )
                        # v_offset > 0 means target is low in frame → move down
                        vert_dir = "down" if smoothed_v_off > 0 else "up"
                        logger.info(
                            "Vertical correction: %s %dcm (smoothed_v=%.2f)",
                            vert_dir, vert_cm, smoothed_v_off,
                        )
                        self._controller.move(vert_dir, vert_cm, delay_override=0.0)
                        self._log_command(
                            f"Move {vert_dir} {vert_cm}cm "
                            f"(vertical, v={smoothed_v_off:.2f})"
                        )
                        time.sleep(cfg.APPROACH_MOVE_DELAY)
                        self._send_fresh_frame_sync(
                            target_description, debug_label=f"approach_vert_step{step}",
                        )
                        continue

                # --- Close enough? (raw reading OR smoothed) ---
                if (
                    perception.relative_size >= cfg.INSPECTION_APPROACH_SIZE_THRESHOLD
                    and perception.confidence >= 0.7
                ):
                    logger.info(
                        "Target close enough (raw_size=%.3f >= %.3f, conf=%.2f)",
                        perception.relative_size,
                        cfg.INSPECTION_APPROACH_SIZE_THRESHOLD,
                        perception.confidence,
                    )
                    break
                if smoothed_size >= cfg.INSPECTION_APPROACH_SIZE_THRESHOLD:
                    logger.info(
                        "Target close enough (smoothed_size=%.3f >= %.3f)",
                        smoothed_size, cfg.INSPECTION_APPROACH_SIZE_THRESHOLD,
                    )
                    break

                # --- Size-adaptive forward distance ---
                if smoothed_size < 0.10:
                    forward_cm = cfg.INSPECTION_FORWARD_FAR
                elif smoothed_size < 0.15:
                    forward_cm = cfg.INSPECTION_FORWARD_MEDIUM
                else:
                    forward_cm = cfg.INSPECTION_FORWARD_CLOSE

                logger.info(
                    "Moving forward %dcm (smoothed_size=%.3f)", forward_cm, smoothed_size,
                )
                result = self._controller.move("forward", forward_cm, delay_override=0.0)
                self._log_command(
                    f"Forward {forward_cm}cm (size: {smoothed_size:.3f})"
                )
                if not result.get("success"):
                    logger.error("Forward move failed: %s", result)
                    raise RuntimeError(
                        f"Movement failed: {result.get('message', 'unknown')}",
                    )
                time.sleep(cfg.APPROACH_MOVE_DELAY)

                # --- Stagnation detection ---
                if last_forward_size is not None and smoothed_size is not None:
                    growth = smoothed_size - last_forward_size
                    if growth < cfg.INSPECTION_STAGNATION_THRESHOLD:
                        consecutive_no_growth += 1
                        logger.info(
                            "Stagnation: growth=%.4f < %.4f (count=%d/%d)",
                            growth, cfg.INSPECTION_STAGNATION_THRESHOLD,
                            consecutive_no_growth, cfg.INSPECTION_STAGNATION_LIMIT,
                        )
                        if consecutive_no_growth >= cfg.INSPECTION_STAGNATION_LIMIT:
                            logger.info(
                                "Stagnation limit reached — as close as we can get. "
                                "Proceeding to inspection."
                            )
                            self._send_text_sync(
                                "Target size is not growing — this is as close as we can get. "
                                "Proceeding to inspection at current distance."
                            )
                            break
                    else:
                        consecutive_no_growth = 0
                last_forward_size = smoothed_size

                # --- Post-movement: send fresh frame + nudge ---
                self._send_fresh_frame_sync(
                    target_description, debug_label=f"approach_fwd_step{step}",
                )

            else:
                logger.info(
                    "Max approach steps (%d) reached. Proceeding to inspection.",
                    cfg.INSPECTION_MAX_APPROACH_STEPS,
                )

            # Store final size for post-inspection move clamping
            if self._mission and smoothed_size is not None:
                self._mission.final_relative_size = smoothed_size

        finally:
            self._perception.deactivate()
            if self._streamer:
                self._streamer.resume_perception_stream()

    # ------------------------------------------------------------------
    # Phase 3: Orbit arc inspection
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_orbit_arc(
        radius: int, angle_deg: int, side: str,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """Compute curve_xyz_speed midpoint and endpoint for an orbit arc.

        The drone orbits around a target at distance *radius*.
        Circle centered at (radius, 0) — the target is at (radius, 0) in drone-local coords.
        At angle θ from origin: position = (R - R*cos(θ), ±R*sin(θ), 0).

        Args:
            radius: orbit radius in cm.
            angle_deg: arc angle in degrees.
            side: "right" (negative y = rightward in Tello frame) or "left" (positive y).

        Returns:
            ((mid_x, mid_y, mid_z), (end_x, end_y, end_z)) — integer cm values.
        """
        half = math.radians(angle_deg / 2)
        full = math.radians(angle_deg)

        mid_x = round(radius * (1 - math.cos(half)))
        mid_y = round(radius * math.sin(half))
        end_x = round(radius * (1 - math.cos(full)))
        end_y = round(radius * math.sin(full))

        # Right orbit = negative y (rightward in Tello frame)
        if side == "right":
            mid_y = -mid_y
            end_y = -end_y

        return (mid_x, mid_y, 0), (end_x, end_y, 0)

    def _run_inspection_phase(
        self, target_description: str, aspects: str | None,
    ) -> None:
        """Capture frames from 3 perspectives using 45-degree orbit arcs."""
        cfg = self._config
        radius = cfg.INSPECTION_ORBIT_RADIUS
        angle = cfg.INSPECTION_ORBIT_ANGLE
        speed = cfg.INSPECTION_ORBIT_SPEED
        stabilize = cfg.INSPECTION_ORBIT_STABILIZE
        captured_frames: list[bytes] = []
        captured_labels: list[str] = []

        # 1. Front close-up — capture at current position
        self._check_abort()
        time.sleep(stabilize)
        frame = self._streamer.get_fresh_dashboard_frame(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append("front close-up")
            logger.info("Captured front close-up (%d bytes)", len(frame))
        else:
            logger.warning("No frame for front close-up")

        # 2. Right orbit arc → rotate to face target → capture
        self._check_abort()
        mid_r, end_r = self._compute_orbit_arc(radius, angle, "right")
        self._send_text_sync(f"Orbiting {angle}° right for angled view.")
        logger.info("Right orbit arc: mid=%s end=%s speed=%d", mid_r, end_r, speed)
        result = self._controller.curve(
            *mid_r, *end_r, speed, delay_override=0.0,
        )
        self._log_command(f"Curve right {angle}° orbit (inspection: right-angled view)")
        if not result.get("success"):
            logger.error("Right orbit curve failed: %s", result)
            raise RuntimeError(f"Right orbit curve failed: {result.get('message', 'unknown')}")
        # Rotate to face target after arc
        self._controller.rotate("counter_clockwise", angle, delay_override=0.0)
        self._log_command(f"Rotate CCW {angle}° (face target after right orbit)")
        time.sleep(stabilize)

        frame = self._streamer.get_fresh_dashboard_frame(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append("right-angled view")
            logger.info("Captured right-angled view (%d bytes)", len(frame))
        else:
            logger.warning("No frame for right-angled view")

        # 3. Return from right orbit to center
        self._check_abort()
        self._controller.rotate("clockwise", angle, delay_override=0.0)
        self._log_command(f"Rotate CW {angle}° (restore heading)")
        # Reverse curve: from endpoint back to origin
        rev_mid = (mid_r[0] - end_r[0], mid_r[1] - end_r[1], 0)
        rev_end = (-end_r[0], -end_r[1], 0)
        result = self._controller.curve(
            *rev_mid, *rev_end, speed, delay_override=0.0,
        )
        self._log_command("Reverse curve (return to center from right)")
        if not result.get("success"):
            logger.warning("Return-from-right curve failed: %s", result)
        time.sleep(stabilize)

        # 4. Left orbit arc → rotate to face target → capture
        self._check_abort()
        mid_l, end_l = self._compute_orbit_arc(radius, angle, "left")
        self._send_text_sync(f"Orbiting {angle}° left for opposite angled view.")
        logger.info("Left orbit arc: mid=%s end=%s speed=%d", mid_l, end_l, speed)
        result = self._controller.curve(
            *mid_l, *end_l, speed, delay_override=0.0,
        )
        self._log_command(f"Curve left {angle}° orbit (inspection: left-angled view)")
        if not result.get("success"):
            logger.error("Left orbit curve failed: %s", result)
            raise RuntimeError(f"Left orbit curve failed: {result.get('message', 'unknown')}")
        # Rotate to face target after arc
        self._controller.rotate("clockwise", angle, delay_override=0.0)
        self._log_command(f"Rotate CW {angle}° (face target after left orbit)")
        time.sleep(stabilize)

        frame = self._streamer.get_fresh_dashboard_frame(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append("left-angled view")
            logger.info("Captured left-angled view (%d bytes)", len(frame))
        else:
            logger.warning("No frame for left-angled view")

        # 5. Return from left orbit to center
        self._check_abort()
        self._controller.rotate("counter_clockwise", angle, delay_override=0.0)
        self._log_command(f"Rotate CCW {angle}° (restore heading)")
        rev_mid = (mid_l[0] - end_l[0], mid_l[1] - end_l[1], 0)
        rev_end = (-end_l[0], -end_l[1], 0)
        result = self._controller.curve(
            *rev_mid, *rev_end, speed, delay_override=0.0,
        )
        self._log_command("Reverse curve (return to center from left)")
        if not result.get("success"):
            logger.warning("Return-from-left curve failed: %s", result)

        if not captured_frames:
            logger.error("No frames captured during inspection")
            if self._sm:
                self._sm.try_transition(MissionStatus.ABORTED)
            self._notify_status()
            return

        # Send batch frames for comprehensive summary
        self._send_text_sync(
            f"Captured {len(captured_frames)} perspectives. Sending for analysis."
        )
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

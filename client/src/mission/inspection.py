"""Inspection mission — approach target with perception loop + lateral strafe views.

Three phases (when needs_search=True):
1. SEARCHING: Deterministic 360-degree scan using Flash API (visual_client) to find target.
2. APPROACHING: Use perception bridge to center on and approach the target
   until it fills enough of the frame (or max steps / stagnation / blind limit).
3. INSPECTING: Lateral strafe + rotation to capture 3 perspectives (front, right-angled,
   left-angled) then send batch to Gemini for comprehensive verbal summary.

When needs_search=False, skips phase 1.
Does NOT auto-land — stays hovering for follow-up commands.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from client.src.config import ClientConfig
from client.src.mission.perception_bridge import PerceptionBridge
from client.src.models.mission import Mission, MissionStateMachine, MissionStatus, MissionType
from client.src.perception.visual import PerceptionResponse, RateLimitError

if TYPE_CHECKING:
    from client.src.drone.controller import DroneController
    from client.src.video.frame_streamer import FrameStreamer

logger = logging.getLogger(__name__)


@dataclass
class ReportCollector:
    """Collects frames and metadata during inspection for post-mission report."""

    acquisition_frame: bytes | None = None
    inspection_frames: list[tuple[bytes, str]] = field(default_factory=list)
    phases_completed: list[str] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    inspection_report: Any = None  # InspectionReport from visual.py


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
        visual_client: Any = None,
        send_text_fn: Any = None,
        on_status_change: Any = None,
        on_command_log: Any = None,
        on_perception_broadcast: Any = None,
        on_ai_activity: Any = None,
    ) -> None:
        self._controller = controller
        self._streamer = frame_streamer
        self._config = config
        self._perception = perception_bridge
        self._visual_client = visual_client
        self._send_text = send_text_fn
        self._on_status_change = on_status_change
        self._on_command_log = on_command_log
        self._on_perception_broadcast = on_perception_broadcast
        self._on_ai_activity = on_ai_activity
        self._abort_event = threading.Event()
        self._mission: Mission | None = None
        self._sm: MissionStateMachine | None = None
        self._loop: Any = None
        self._debug_frame_dir: Path | None = None
        self._debug_frame_counter: int = 0
        self._consecutive_send_failures: int = 0
        self._max_send_failures: int = 3
        self._report = ReportCollector()

    @property
    def mission(self) -> Mission | None:
        return self._mission

    @property
    def report(self) -> ReportCollector:
        return self._report

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

    def _broadcast_approach_perception(
        self, response: Any, h_off: float, v_off: float, rel_size: float,
    ) -> None:
        """Broadcast approach perception data to dashboard overlay."""
        if not self._on_perception_broadcast:
            return
        from client.src.models.tool_calls import DashboardPerception

        perc = DashboardPerception.from_visual_perception(response, h_off, v_off, rel_size)
        try:
            self._on_perception_broadcast(perc)
        except Exception:
            logger.debug("Perception broadcast error", exc_info=True)

    def _broadcast_ai_activity(self, call_type: str, model: str = "gemini-2.5-flash") -> None:
        """Broadcast AI activity event for dashboard API cost tracking."""
        if self._on_ai_activity:
            try:
                self._on_ai_activity({"model": model, "call_type": call_type, "source": "flash"})
            except Exception:
                logger.debug("AI activity broadcast error", exc_info=True)

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
        if self._send_text and self._loop and not self._loop.is_closed():
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

    def _check_abort(self) -> None:
        """Raise if abort requested."""
        if self._abort_event.is_set():
            raise RuntimeError("Inspection aborted by user")

    def _interruptible_sleep(self, duration: float, interval: float = 0.25) -> None:
        """Sleep in small intervals, checking abort between each."""
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if self._abort_event.is_set():
                raise RuntimeError("Inspection aborted by user")
            time.sleep(min(interval, end - time.monotonic()))

    def run(
        self,
        target_description: str,
        aspects: str | None = None,
        needs_search: bool = False,
        viewing_angle: str = "front",
    ) -> Mission:
        """Execute the full inspection mission (called from background thread).

        Phases: [searching →] approaching → inspecting → complete/aborted
        """
        self._report = ReportCollector(started_at=time.time())
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

            # Pause perception stream for mission phases (resumed in finally)
            if self._streamer:
                self._streamer.pause_perception_stream()

            # Inform Gemini of flight status — perception stream is paused so
            # the model may still see stale pre-takeoff frames.
            altitude = self._controller.state.altitude or 70
            self._send_text_sync(
                f"The drone is flying at {altitude}cm altitude. "
                f"Video perception is paused during autonomous approach — "
                f"rely on my text updates for status, not the video feed."
            )

            # Notify Gemini about the inspection
            self._send_text_sync(
                f"Starting inspection of: {target_description}. "
                f"I will approach the target and then capture frames from multiple "
                f"angles for a detailed assessment."
                + (f" Focus on: {aspects}." if aspects else ""),
            )

            # --- Phase 1: Search (optional) ---
            # NOTE: heartbeat stays running during search — the search phase has long
            # gaps between drone commands (Gemini API calls ~10-15s) and pausing
            # the heartbeat would trigger Tello's 15s auto-land (lesson A4).
            # The heartbeat uses non-blocking lock acquire so it won't conflict
            # with mission commands (lesson C5).
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
                self._report.phases_completed.append("search")

            # Capture acquisition frame if not already captured (search skipped)
            if self._report.acquisition_frame is None and self._streamer:
                acq = self._streamer.get_fresh_perception_frame_bytes(timeout=3.0)
                if acq:
                    self._report.acquisition_frame = acq
                    logger.info(
                        "Report: saved acquisition frame at approach start (%d bytes)", len(acq),
                    )

            # Pre-approach verification: when search was skipped, confirm Flash
            # can see the target before committing to approach.
            if not needs_search and self._visual_client and self._streamer:
                verify_frame = self._streamer.get_fresh_perception_frame_bytes(
                    timeout=3.0, min_new_frames=30,
                )
                if verify_frame:
                    try:
                        verify_resp = self._visual_client.detect(
                            verify_frame, target_description,
                        )
                        self._broadcast_ai_activity("search_detect")
                    except RateLimitError:
                        verify_resp = PerceptionResponse(
                            target_visible=False, confidence=0.0,
                        )
                    logger.info(
                        "Pre-approach verify: visible=%s conf=%.2f",
                        verify_resp.target_visible, verify_resp.confidence,
                    )
                    if (
                        not verify_resp.target_visible
                        or verify_resp.confidence < self._config.SEARCH_MIN_CONFIDENCE
                    ):
                        logger.warning(
                            "Target not confirmed by Flash — falling back to search",
                        )
                        self._send_text_sync(
                            "I cannot confirm the target visually. "
                            "Running a search scan.",
                        )
                        sm.transition(MissionStatus.SEARCHING)
                        self._notify_status()
                        found = self._run_search_phase(target_description)
                        if not found:
                            self._send_text_sync(
                                f"Could not find '{target_description}' after a "
                                f"full 360-degree scan. Manual control restored.",
                            )
                            sm.try_transition(MissionStatus.ABORTED)
                            self._notify_status()
                            return self._mission
                        self._check_abort()
                        self._report.phases_completed.append("search")

            # Pause heartbeat during approach/inspection — rapid drone commands keep
            # connection alive and heartbeat could cause UDP response interleaving.
            self._controller.executor.pause_heartbeat()

            # --- Phase 2: Approach ---
            sm.transition(MissionStatus.APPROACHING)
            self._notify_status()
            logger.info(
                "Inspection approach started: '%s' (aspects=%s)",
                target_description, aspects,
            )

            self._send_text_sync(
                "Starting approach phase. I will use visual perception to guide "
                "the drone toward the target automatically.",
            )
            self._run_approach_phase(target_description)

            self._check_abort()
            self._report.phases_completed.append("approach")

            # --- Final centering before inspection ---
            self._final_centering(target_description)
            self._check_abort()

            # --- L-maneuver: reposition for requested viewing angle ---
            if viewing_angle != "front":
                sm.transition(MissionStatus.REPOSITIONING)
                self._notify_status()
                self._run_l_maneuver(target_description, viewing_angle)
                self._check_abort()
                self._report.phases_completed.append("reposition")

            # --- Phase 3: Lateral strafe inspection ---
            sm.transition(MissionStatus.INSPECTING)
            self._notify_status()

            self._run_inspection_phase(target_description, aspects, viewing_angle)

            self._report.phases_completed.append("inspect")
            self._report.finished_at = time.time()
            if not sm.try_transition(MissionStatus.COMPLETE):
                logger.warning(
                    "Could not transition to COMPLETE (current=%s) — likely aborted concurrently",
                    sm.status.value,
                )
                return self._mission
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
            # Generate partial report from any frames captured before the failure
            if self._report.inspection_frames and self._visual_client:
                try:
                    frames = [f for f, _ in self._report.inspection_frames]
                    labels = [lbl for _, lbl in self._report.inspection_frames]
                    self._report.inspection_report = self._visual_client.generate_report(
                        frames, labels, target_description, aspects,
                    )
                    self._send_text_sync(
                        f"Inspection error: {e}. Generated partial report from "
                        f"{len(frames)} perspective(s). Manual control restored."
                    )
                except Exception:
                    logger.warning("Partial report generation failed", exc_info=True)
                    self._send_text_sync("Inspection stopped. Manual control restored.")
            else:
                self._send_text_sync("Inspection stopped. Manual control restored.")
            return self._mission

        except Exception:
            logger.exception("Unexpected error in inspection mission")
            sm.try_transition(MissionStatus.ABORTED)
            self._perception.deactivate()
            self._notify_status()
            # Generate partial report from any frames captured before the failure
            if self._report.inspection_frames and self._visual_client:
                try:
                    frames = [f for f, _ in self._report.inspection_frames]
                    labels = [lbl for _, lbl in self._report.inspection_frames]
                    self._report.inspection_report = self._visual_client.generate_report(
                        frames, labels, target_description, aspects,
                    )
                    self._send_text_sync(
                        f"Inspection failed but generated partial report from "
                        f"{len(frames)} perspective(s). Manual control restored."
                    )
                except Exception:
                    logger.warning("Partial report generation failed", exc_info=True)
                    self._send_text_sync("Inspection stopped. Manual control restored.")
            else:
                self._send_text_sync("Inspection stopped. Manual control restored.")
            if self._controller.state.is_flying:
                self._controller.emergency_land()
            return self._mission

        finally:
            self._controller.executor.resume_heartbeat()
            if self._streamer:
                self._streamer.resume_perception_stream()

    # ------------------------------------------------------------------
    # Phase 1: Deterministic search scan
    # ------------------------------------------------------------------

    def _run_search_phase(self, target_description: str) -> bool:
        """Deterministic 360-degree scan to find the target.

        Rotates SEARCH_ROTATION_STEP degrees at each position, uses Flash API
        (visual_client.detect) for perception. Returns True if target found.
        """
        cfg = self._config
        total = cfg.SEARCH_MAX_POSITIONS

        for position in range(total):
            self._check_abort()

            # Detect if drone auto-landed (Tello 15s timeout — lesson A4)
            if not self._controller.state.is_flying:
                logger.error("Drone no longer flying during search — aborting")
                self._send_text_sync(
                    "Search aborted — drone is no longer airborne. "
                    "It may have auto-landed due to inactivity."
                )
                return False

            # Rotate (skip on first position — use current heading)
            if position > 0:
                result = self._controller.rotate("clockwise", cfg.SEARCH_ROTATION_STEP)
                if not result.get("success"):
                    logger.error("Search rotation failed: %s — aborting", result)
                    self._send_text_sync("Search aborted — rotation command failed.")
                    return False
                self._log_command(f"Rotate CW {cfg.SEARCH_ROTATION_STEP}° (search scan)")
                self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)

            self._check_abort()

            # Get fresh frame and run Flash API detection
            frame_bytes = self._streamer.get_fresh_perception_frame_bytes(
                timeout=3.0, min_new_frames=30 if position > 0 else 10,
            )
            if not frame_bytes:
                logger.warning("No fresh frame at search position %d", position + 1)
                continue

            self._save_debug_frame(frame_bytes, f"search_pos{position + 1}")

            try:
                response = self._visual_client.detect(frame_bytes, target_description)
                self._broadcast_ai_activity("search_detect")
            except RateLimitError as e:
                logger.warning(
                    "Rate limited at search position %d — waiting %.0fs and retrying",
                    position + 1, e.retry_after,
                )
                self._send_text_sync(
                    f"Rate limited at position {position + 1} of {total}. "
                    f"Waiting {int(e.retry_after)} seconds before retrying."
                )
                self._interruptible_sleep(min(e.retry_after, 60.0))
                # Retry once with a fresh frame
                frame_bytes = self._streamer.get_fresh_perception_frame_bytes(
                    timeout=3.0, min_new_frames=10,
                )
                if not frame_bytes:
                    continue
                try:
                    response = self._visual_client.detect(frame_bytes, target_description)
                    self._broadcast_ai_activity("search_detect")
                except RateLimitError:
                    logger.warning("Still rate limited — skipping position %d", position + 1)
                    continue

            logger.info(
                "Search position %d/%d: visible=%s conf=%.2f",
                position + 1, total, response.target_visible, response.confidence,
            )

            if (
                response.target_visible
                and response.confidence >= cfg.SEARCH_MIN_CONFIDENCE
            ):
                self._send_text_sync(
                    f"Target spotted at position {position + 1}! Starting approach."
                )
                logger.info("Target found at search position %d", position + 1)
                # Capture acquisition frame for report
                acq = self._streamer.get_fresh_perception_frame_bytes(timeout=3.0)
                if acq:
                    self._report.acquisition_frame = acq
                    logger.info("Report: saved acquisition frame (%d bytes)", len(acq))
                return True

            # Narrate + pause for Live API to speak naturally
            self._send_text_sync(
                f"Scanning position {position + 1} of {total}... "
                f"no target visible here."
            )
            self._interruptible_sleep(cfg.SEARCH_POST_DETECT_DELAY)

        # Full sweep complete, not found
        logger.warning("Target not found after full 360-degree scan")
        return False

    # ------------------------------------------------------------------
    # Phase 2: Perception-guided approach
    # ------------------------------------------------------------------

    def _search_recovery(
        self,
        target_description: str,
        step: int,
        cfg: ClientConfig,
    ) -> bool:
        """3-step search recovery: CCW → CW → CCW (net heading change = 0).

        Uses Flash API (visual_client.detect) instead of Live API perception.
        Returns True if target re-acquired during recovery.
        """
        deg = cfg.INSPECTION_SEARCH_RECOVERY_DEG

        # Step 1: Rotate CCW
        logger.info("Search recovery step 1: CCW %d°", deg)
        self._controller.rotate("counter_clockwise", deg)
        self._log_command(f"Rotate CCW {deg}° (search recovery step 1)")
        self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)
        frame_bytes = self._streamer.get_fresh_perception_frame_bytes(
            timeout=3.0, min_new_frames=30,
        )
        if frame_bytes:
            self._save_debug_frame(frame_bytes, f"approach_recovery1_step{step}")
            try:
                response = self._visual_client.detect(frame_bytes, target_description)
            except RateLimitError:
                logger.warning("Rate limited during search recovery step 1")
                response = PerceptionResponse(target_visible=False, confidence=0.0)
            if response.target_visible and response.confidence >= 0.3:
                logger.info("Search recovery: target re-acquired after CCW")
                return True

        # Step 2: Rotate CW (double, to go past original heading)
        logger.info("Search recovery step 2: CW %d°", deg * 2)
        self._controller.rotate("clockwise", deg * 2)
        self._log_command(f"Rotate CW {deg * 2}° (search recovery step 2)")
        self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)
        frame_bytes = self._streamer.get_fresh_perception_frame_bytes(
            timeout=3.0, min_new_frames=30,
        )
        if frame_bytes:
            self._save_debug_frame(frame_bytes, f"approach_recovery2_step{step}")
            try:
                response = self._visual_client.detect(frame_bytes, target_description)
            except RateLimitError:
                logger.warning("Rate limited during search recovery step 2")
                response = PerceptionResponse(target_visible=False, confidence=0.0)
            if response.target_visible and response.confidence >= 0.3:
                logger.info("Search recovery: target re-acquired after CW")
                return True

        # Step 3: Restore original heading (CCW back)
        logger.info("Search recovery step 3: CCW %d° (restore heading)", deg)
        self._controller.rotate("counter_clockwise", deg)
        self._log_command(f"Rotate CCW {deg}° (search recovery step 3, restore heading)")
        self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)
        logger.warning("Search recovery: target NOT re-acquired")
        return False

    def _run_approach_phase(self, target_description: str) -> None:
        """Approach the target using bounding box perception via generate_content.

        Uses VisualPerceptionClient for accurate box_2d detection, with EMA
        smoothing, size-adaptive forward distance, centering gate, and
        stagnation detection.
        """
        from client.src.perception.visual import compute_offsets

        cfg = self._config

        # EMA smoothing state
        smoothed_size: float | None = None
        smoothed_h_off: float | None = None
        smoothed_v_off: float | None = None
        alpha = 0.5

        # Stagnation detection
        last_forward_size: float | None = None
        consecutive_no_growth = 0
        forward_step_count = 0
        not_visible_count = 0
        watchdog_start = time.monotonic()

        # Supplementary rotation tracking
        consecutive_strafe_no_improvement: int = 0
        prev_h_magnitude: float | None = None

        # Command failure tolerance (lesson J1: COMMAND errors → retry, not abort)
        consecutive_cmd_failures = 0

        # Perception caching — skip detect() if drone didn't move
        moved_this_step = True
        prev_response = None
        recovery_attempts = 0

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

            # Get fresh frame and run visual perception
            frame_bytes = self._streamer.get_fresh_perception_frame_bytes(
                timeout=3.0, min_new_frames=10,
            )
            if not frame_bytes:
                logger.warning("No fresh frame available at step %d", step)
                self._interruptible_sleep(1.0)
                continue

            self._save_debug_frame(frame_bytes, f"approach_step{step}")

            if not moved_this_step and prev_response is not None and prev_response.target_visible:
                response = prev_response
                logger.info("No movement last step — reusing cached perception")
            else:
                try:
                    response = self._visual_client.detect(frame_bytes, target_description)
                    self._broadcast_ai_activity("approach_detect")
                except RateLimitError:
                    logger.warning("Rate limited during approach, treating as not visible")
                    response = PerceptionResponse(target_visible=False, confidence=0.0)
                prev_response = response
            moved_this_step = False  # Reset for this iteration

            logger.info(
                "Approach step %d: visible=%s conf=%.2f box=%s path_clear=%s",
                step, response.target_visible, response.confidence,
                response.box_2d, response.path_clear,
            )

            # Target not visible or low confidence
            if not response.target_visible or response.confidence < 0.3:
                self._broadcast_approach_perception(response, 0.0, 0.0, 0.0)
                not_visible_count += 1
                if not_visible_count >= cfg.INSPECTION_MAX_BLIND_STEPS:
                    logger.info(
                        "Target not visible %d consecutive — search recovery",
                        not_visible_count,
                    )
                    self._send_text_sync(
                        "Lost sight of target, searching nearby."
                    )
                    recovery_attempts += 1
                    if recovery_attempts > cfg.INSPECTION_MAX_RECOVERY_ATTEMPTS:
                        logger.warning(
                            "Recovery attempt %d exceeds limit — falling back to full search",
                            recovery_attempts,
                        )
                        self._send_text_sync(
                            "Multiple recovery attempts failed. Running full 360-degree search."
                        )
                        if self._run_search_phase(target_description):
                            recovery_attempts = 0
                            smoothed_size = None
                            smoothed_h_off = None
                            smoothed_v_off = None
                            last_forward_size = None
                            consecutive_no_growth = 0
                            forward_step_count = 0
                            prev_h_magnitude = None
                            consecutive_strafe_no_improvement = 0
                        else:
                            logger.warning("Full search failed — proceeding to inspection")
                            break
                    else:
                        self._search_recovery(target_description, step, cfg)
                    prev_response = None
                    moved_this_step = True
                    not_visible_count = 0
                continue

            not_visible_count = 0

            # No bounding box despite visible — treat as blind
            if not response.box_2d or len(response.box_2d) != 4:
                logger.warning("Target visible but no box_2d — skipping step")
                continue

            # Compute offsets from bounding box
            h_off, v_off, rel_size = compute_offsets(response.box_2d)

            logger.info(
                "Box offsets: h=%.3f v=%.3f size=%.3f (box=%s)",
                h_off, v_off, rel_size, response.box_2d,
            )

            # --- EMA smoothing ---
            if smoothed_size is None:
                smoothed_size = rel_size
                smoothed_h_off = h_off
                smoothed_v_off = v_off
            else:
                smoothed_size = alpha * rel_size + (1 - alpha) * smoothed_size
                smoothed_h_off = alpha * h_off + (1 - alpha) * smoothed_h_off
                smoothed_v_off = alpha * v_off + (1 - alpha) * smoothed_v_off

            logger.info(
                "EMA: size=%.3f h=%.2f v=%.2f",
                smoothed_size, smoothed_h_off, smoothed_v_off,
            )

            # Broadcast perception to dashboard overlay
            self._broadcast_approach_perception(response, h_off, v_off, rel_size)

            # --- Compute-then-execute ---

            # 3A: Compute horizontal correction (strafe-first)
            h_cmd: tuple[str, str, int] | None = None
            if abs(smoothed_h_off) > cfg.INSPECTION_H_DEADBAND:
                strafe_cm = int(abs(smoothed_h_off) * cfg.INSPECTION_KP_LATERAL)
                if strafe_cm >= cfg.INSPECTION_SKIP_LATERAL_CM:
                    strafe_cm = max(
                        cfg.INSPECTION_MIN_STRAFE,
                        min(strafe_cm, cfg.INSPECTION_MAX_STRAFE),
                    )
                    direction = "right" if smoothed_h_off > 0 else "left"
                    h_cmd = ("strafe", direction, strafe_cm)

                    # Track strafe improvement for supplementary rotation
                    cur_h_mag = abs(smoothed_h_off)
                    if prev_h_magnitude is not None and cur_h_mag >= prev_h_magnitude:
                        consecutive_strafe_no_improvement += 1
                    else:
                        consecutive_strafe_no_improvement = 0
                    prev_h_magnitude = cur_h_mag

                    # Supplementary rotation if strafes aren't helping
                    if (
                        consecutive_strafe_no_improvement >= 3
                        and abs(smoothed_h_off) >= 0.20
                    ):
                        raw = int(abs(smoothed_h_off) * cfg.INSPECTION_ROTATION_GAIN)
                        rotate_deg = max(cfg.MIN_ROTATION, min(15, raw))
                        rot_dir = (
                            "clockwise" if smoothed_h_off > 0
                            else "counter_clockwise"
                        )
                        h_cmd = ("rotate", rot_dir, rotate_deg)
                        consecutive_strafe_no_improvement = 0
                        logger.info(
                            "Supplementary rotation: %s %d° (strafe not improving)",
                            rot_dir, rotate_deg,
                        )
                else:
                    logger.debug(
                        "Skipping tiny strafe: %.0fcm < %.0f threshold",
                        strafe_cm, cfg.INSPECTION_SKIP_LATERAL_CM,
                    )

            # 3B: Compute vertical correction
            v_cmd: tuple[str, int] | None = None
            if smoothed_v_off is not None and abs(smoothed_v_off) > cfg.INSPECTION_V_DEADBAND:
                vert_cm = int(abs(smoothed_v_off) * cfg.INSPECTION_KP_VERTICAL)
                if vert_cm >= cfg.INSPECTION_SKIP_VERTICAL_CM:
                    vert_cm = max(
                        cfg.INSPECTION_MIN_VERTICAL,
                        min(vert_cm, cfg.INSPECTION_MAX_VERTICAL),
                    )
                    vert_dir = "up" if smoothed_v_off > 0 else "down"
                    v_cmd = (vert_dir, vert_cm)
                else:
                    logger.debug(
                        "Skipping tiny vertical: %dcm < %d threshold",
                        vert_cm, cfg.INSPECTION_SKIP_VERTICAL_CM,
                    )

            # 3C: Compute forward distance (gated by centering + path_clear)
            forward_cm = 0
            if abs(smoothed_h_off) < cfg.INSPECTION_CENTERING_THRESHOLD:
                if smoothed_size < 0.15:
                    forward_cm = cfg.INSPECTION_FORWARD_FAR
                elif smoothed_size < 0.25:
                    forward_cm = cfg.INSPECTION_FORWARD_MEDIUM
                else:
                    forward_cm = cfg.INSPECTION_FORWARD_CLOSE

            # 3D: Exit check (before executing)
            if (
                forward_step_count >= cfg.INSPECTION_MIN_FORWARD_STEPS
                and smoothed_size >= cfg.INSPECTION_APPROACH_SIZE_THRESHOLD
            ):
                logger.info(
                    "Target close enough (smoothed_size=%.3f >= %.3f, "
                    "forward_steps=%d)",
                    smoothed_size, cfg.INSPECTION_APPROACH_SIZE_THRESHOLD,
                    forward_step_count,
                )
                self._send_text_sync(
                    "Target centered and close enough, starting inspection."
                )
                break

            # 3E: Execute all non-zero corrections sequentially
            moves_done: list[str] = []

            if h_cmd is not None:
                h_type, h_dir, h_amount = h_cmd
                if h_type == "strafe":
                    logger.info(
                        "Strafe centering: %s %dcm (smoothed_h=%.2f, size=%.3f)",
                        h_dir, h_amount, smoothed_h_off, smoothed_size,
                    )
                    self._controller.move(h_dir, h_amount)
                    self._log_command(
                        f"Strafe {h_dir} {h_amount}cm "
                        f"(centering, h={smoothed_h_off:.2f})"
                    )
                    self._interruptible_sleep(cfg.APPROACH_MOVE_DELAY)
                    moves_done.append(f"strafed {h_dir} {h_amount}cm")
                else:
                    dir_label = "CW" if h_dir == "clockwise" else "CCW"
                    logger.info(
                        "Rotation centering: %s %d° (smoothed_h=%.2f)",
                        h_dir, h_amount, smoothed_h_off,
                    )
                    self._controller.rotate(h_dir, h_amount)
                    self._log_command(
                        f"Rotate {dir_label} {h_amount}° "
                        f"(centering, h={smoothed_h_off:.2f})"
                    )
                    self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)
                    moves_done.append(f"rotated {dir_label} {h_amount}°")

            if v_cmd is not None:
                v_dir, v_amount = v_cmd
                logger.info(
                    "Vertical correction: %s %dcm (smoothed_v=%.2f)",
                    v_dir, v_amount, smoothed_v_off,
                )
                self._controller.move(v_dir, v_amount)
                self._log_command(
                    f"Move {v_dir} {v_amount}cm "
                    f"(vertical, v={smoothed_v_off:.2f})"
                )
                self._interruptible_sleep(cfg.APPROACH_MOVE_DELAY)
                moves_done.append(f"moved {v_dir} {v_amount}cm")

            if forward_cm > 0:
                logger.info(
                    "Moving forward %dcm (smoothed_size=%.3f)",
                    forward_cm, smoothed_size,
                )
                result = self._controller.move("forward", forward_cm)
                self._log_command(
                    f"Forward {forward_cm}cm (size: {smoothed_size:.3f})"
                )
                if not result.get("success"):
                    if not self._controller.state.is_flying:
                        raise RuntimeError("Drone auto-landed during approach")
                    consecutive_cmd_failures += 1
                    logger.warning(
                        "Forward move failed (%d/3): %s",
                        consecutive_cmd_failures, result,
                    )
                    if consecutive_cmd_failures >= 3:
                        raise RuntimeError(
                            "3 consecutive command failures — aborting approach"
                        )
                    continue  # Don't count step (lesson F5)
                consecutive_cmd_failures = 0
                forward_step_count += 1
                self._interruptible_sleep(cfg.APPROACH_MOVE_DELAY)
                moves_done.append(f"moved forward {forward_cm}cm")

                # Stagnation detection (only on forward steps)
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
                                "Target size is not growing — this is as close "
                                "as we can get. "
                                "Proceeding to inspection at current distance."
                            )
                            break
                    else:
                        consecutive_no_growth = 0
                last_forward_size = smoothed_size

            moved_this_step = bool(moves_done)

            if not moves_done:
                logger.info(
                    "No movement this step (h=%.2f, fwd=%d)",
                    smoothed_h_off, forward_cm,
                )

            # Narrate progress periodically
            if moves_done and step % cfg.INSPECTION_NARRATION_INTERVAL == 0:
                # Build size description
                if smoothed_size < 0.10:
                    size_desc = "still far out"
                elif smoothed_size < 0.15:
                    size_desc = "getting closer"
                elif smoothed_size < 0.20:
                    size_desc = "fairly close"
                else:
                    size_desc = "nearly in position"

                # Build move descriptions
                move_descs = []
                for m in moves_done:
                    if "forward" in m:
                        move_descs.append("moving closer")
                    elif "right" in m.lower():
                        move_descs.append("adjusting right")
                    elif "left" in m.lower():
                        move_descs.append("adjusting left")
                    elif "up" in m:
                        move_descs.append("adjusting up")
                    elif "down" in m:
                        move_descs.append("adjusting down")

                narration = f"Step {step + 1}: {', '.join(move_descs)}. Target {size_desc}."
                self._send_text_sync(narration)

            # Minimum 1s per step — prevents tight loops on cached/zero-movement steps
            self._interruptible_sleep(1.0)

        else:
            logger.info(
                "Max approach steps (%d) reached. Proceeding to inspection.",
                cfg.INSPECTION_MAX_APPROACH_STEPS,
            )

        # Store final size for post-inspection move clamping
        if self._mission and smoothed_size is not None:
            self._mission.final_relative_size = smoothed_size

    # ------------------------------------------------------------------
    # Final centering (between approach and inspection)
    # ------------------------------------------------------------------

    def _final_centering(self, target_description: str) -> None:
        """Tight centering adjustments after approach, before orbit inspection.

        Only lateral and vertical corrections — no forward movement.
        """
        cfg = self._config
        max_steps = cfg.INSPECTION_FINAL_CENTERING_MAX_STEPS
        logger.info("Starting final centering (max %d steps)", max_steps)

        from client.src.perception.visual import compute_offsets

        for i in range(max_steps):
            self._check_abort()

            frame_bytes = self._streamer.get_fresh_perception_frame_bytes(
                timeout=3.0, min_new_frames=10,
            )
            if not frame_bytes:
                logger.warning("No frame for final centering step %d", i)
                break

            try:
                response = self._visual_client.detect(frame_bytes, target_description)
                self._broadcast_ai_activity("centering_detect")
            except RateLimitError:
                logger.warning("Rate limited during final centering, treating as not visible")
                break
            if not response.target_visible or not response.box_2d or len(response.box_2d) != 4:
                logger.info("Target not visible during final centering — done")
                break

            h_off, v_off, rel_size = compute_offsets(response.box_2d)
            self._broadcast_approach_perception(response, h_off, v_off, rel_size)
            needs_correction = False

            try:
                if abs(h_off) > cfg.INSPECTION_FINAL_CENTERING_H:
                    strafe_cm = int(abs(h_off) * cfg.INSPECTION_KP_LATERAL)
                    if strafe_cm >= cfg.INSPECTION_MIN_STRAFE:
                        # Large offset -> strafe
                        strafe_cm = min(strafe_cm, cfg.INSPECTION_MAX_STRAFE)
                        direction = "right" if h_off > 0 else "left"
                        logger.info(
                            "Final centering: strafe %s %dcm (h=%.3f)",
                            direction, strafe_cm, h_off,
                        )
                        self._controller.move(direction, strafe_cm)
                        self._interruptible_sleep(cfg.APPROACH_MOVE_DELAY)
                    else:
                        # Small offset -> rotation (finer than min strafe)
                        rot_deg = int(abs(h_off) * cfg.INSPECTION_FINAL_CENTERING_ROTATION_GAIN)
                        rot_deg = max(
                            cfg.MIN_ROTATION,
                            min(rot_deg, cfg.INSPECTION_FINAL_CENTERING_MAX_ROTATION),
                        )
                        rot_dir = "clockwise" if h_off > 0 else "counter_clockwise"
                        self._controller.rotate(rot_dir, rot_deg)
                        cw_label = "CW" if h_off > 0 else "CCW"
                        self._log_command(
                            f"Rotate {cw_label} {rot_deg}\u00b0 (fine centering, h={h_off:.3f})"
                        )
                        self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)
                    needs_correction = True

                if abs(v_off) > cfg.INSPECTION_FINAL_CENTERING_V:
                    vert_cm = int(abs(v_off) * cfg.INSPECTION_KP_VERTICAL)
                    vert_cm = max(
                        cfg.INSPECTION_MIN_VERTICAL,
                        min(vert_cm, cfg.INSPECTION_MAX_VERTICAL),
                    )
                    vert_dir = "up" if v_off > 0 else "down"
                    logger.info(
                        "Final centering: %s %dcm (v=%.3f)",
                        vert_dir, vert_cm, v_off,
                    )
                    self._controller.move(vert_dir, vert_cm)
                    self._interruptible_sleep(cfg.APPROACH_MOVE_DELAY)
                    needs_correction = True
            except Exception:
                logger.warning(
                    "Final centering move failed at step %d",
                    i, exc_info=True,
                )
                if not self._controller.state.is_flying:
                    raise RuntimeError("Drone auto-landed during final centering")
                break

            if not needs_correction:
                logger.info("Final centering converged at step %d", i)
                break

        self._send_text_sync("Final centering complete.")
        logger.info("Final centering done")

    # ------------------------------------------------------------------
    # L-Maneuver: reposition to requested viewing angle
    # ------------------------------------------------------------------

    def _run_l_maneuver(self, target_description: str, viewing_angle: str) -> None:
        """Execute L-shaped maneuver to reposition drone to the requested viewing angle.

        Geometry:
          behind → strafe right, move forward, rotate CW 180°
          left   → strafe right, move forward, rotate CW 90°
          right  → strafe left, move forward, rotate CCW 90°
        """
        cfg = self._config
        strafe = cfg.LMANEUVER_STRAFE_DISTANCE
        forward = cfg.LMANEUVER_FORWARD_DISTANCE
        stabilize = cfg.INSPECTION_ORBIT_STABILIZE

        # Determine maneuver parameters based on viewing angle
        if viewing_angle == "behind":
            strafe_dir, rotate_dir, rotate_deg = "right", "clockwise", 180
        elif viewing_angle == "left":
            strafe_dir, rotate_dir, rotate_deg = "right", "clockwise", 90
        elif viewing_angle == "right":
            strafe_dir, rotate_dir, rotate_deg = "left", "counter_clockwise", 90
        else:
            logger.warning("Unknown viewing angle '%s' — skipping L-maneuver", viewing_angle)
            return

        self._send_text_sync(
            f"Repositioning to view the target from {viewing_angle}. "
            f"Executing L-maneuver: strafe {strafe_dir} {strafe}cm, "
            f"forward {forward}cm, then rotate {rotate_deg} degrees."
        )

        # Step 1: Strafe sideways to clear the target
        self._check_abort()
        self._controller.move(strafe_dir, strafe)
        self._log_command(f"Move {strafe_dir} {strafe}cm (L-maneuver: clear target)")
        self._interruptible_sleep(stabilize)

        # Step 2: Move forward to pass the target
        self._check_abort()
        self._controller.move("forward", forward)
        self._log_command(f"Move forward {forward}cm (L-maneuver: pass target)")
        self._interruptible_sleep(stabilize)

        # Step 3: Rotate to face the target from the new angle
        self._check_abort()
        self._controller.rotate(rotate_dir, rotate_deg)
        self._log_command(f"Rotate {rotate_dir} {rotate_deg}° (L-maneuver: face target)")
        self._interruptible_sleep(stabilize)

        # Step 4: Re-acquire the target
        self._reacquire_after_l_maneuver(target_description)

    def _reacquire_after_l_maneuver(self, target_description: str) -> None:
        """Re-acquire target after L-maneuver using expanding sweep search.

        First checks current view. If not visible, alternates CW/CCW sweeps
        with increasing angle: 20°, 20°, 40°, 40°, 60°, 60° (configurable).
        On success, runs _final_centering. On failure, proceeds with best-effort.
        """
        cfg = self._config
        sweep_deg = cfg.LMANEUVER_REACQUIRE_SWEEP_DEG
        max_sweeps = cfg.LMANEUVER_REACQUIRE_MAX_SWEEPS

        if not self._visual_client or not self._streamer:
            logger.warning("No visual client/streamer — skipping re-acquisition")
            return

        # Check current view first
        frame = self._streamer.get_fresh_perception_frame_bytes(
            timeout=3.0, min_new_frames=30,
        )
        if frame:
            try:
                resp = self._visual_client.detect(frame, target_description)
                self._broadcast_ai_activity("reacquire_detect")
                if resp.target_visible and resp.confidence >= cfg.SEARCH_MIN_CONFIDENCE:
                    logger.info("Target re-acquired immediately after L-maneuver")
                    self._send_text_sync("Target re-acquired. Centering for inspection.")
                    self._final_centering(target_description)
                    return
            except RateLimitError:
                logger.warning("Rate limited during re-acquisition — proceeding with sweep")

        # Expanding sweep: alternate CW/CCW with increasing angles
        self._send_text_sync("Target not visible — sweeping to re-acquire.")
        net_rotation = 0  # Track cumulative rotation to restore heading on failure

        for i in range(max_sweeps):
            self._check_abort()
            # Alternate directions: even=CW, odd=CCW
            # Increasing angle: (i // 2 + 1) * sweep_deg
            angle = ((i // 2) + 1) * sweep_deg
            direction = "clockwise" if i % 2 == 0 else "counter_clockwise"

            self._controller.rotate(direction, angle)
            self._log_command(f"Rotate {direction} {angle}° (re-acquire sweep {i + 1})")
            if direction == "clockwise":
                net_rotation += angle
            else:
                net_rotation -= angle
            self._interruptible_sleep(cfg.APPROACH_ROTATE_DELAY)

            frame = self._streamer.get_fresh_perception_frame_bytes(
                timeout=3.0, min_new_frames=30,
            )
            if not frame:
                continue

            try:
                resp = self._visual_client.detect(frame, target_description)
                self._broadcast_ai_activity("reacquire_detect")
            except RateLimitError:
                continue

            if resp.target_visible and resp.confidence >= cfg.SEARCH_MIN_CONFIDENCE:
                logger.info(
                    "Target re-acquired at sweep %d (net rotation %d°)",
                    i + 1, net_rotation,
                )
                self._send_text_sync("Target re-acquired. Centering for inspection.")
                self._final_centering(target_description)
                return

        # Exhausted all sweeps — proceed with best effort
        logger.warning(
            "Re-acquisition failed after %d sweeps (net rotation %d°) — "
            "proceeding with inspection from current position",
            max_sweeps, net_rotation,
        )
        self._send_text_sync(
            "Could not re-acquire target after repositioning. "
            "Proceeding with inspection from current position."
        )

    # ------------------------------------------------------------------
    # Phase 3: Strafe + rotation inspection
    # ------------------------------------------------------------------

    def _run_inspection_phase(
        self, target_description: str, aspects: str | None,
        viewing_angle: str = "front",
    ) -> None:
        """Capture frames from 3 perspectives using lateral strafe + rotation."""
        cfg = self._config
        strafe_dist = cfg.INSPECTION_STRAFE_DISTANCE
        strafe_rot = cfg.INSPECTION_STRAFE_ROTATION
        stabilize = cfg.INSPECTION_ORBIT_STABILIZE
        captured_frames: list[bytes] = []
        captured_labels: list[str] = []

        # Compute labels based on viewing angle
        if viewing_angle != "front":
            front_label = f"{viewing_angle} close-up"
            right_label = f"{viewing_angle} right-angled view"
            left_label = f"{viewing_angle} left-angled view"
        else:
            front_label = "front close-up"
            right_label = "right-angled view"
            left_label = "left-angled view"

        # 1. Front close-up — capture at current position
        self._check_abort()
        self._interruptible_sleep(stabilize)
        frame = self._streamer.get_fresh_perception_frame_bytes(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append(front_label)
            self._report.inspection_frames.append((frame, front_label))
            logger.info("Captured %s (%d bytes)", front_label, len(frame))
        else:
            logger.warning("No frame for %s", front_label)

        # 2. Right angled view: strafe right → rotate to face target → capture → return
        self._check_abort()
        self._send_text_sync(f"Strafing right {strafe_dist}cm for angled view.")
        self._controller.move("right", strafe_dist)
        self._log_command(f"Move right {strafe_dist}cm (inspection: {right_label})")
        self._controller.rotate("counter_clockwise", strafe_rot)
        self._log_command(f"Rotate CCW {strafe_rot}° (face target)")
        self._interruptible_sleep(stabilize)

        frame = self._streamer.get_fresh_perception_frame_bytes(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append(right_label)
            self._report.inspection_frames.append((frame, right_label))
            logger.info("Captured %s (%d bytes)", right_label, len(frame))
        else:
            logger.warning("No frame for %s", right_label)

        # Return to center from right
        self._check_abort()
        self._controller.rotate("clockwise", strafe_rot)
        self._log_command(f"Rotate CW {strafe_rot}° (restore heading)")
        self._controller.move("left", strafe_dist)
        self._log_command(f"Move left {strafe_dist}cm (return to center)")
        self._interruptible_sleep(stabilize)

        # 3. Left angled view: strafe left → rotate to face target → capture → return
        self._check_abort()
        self._send_text_sync(f"Strafing left {strafe_dist}cm for opposite angled view.")
        self._controller.move("left", strafe_dist)
        self._log_command(f"Move left {strafe_dist}cm (inspection: {left_label})")
        self._controller.rotate("clockwise", strafe_rot)
        self._log_command(f"Rotate CW {strafe_rot}° (face target)")
        self._interruptible_sleep(stabilize)

        frame = self._streamer.get_fresh_perception_frame_bytes(timeout=3.0)
        if frame is not None:
            captured_frames.append(frame)
            captured_labels.append(left_label)
            self._report.inspection_frames.append((frame, left_label))
            logger.info("Captured %s (%d bytes)", left_label, len(frame))
        else:
            logger.warning("No frame for %s", left_label)

        # Return to center from left
        self._check_abort()
        self._controller.rotate("counter_clockwise", strafe_rot)
        self._log_command(f"Rotate CCW {strafe_rot}° (restore heading)")
        self._controller.move("right", strafe_dist)
        self._log_command(f"Move right {strafe_dist}cm (return to center)")

        if not captured_frames:
            logger.error("No frames captured during inspection")
            if self._sm:
                self._sm.try_transition(MissionStatus.ABORTED)
            self._notify_status()
            return

        logger.info(
            "Inspection capture complete (%d frames: %s). Generating report via Flash.",
            len(captured_frames), ", ".join(captured_labels),
        )

        self._check_abort()

        # Resume heartbeat before report generation — Flash API can take 10-20s
        # and the Tello auto-lands after 15s without keepalive.
        self._controller.executor.resume_heartbeat()

        # Generate structured report via Gemini Flash (cheap text output)
        if self._visual_client:
            self._report.inspection_report = self._visual_client.generate_report(
                captured_frames, captured_labels, target_description, aspects,
            )
            self._broadcast_ai_activity("inspection_report")
            summary = self._report.inspection_report.summary
        else:
            summary = "Inspection frames captured but no visual client available."
            logger.warning("No visual client — skipping report generation")

        # Send brief summary to Live session for verbal announcement
        self._send_text_sync(f"Inspection complete. {summary}")

        logger.info("Inspection phase complete — staying airborne for follow-up")

"""Exploration mission — scan pattern, target acquisition, and proportional approach.

Implements User Story 2: "Find the red bag" — the drone autonomously scans,
identifies the target, navigates toward it with proportional control, and
confirms when found.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from client.src.config import ClientConfig
from client.src.models.mission import Mission, MissionStatus, MissionType
from client.src.models.perception import PerceptionResult, ScanFrame
from client.src.models.tool_calls import ReportPerceptionParams, ReportScanAnalysisParams

if TYPE_CHECKING:
    from client.src.drone.controller import DroneController
    from client.src.video.frame_streamer import FrameStreamer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Perception Bridge
# ---------------------------------------------------------------------------

PERCEPTION_FALLBACK_TIMEOUT = 3.0  # seconds to wait before sending text nudge


class PerceptionBridge:
    """Bridges report_perception tool calls to the approach controller.

    The Gemini system instruction tells it to call report_perception on every
    video frame during active approach.  If no perception arrives within
    ``PERCEPTION_FALLBACK_TIMEOUT`` after a movement command, the bridge
    provides a ``request_perception_nudge`` text that the caller can inject
    into the session via ``send_client_content`` to remind Gemini.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._latest: ReportPerceptionParams | None = None
        self._lock = threading.Lock()
        self._active = False

    def activate(self) -> None:
        """Mark approach as active — perception results are expected."""
        self._active = True
        self._event.clear()

    def deactivate(self) -> None:
        """Mark approach as inactive."""
        self._active = False
        self._event.set()  # Unblock any waiters

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, params: ReportPerceptionParams) -> None:
        """Called by ToolHandler when a report_perception tool call arrives."""
        with self._lock:
            self._latest = params
        self._event.set()

    def wait_for_perception(
        self, timeout: float = PERCEPTION_FALLBACK_TIMEOUT,
    ) -> PerceptionResult | None:
        """Block until a perception result is available or timeout.

        Returns None if timed out (caller should send a text nudge).
        """
        self._event.clear()
        got_it = self._event.wait(timeout=timeout)
        if not got_it:
            return None
        with self._lock:
            p = self._latest
            self._latest = None
        if p is None:
            return None
        return PerceptionResult(
            target_visible=p.target_visible,
            horizontal_offset=p.horizontal_offset,
            vertical_offset=p.vertical_offset,
            relative_size=p.relative_size,
            confidence=p.confidence,
        )

    @staticmethod
    def build_nudge_text(target_description: str) -> str:
        """Build the fallback text prompt to nudge Gemini for perception."""
        return (
            f"Analyze the current video frame and call report_perception "
            f"for the target: {target_description}. "
            f"Report target_visible, horizontal_offset, vertical_offset, "
            f"relative_size, and confidence."
        )


# ---------------------------------------------------------------------------
# Scan Analysis Bridge
# ---------------------------------------------------------------------------


class ScanAnalysisBridge:
    """Bridges report_scan_analysis tool calls to the exploration mission."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._latest: ReportScanAnalysisParams | None = None
        self._lock = threading.Lock()

    def feed(self, params: ReportScanAnalysisParams) -> None:
        """Called by ToolHandler when report_scan_analysis arrives."""
        with self._lock:
            self._latest = params
        self._event.set()

    def wait_for_analysis(self, timeout: float = 60.0) -> ReportScanAnalysisParams | None:
        """Block until scan analysis arrives or timeout."""
        self._event.clear()
        got_it = self._event.wait(timeout=timeout)
        if not got_it:
            return None
        with self._lock:
            result = self._latest
            self._latest = None
        return result


# ---------------------------------------------------------------------------
# Scan Pattern
# ---------------------------------------------------------------------------

SCAN_POSITIONS = 8
SCAN_DEGREES = 45
MAX_ROTATION_RETRIES = 3


class ScanPattern:
    """8-position 360-degree recon scan.

    At each position: rotate drone with retry logic, wait stabilization,
    capture frame as ScanFrame (JPEG + heading).
    """

    def __init__(
        self,
        controller: DroneController,
        frame_streamer: FrameStreamer,
        config: ClientConfig,
    ) -> None:
        self._controller = controller
        self._streamer = frame_streamer
        self._config = config

    def execute(self, mission: Mission, abort_event: threading.Event) -> list[ScanFrame]:
        """Run the 8-position scan and return captured frames.

        Raises RuntimeError if aborted.
        """
        frames: list[ScanFrame] = []
        heading = 0

        for i in range(SCAN_POSITIONS):
            if abort_event.is_set():
                raise RuntimeError("Scan aborted by user")

            # Rotate to next position (skip first — we're already at heading 0)
            if i > 0:
                rotated = False
                for attempt in range(1, MAX_ROTATION_RETRIES + 1):
                    result = self._controller.rotate("clockwise", SCAN_DEGREES)
                    if result.get("success"):
                        rotated = True
                        break
                    logger.warning(
                        "Rotation attempt %d/%d failed at position %d",
                        attempt, MAX_ROTATION_RETRIES, i,
                    )
                if not rotated:
                    logger.error(
                        "Failed to rotate to position %d after %d attempts",
                        i, MAX_ROTATION_RETRIES,
                    )
                    # Continue anyway — capture frame at current heading
                heading = (heading + SCAN_DEGREES) % 360

            # Wait stabilization
            time.sleep(self._config.INTER_COMMAND_ROTATE_DELAY)

            # Capture frame
            jpeg_bytes = self._streamer.get_perception_frame_bytes()
            if jpeg_bytes is None:
                logger.warning("No frame captured at scan position %d", i)
                continue

            scan_frame = ScanFrame(
                index=i,
                heading_degrees=heading,
                jpeg_bytes=jpeg_bytes,
                captured_at=time.time(),
            )
            frames.append(scan_frame)
            logger.info(
                "Scan frame %d/%d captured (heading=%d°, %d bytes)",
                i + 1, SCAN_POSITIONS, heading, len(jpeg_bytes),
            )

        return frames


# ---------------------------------------------------------------------------
# Approach Controller
# ---------------------------------------------------------------------------


class ApproachController:
    """Proportional approach controller consuming PerceptionResult data.

    Applies EMA smoothing, horizontal/vertical alignment, forward movement
    with zone-based max distance, completion detection, search recovery,
    and watchdog timeout.
    """

    def __init__(self, config: ClientConfig) -> None:
        self._config = config
        # EMA-smoothed values
        self._s_h_offset = 0.0
        self._s_v_offset = 0.0
        self._s_size = 0.0
        self._s_confidence = 0.0
        self._not_visible_count = 0
        self._step = 0
        self._started_at = 0.0

    def reset(self) -> None:
        """Reset controller state for a new approach."""
        self._s_h_offset = 0.0
        self._s_v_offset = 0.0
        self._s_size = 0.0
        self._s_confidence = 0.0
        self._not_visible_count = 0
        self._step = 0
        self._started_at = time.time()

    def _ema(self, old: float, new: float) -> float:
        alpha = self._config.EMA_ALPHA
        return alpha * new + (1 - alpha) * old

    def update(self, perception: PerceptionResult) -> None:
        """Update smoothed state with a new perception result."""
        if not perception.target_visible or perception.confidence < 0.5:
            self._not_visible_count += 1
            return

        self._not_visible_count = 0
        self._s_h_offset = self._ema(self._s_h_offset, perception.horizontal_offset)
        self._s_v_offset = self._ema(self._s_v_offset, perception.vertical_offset)
        self._s_size = self._ema(self._s_size, perception.relative_size)
        self._s_confidence = self._ema(self._s_confidence, perception.confidence)

    @property
    def is_complete(self) -> bool:
        """Check if target is close enough (relative_size >= COMPLETION_SIZE)."""
        return self._s_size >= self._config.COMPLETION_SIZE

    @property
    def needs_search_recovery(self) -> bool:
        """3 consecutive not-visible results → search recovery."""
        return self._not_visible_count >= 3

    @property
    def is_watchdog_expired(self) -> bool:
        return (time.time() - self._started_at) > self._config.APPROACH_WATCHDOG_S

    @property
    def is_max_steps(self) -> bool:
        return self._step >= self._config.MAX_APPROACH_STEPS

    def compute_command(self, controller: DroneController) -> str:
        """Compute and execute the next approach command.

        Returns a description string of the action taken.
        """
        self._step += 1
        cfg = self._config

        # Determine distance zone for forward movement caps
        if self._s_size < 0.08:
            zone = "far"
            max_forward = cfg.MAX_FORWARD_FAR
        elif self._s_size < 0.15:
            zone = "medium-far"
            max_forward = cfg.MAX_FORWARD_FAR
        elif self._s_size < 0.25:
            zone = "medium"
            max_forward = cfg.MAX_FORWARD_MEDIUM
        else:
            zone = "close"
            max_forward = cfg.MAX_FORWARD_CLOSE

        # --- Horizontal alignment ---
        h_abs = abs(self._s_h_offset)
        if h_abs > cfg.CENTERING_THRESHOLD:
            if self._s_size < cfg.STRAFE_ZONE_THRESHOLD:
                # Far away → rotate
                degrees = int(abs(cfg.KP_ROTATION * self._s_h_offset))
                if degrees >= cfg.SKIP_ROTATION_THRESHOLD:
                    direction = "clockwise" if self._s_h_offset > 0 else "counter_clockwise"
                    degrees = max(int(cfg.MIN_ROTATION), min(degrees, int(cfg.MAX_ROTATION)))
                    controller.rotate(direction, degrees)
                    time.sleep(cfg.APPROACH_ROTATE_DELAY)
                    return f"rotate_{direction}_{degrees}deg (zone={zone})"
            else:
                # Close → lateral strafe
                dist = int(abs(cfg.KP_LATERAL * self._s_h_offset))
                if dist >= cfg.SKIP_LATERAL_THRESHOLD:
                    direction = "right" if self._s_h_offset > 0 else "left"
                    dist = max(int(cfg.MIN_MOVE_DISTANCE), min(dist, int(cfg.MAX_MOVE_DISTANCE)))
                    controller.move(direction, dist)
                    time.sleep(cfg.APPROACH_MOVE_DELAY)
                    return f"strafe_{direction}_{dist}cm (zone={zone})"

        # --- Vertical alignment ---
        v_abs = abs(self._s_v_offset)
        if v_abs > cfg.VERTICAL_DEADBAND:
            dist = int(abs(cfg.KP_VERTICAL * self._s_v_offset))
            if dist >= cfg.SKIP_VERTICAL_THRESHOLD:
                direction = "down" if self._s_v_offset > 0 else "up"
                dist = max(int(cfg.MIN_MOVE_DISTANCE), min(dist, int(cfg.MAX_MOVE_DISTANCE)))
                controller.move(direction, dist)
                time.sleep(cfg.APPROACH_MOVE_DELAY)
                return f"vertical_{direction}_{dist}cm (zone={zone})"

        # --- Forward movement (only if horizontally centered) ---
        if h_abs < cfg.CENTERING_THRESHOLD:
            forward_dist = int(cfg.KP_FORWARD * (cfg.COMPLETION_SIZE - self._s_size))
            forward_dist = max(int(cfg.MIN_MOVE_DISTANCE), min(forward_dist, max_forward))
            controller.move("forward", forward_dist)
            time.sleep(cfg.APPROACH_MOVE_DELAY)
            return f"forward_{forward_dist}cm (zone={zone})"

        return "no_action"

    def search_recovery(self, controller: DroneController) -> str:
        """Small search sweep when target lost 3+ times."""
        logger.warning("Search recovery — sweeping for target")
        # Small clockwise sweep
        controller.rotate("clockwise", 30)
        time.sleep(self._config.APPROACH_ROTATE_DELAY)
        self._not_visible_count = 0
        return "search_sweep_30deg"


# ---------------------------------------------------------------------------
# Exploration Mission
# ---------------------------------------------------------------------------


class ExplorationMission:
    """Full exploration mission: scan → analyze → approach → complete.

    Runs in a background thread. Uses PerceptionBridge for approach-phase
    perception and ScanAnalysisBridge for scan-phase analysis results.
    """

    def __init__(
        self,
        controller: DroneController,
        frame_streamer: FrameStreamer,
        config: ClientConfig,
        perception_bridge: PerceptionBridge,
        scan_analysis_bridge: ScanAnalysisBridge,
        send_text_fn: Any = None,
        send_frames_fn: Any = None,
        on_status_change: Any = None,
    ) -> None:
        self._controller = controller
        self._streamer = frame_streamer
        self._config = config
        self._perception = perception_bridge
        self._scan_analysis = scan_analysis_bridge
        self._send_text = send_text_fn  # async fn to inject text into Gemini session
        self._send_frames = send_frames_fn  # async fn to send scan frames to Gemini
        self._on_status_change = on_status_change  # callback(mission)
        self._abort_event = threading.Event()
        self._mission: Mission | None = None
        self._loop: Any = None  # asyncio event loop for calling async fns from thread

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

    def _send_scan_frames_sync(self, frames: list[ScanFrame], target: str) -> None:
        """Send scan frames to Gemini for analysis from sync thread."""
        if self._send_frames and self._loop:
            import asyncio
            future = asyncio.run_coroutine_threadsafe(
                self._send_frames(frames, target), self._loop
            )
            try:
                future.result(timeout=30.0)
            except Exception:
                logger.warning("Failed to send scan frames to Gemini", exc_info=True)

    def run(self, target_description: str) -> Mission:
        """Execute the full exploration mission (called from background thread).

        Phases: scanning → analyzing → approaching → complete/aborted
        """
        self._mission = Mission(
            type=MissionType.EXPLORE,
            status=MissionStatus.SCANNING,
            target_description=target_description,
            started_at=time.time(),
        )
        self._abort_event.clear()

        try:
            # --- Phase 1: Scanning ---
            self._notify_status()
            logger.info("Exploration started: scanning for '%s'", target_description)

            scan = ScanPattern(self._controller, self._streamer, self._config)
            frames = scan.execute(self._mission, self._abort_event)

            if not frames:
                logger.error("No frames captured during scan")
                self._mission.status = MissionStatus.ABORTED
                self._notify_status()
                return self._mission

            self._mission.scan_frames = frames

            # Land after scan (F1 pattern from data-model.md)
            logger.info("Scan complete (%d frames). Landing for analysis.", len(frames))
            self._controller.land()

            # --- Phase 2: Analyzing ---
            self._mission.status = MissionStatus.ANALYZING
            self._notify_status()

            # Send frames to Gemini for analysis
            self._send_scan_frames_sync(frames, target_description)

            # Wait for report_scan_analysis tool call
            logger.info("Waiting for Gemini scan analysis...")
            analysis = self._scan_analysis.wait_for_analysis(timeout=60.0)

            if analysis is None:
                logger.error("No scan analysis received from Gemini (timeout)")
                self._mission.status = MissionStatus.ABORTED
                self._notify_status()
                return self._mission

            if not analysis.target_visible:
                logger.info("Target not found in scan frames")
                self._send_text_sync(
                    f"I scanned 360 degrees but could not find '{target_description}'. "
                    f"The target was not visible in any scan frame."
                )
                self._mission.status = MissionStatus.COMPLETE
                self._notify_status()
                return self._mission

            # Target found
            self._mission.best_scan_index = analysis.best_index
            self._mission.refined_label = analysis.refined_label
            logger.info(
                "Target found at scan index %d, refined label: '%s'",
                analysis.best_index, analysis.refined_label,
            )

            # --- Phase 3: Approaching ---
            # Take off and rotate to target heading
            self._controller.takeoff()

            if self._abort_event.is_set():
                raise RuntimeError("Mission aborted by user")

            target_heading = frames[analysis.best_index].heading_degrees
            if target_heading > 0:
                self._controller.rotate("clockwise", target_heading)
                time.sleep(self._config.INTER_COMMAND_ROTATE_DELAY)

            self._mission.status = MissionStatus.APPROACHING
            self._notify_status()

            # Activate perception bridge
            self._perception.activate()

            # Notify Gemini that approach is starting
            self._send_text_sync(
                f"I am now approaching the target: {analysis.refined_label}. "
                f"Call report_perception on every video frame to guide the approach."
            )

            approach = ApproachController(self._config)
            approach.reset()

            while not self._abort_event.is_set():
                if approach.is_watchdog_expired:
                    logger.warning(
                        "Approach watchdog expired (%ds)",
                        self._config.APPROACH_WATCHDOG_S,
                    )
                    break

                if approach.is_max_steps:
                    logger.warning(
                        "Max approach steps reached (%d)",
                        self._config.MAX_APPROACH_STEPS,
                    )
                    break

                # Wait for perception with fallback nudge
                perception = self._perception.wait_for_perception(
                    timeout=PERCEPTION_FALLBACK_TIMEOUT
                )

                if perception is None:
                    # Fallback: nudge Gemini for perception
                    nudge = PerceptionBridge.build_nudge_text(
                        self._mission.refined_label or target_description
                    )
                    self._send_text_sync(nudge)

                    # Wait again after nudge
                    perception = self._perception.wait_for_perception(timeout=5.0)
                    if perception is None:
                        logger.warning("No perception even after nudge, skipping step")
                        approach._step += 1
                        continue

                approach.update(perception)
                self._mission.approach_step = approach._step

                if approach.is_complete:
                    logger.info("Target reached! (relative_size=%.3f)", approach._s_size)
                    self._send_text_sync(
                        f"I've reached the target: {self._mission.refined_label}. "
                        f"The target is right in front of me."
                    )
                    break

                if approach.needs_search_recovery:
                    action = approach.search_recovery(self._controller)
                    logger.info("Search recovery: %s", action)
                    self._mission.approach_step = approach._step
                    self._notify_status()
                    continue

                action = approach.compute_command(self._controller)
                logger.info(
                    "Approach step %d/%d: %s (size=%.3f, h=%.2f, v=%.2f)",
                    approach._step, self._config.MAX_APPROACH_STEPS,
                    action, approach._s_size, approach._s_h_offset, approach._s_v_offset,
                )
                self._mission.approach_step = approach._step
                self._notify_status()

            # Deactivate perception bridge
            self._perception.deactivate()

            if self._abort_event.is_set():
                self._mission.status = MissionStatus.ABORTED
            elif approach.is_complete:
                self._mission.status = MissionStatus.COMPLETE
            else:
                # Watchdog or max steps — partial completion
                self._mission.status = MissionStatus.COMPLETE

            self._notify_status()
            logger.info("Exploration mission finished: %s", self._mission.status)
            return self._mission

        except RuntimeError as e:
            logger.warning("Exploration mission error: %s", e)
            self._mission.status = MissionStatus.ABORTED
            self._perception.deactivate()
            self._notify_status()
            return self._mission

        except Exception:
            logger.exception("Unexpected error in exploration mission")
            self._mission.status = MissionStatus.ABORTED
            self._perception.deactivate()
            self._notify_status()
            # Safety: land if still flying
            if self._controller.state.is_flying:
                self._controller.emergency_land()
            return self._mission

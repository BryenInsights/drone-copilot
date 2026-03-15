"""Visual perception via Gemini generate_content — bounding box detection.

Uses gemini-2.5-flash (free tier) with box_2d to get accurate pixel-level
bounding boxes for approach guidance, replacing the Live API's vague float
estimates from report_perception tool calls.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RateLimitError(Exception):
    """Raised when Gemini API returns 429 and all retries are exhausted."""

    def __init__(self, retry_after: float = 30.0, message: str = ""):
        self.retry_after = retry_after
        super().__init__(message or f"Rate limited, retry after {retry_after}s")


def _parse_retry_delay(error: Exception) -> float:
    """Extract retryDelay from a google.genai ClientError 429 response."""
    try:
        details = getattr(error, "details", {})
        for entry in details.get("error", {}).get("details", []):
            if entry.get("@type", "").endswith("RetryInfo"):
                delay_str = entry.get("retryDelay", "")
                if delay_str.endswith("s"):
                    return float(delay_str[:-1])
    except Exception:
        pass
    return 30.0


_DETECTION_PROMPT = """\
Look at this drone camera image. Find the object matching this description: "{target}"

Return a JSON object with:
- target_visible: true ONLY if the described object is clearly visible.
  If what you see does not match the description, set to false.
- confidence: 0.0-1.0 how confident you are.
  MUST be 0.0 when target_visible is false.
- box_2d: bounding box [ymin, xmin, ymax, xmax] values 0-1000.
  MUST be null when target_visible is false.
- relative_size: estimate target width as a fraction of the full frame width.
  Use this guide:
    0.03-0.08 = tiny/far (3+ meters)
    0.08-0.15 = small (1.5-3 meters)
    0.15-0.25 = medium (0.8-1.5 meters)
    0.25-0.40 = large/close (under 0.8 meters)
    0.40+     = very close / filling the frame
  MUST be 0.0 when target_visible is false.
- path_clear: true if flight path toward object is clear of obstacles.
  Default true when target_visible is false.
"""


_PLANNING_PROMPT = """\
Split this inspection target into a searchable object and a specific feature.

Target: "{target}"

Rules:
- searchable_object: The main object that can be found from any angle \
(e.g. "green bicycle", "red car", "wooden bookshelf").
- feature: The specific part or detail that might only be visible from \
certain angles. Leave empty string "" if the target IS the whole object.

Examples:
- "the rear cassette of the green bicycle" → object="green bicycle", feature="rear cassette"
- "the license plate on the red car" → object="red car", feature="license plate"
- "the label on the back of the green box" → object="green box", feature="label on the back"
- "the green box" → searchable_object="green box", feature=""
- "inspect the drone battery" → searchable_object="drone battery", feature=""
"""

_REPOSITION_SYSTEM = """\
You are guiding a drone to find a specific feature on an object. The drone is \
close to the object and needs to reposition to see a hidden feature.

Strategy tips:
- To see the back of an object, orbit around it: move sideways then rotate to keep facing it.
- Small moves (20-60cm) are better than large ones — the object is close.
- After moving, rotate toward the object to keep it in frame.
- If you've moved significantly and still can't see the feature, try the opposite side.
- Say "done" when the feature is visible OR when you've exhausted reasonable options.

Available actions:
- move: direction (forward/back/left/right/up/down), amount (cm, 20-100)
- rotate: direction (clockwise/counter_clockwise), amount (degrees, 15-90)
- done: stop repositioning (feature found or giving up)
"""

_REPOSITION_TURN_PROMPT = """\
The drone is near a "{object}" and looking for the "{feature}".

{history_text}

Look at the current camera image. Can you see the "{feature}"?
- If YES: return action="done"
- If NO: return the next move/rotate command to reposition.
"""



class AngleObservation(BaseModel):
    """What one camera angle uniquely revealed."""

    angle: str
    observation: str


class InspectionReport(BaseModel):
    """Structured inspection report from Gemini Flash."""

    description: str
    condition: str  # excellent/good/fair/poor/damaged
    confidence: float = Field(ge=0.0, le=1.0)
    findings: list[str]
    summary: str  # One-sentence summary for verbal announcement

    # Extended fields for richer reports
    object_identity: str | None = None
    visible_text: list[str] | None = None
    per_angle: list[AngleObservation] | None = None
    damage_details: list[str] | None = None


_REPORT_PROMPT = """\
You are inspecting an object from multiple drone camera angles.
Target: "{target}"
Angles captured: {labels}
{aspects_line}

CRITICAL INSTRUCTIONS — follow every one:
1. READ ALL VISIBLE TEXT on the object: brand names, model numbers, sizes, \
warnings, barcodes, serial numbers, labels. Transcribe them exactly.
2. IDENTIFY THE OBJECT SPECIFICALLY — not just "green box" but e.g. \
"Puma RS-X sneaker box, green/black colorway, US size 10". \
Use the text you read to determine brand, product, variant, size.
3. For EACH camera angle, describe what that specific viewpoint uniquely \
reveals that other angles do not.
4. List EVERY scratch, scuff, dent, tear, stain, discoloration, or defect \
you can see. If the object is clean, return an empty damage_details list.
5. Provide 4-8 specific findings (not generic — reference actual features).

Fill in ALL fields:
- object_identity: Specific identification including brand/product/variant \
(e.g. "Puma RS-X sneaker box, green/black, US 10"). NOT just "green box".
- visible_text: Every piece of readable text as a list of strings \
(e.g. ["PUMA", "RS-X", "US 10", "EUR 43", "UK 9"]).
- description: Overall condition assessment (2-3 sentences) referencing \
actual observed details.
- condition: One of: excellent, good, fair, poor, damaged.
- confidence: 0.0-1.0 how confident you are in your assessment.
- findings: 4-8 specific observations referencing real features, text, \
or details you can see.
- per_angle: For each camera angle, what it uniquely reveals. Use the \
exact angle labels provided.
- damage_details: List of every defect found. Empty list if none.
- summary: One concise sentence summarizing the inspection INCLUDING \
the object identity (brand/product).
"""


class InspectionPlan(BaseModel):
    """Split a target description into a searchable object + specific feature."""

    searchable_object: str
    feature: str = ""


class RepositionCommand(BaseModel):
    """Single repositioning command from the Flash multi-turn chat."""

    action: Literal["move", "rotate", "done"]
    direction: str = ""
    amount: int = 0
    reason: str = ""



class PerceptionResponse(BaseModel):
    """Structured response from visual perception."""

    target_visible: bool
    confidence: float = Field(ge=0.0, le=1.0)
    box_2d: list[int] | None = None  # [ymin, xmin, ymax, xmax], 0-1000
    relative_size: float = Field(ge=0.0, le=1.0, default=0.0)
    path_clear: bool = True


def compute_offsets(box_2d: list[int]) -> tuple[float, float, float]:
    """Compute h_offset, v_offset, relative_size from a box_2d.

    Args:
        box_2d: [ymin, xmin, ymax, xmax] normalized to 0-1000.

    Returns:
        (horizontal_offset, vertical_offset, relative_size)
        h_offset: -1.0 (left) to +1.0 (right)
        v_offset: -1.0 (bottom) to +1.0 (top)
        relative_size: 0.0 to 1.0 (fraction of frame width)
    """
    ymin, xmin, ymax, xmax = box_2d
    box_w = (xmax - xmin) / 1000.0
    box_h = (ymax - ymin) / 1000.0
    relative_size = max(box_w, box_h)
    center_x = (xmin + xmax) / 2000.0
    center_y = (ymin + ymax) / 2000.0
    horizontal_offset = (center_x - 0.5) * 2.0  # -1.0 to +1.0
    vertical_offset = (0.5 - center_y) * 2.0  # -1.0 to +1.0 (top=positive)
    return horizontal_offset, vertical_offset, relative_size


_FALLBACK_PERCEPTION = PerceptionResponse(target_visible=False, confidence=0.0)


class VisualPerceptionClient:
    """Detect objects via Gemini generate_content with bounding box output."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        use_vertex_ai: bool = False,
        gcp_project: str = "",
        gcp_location: str = "us-central1",
        temperature: float = 0.0,
        timeout_ms: int = 60000,
        max_retries: int = 2,
    ) -> None:
        http_opts = types.HttpOptions(timeout=timeout_ms)
        if use_vertex_ai:
            self._client = genai.Client(
                vertexai=True,
                project=gcp_project,
                location=gcp_location,
                http_options=http_opts,
            )
        else:
            self._client = genai.Client(
                api_key=api_key,
                http_options=http_opts,
            )
        self._model = model
        self._temperature = temperature
        self._max_retries = max_retries

    def detect(self, frame_jpeg: bytes, target_description: str) -> PerceptionResponse:
        """Detect a target in a JPEG frame and return structured perception.

        Args:
            frame_jpeg: Raw JPEG bytes of the drone camera frame.
            target_description: What to look for (e.g. "green box on the table").

        Returns:
            PerceptionResponse with bounding box if target found.
            On error/timeout, returns target_visible=False.
        """
        prompt = _DETECTION_PROMPT.format(target=target_description)
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=[
                        types.Part.from_bytes(data=frame_jpeg, mime_type="image/jpeg"),
                        prompt,
                    ],
                    config=types.GenerateContentConfig(
                        temperature=self._temperature,
                        response_mime_type="application/json",
                        response_schema=PerceptionResponse,
                    ),
                )
                if not response.text:
                    logger.warning(
                        "Empty detect() response (attempt %d/%d)",
                        attempt + 1, self._max_retries + 1,
                    )
                    if attempt < self._max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return _FALLBACK_PERCEPTION

                result = PerceptionResponse.model_validate_json(response.text)
                # Post-validation: enforce consistency when target not visible
                if not result.target_visible:
                    result = PerceptionResponse(
                        target_visible=False, confidence=0.0, box_2d=None,
                        relative_size=0.0, path_clear=True,
                    )
                logger.debug(
                    "Perception: visible=%s conf=%.2f box=%s rel_size=%.3f path_clear=%s",
                    result.target_visible,
                    result.confidence,
                    result.box_2d,
                    result.relative_size,
                    result.path_clear,
                )
                return result

            except (TimeoutError, ConnectionError) as e:
                logger.warning(
                    "detect() %s (attempt %d/%d): %s",
                    type(e).__name__, attempt + 1, self._max_retries + 1, e,
                )
                if attempt < self._max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return _FALLBACK_PERCEPTION

            except ClientError as e:
                if e.code == 429:
                    retry_after = _parse_retry_delay(e)
                    logger.warning(
                        "Rate limited (429) attempt %d/%d, retry after %.0fs",
                        attempt + 1, self._max_retries + 1, retry_after,
                    )
                    if attempt < self._max_retries:
                        time.sleep(min(retry_after, 60.0))
                        continue
                    raise RateLimitError(retry_after=retry_after) from e
                # Non-429 client errors (400, 403, etc.)
                logger.warning("ClientError %d in detect(): %s", e.code, e)
                return _FALLBACK_PERCEPTION

            except Exception:
                logger.warning("Visual perception error", exc_info=True)
                return _FALLBACK_PERCEPTION

        return _FALLBACK_PERCEPTION

    def generate_report(
        self,
        frames_jpeg: list[bytes],
        labels: list[str],
        target_description: str,
        aspects: str | None = None,
    ) -> InspectionReport:
        """Generate a structured inspection report from multiple frames.

        Args:
            frames_jpeg: List of JPEG-encoded frames from different angles.
            labels: Human-readable label for each frame angle.
            target_description: What was inspected.
            aspects: Optional focus areas requested by the user.

        Returns:
            InspectionReport with structured assessment.
            On error, returns a fallback report.
        """
        fallback = InspectionReport(
            description="Analysis unavailable.",
            condition="unknown",
            confidence=0.0,
            findings=[],
            summary="Inspection analysis could not be completed.",
        )
        aspects_line = f"Focus especially on: {aspects}" if aspects else ""
        prompt = _REPORT_PROMPT.format(
            target=target_description,
            labels=", ".join(labels),
            aspects_line=aspects_line,
        )
        contents: list[types.Part | str] = []
        for i, frame in enumerate(frames_jpeg):
            contents.append(
                types.Part.from_bytes(data=frame, mime_type="image/jpeg"),
            )
            if i < len(labels):
                contents.append(f"[{labels[i]}]")
        contents.append(prompt)

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=self._temperature,
                        response_mime_type="application/json",
                        response_schema=InspectionReport,
                    ),
                )
                if not response.text:
                    logger.warning(
                        "Empty generate_report() response (attempt %d/%d)",
                        attempt + 1, self._max_retries + 1,
                    )
                    if attempt < self._max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return fallback

                result = InspectionReport.model_validate_json(response.text)
                logger.info(
                    "Inspection report: condition=%s conf=%.2f findings=%d",
                    result.condition, result.confidence, len(result.findings),
                )
                return result

            except (TimeoutError, ConnectionError) as e:
                logger.warning(
                    "generate_report() %s (attempt %d/%d): %s",
                    type(e).__name__, attempt + 1, self._max_retries + 1, e,
                )
                if attempt < self._max_retries:
                    time.sleep(2 ** attempt)
                    continue
                return fallback

            except ClientError as e:
                if e.code == 429:
                    retry_after = _parse_retry_delay(e)
                    logger.warning(
                        "Rate limited (429) in generate_report() attempt %d/%d, "
                        "retry after %.0fs",
                        attempt + 1, self._max_retries + 1, retry_after,
                    )
                    if attempt < self._max_retries:
                        time.sleep(min(retry_after, 60.0))
                        continue
                    raise RateLimitError(retry_after=retry_after) from e
                logger.warning("ClientError %d in generate_report(): %s", e.code, e)
                return fallback

            except Exception:
                logger.warning("Inspection report generation error", exc_info=True)
                return fallback

        return fallback

    # ------------------------------------------------------------------
    # Inspection planning + repositioning
    # ------------------------------------------------------------------

    def plan_inspection(self, target_description: str) -> InspectionPlan:
        """Split target into searchable object + specific feature.

        On error, returns the full description as searchable_object with no feature
        (safe fallback = today's behavior).
        """
        fallback = InspectionPlan(searchable_object=target_description, feature="")
        prompt = _PLANNING_PROMPT.format(target=target_description)
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    temperature=self._temperature,
                    response_mime_type="application/json",
                    response_schema=InspectionPlan,
                ),
            )
            if not response.text:
                return fallback
            result = InspectionPlan.model_validate_json(response.text)
            logger.info(
                "Inspection plan: object=%r feature=%r",
                result.searchable_object, result.feature,
            )
            return result
        except Exception:
            logger.warning("plan_inspection() failed — using full description", exc_info=True)
            return fallback

    def reposition_step(
        self,
        frame_jpeg: bytes,
        object_desc: str,
        feature_desc: str,
        conversation_history: list[types.Content],
        move_descriptions: list[str] | None = None,
    ) -> tuple[RepositionCommand, list[types.Content]]:
        """One turn of the multi-turn repositioning chat.

        Args:
            frame_jpeg: Current drone camera frame.
            object_desc: The main object description.
            feature_desc: The specific feature to find.
            conversation_history: Growing list of Content objects for multi-turn.
            move_descriptions: Step-by-step history of actual moves executed.

        Returns:
            (command, updated_history) — the command to execute and updated history
            for the next call. On error, returns action="done" (safe fallback).
        """
        fallback_cmd = RepositionCommand(action="done", reason="error fallback")
        if move_descriptions:
            history_text = "Moves so far:\n" + "\n".join(move_descriptions)
        elif not conversation_history:
            history_text = "No moves yet."
        else:
            history_text = f"Previous moves: {len(conversation_history) // 2} turn(s) so far."
        prompt = _REPOSITION_TURN_PROMPT.format(
            object=object_desc, feature=feature_desc, history_text=history_text,
        )

        # Build user turn with image + text
        user_parts = [
            types.Part.from_bytes(data=frame_jpeg, mime_type="image/jpeg"),
            types.Part.from_text(text=prompt),
        ]
        user_content = types.Content(role="user", parts=user_parts)
        updated_history = list(conversation_history) + [user_content]

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=updated_history,
                config=types.GenerateContentConfig(
                    temperature=self._temperature,
                    response_mime_type="application/json",
                    response_schema=RepositionCommand,
                    system_instruction=_REPOSITION_SYSTEM,
                ),
            )
            if not response.text:
                return fallback_cmd, conversation_history
            result = RepositionCommand.model_validate_json(response.text)
            logger.info(
                "Reposition step: action=%s dir=%s amount=%d reason=%s",
                result.action, result.direction, result.amount, result.reason,
            )
            # Append model response to history
            model_content = types.Content(
                role="model",
                parts=[types.Part.from_text(text=response.text)],
            )
            updated_history.append(model_content)
            return result, updated_history
        except Exception:
            logger.warning("reposition_step() failed", exc_info=True)
            return fallback_cmd, conversation_history

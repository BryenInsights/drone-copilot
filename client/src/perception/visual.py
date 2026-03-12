"""Visual perception via Gemini generate_content — bounding box detection.

Uses gemini-2.5-flash (free tier) with box_2d to get accurate pixel-level
bounding boxes for approach guidance, replacing the Live API's vague float
estimates from report_perception tool calls.
"""

from __future__ import annotations

import logging
import time

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
- path_clear: true if flight path toward object is clear of obstacles.
  Default true when target_visible is false.
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


class PerceptionResponse(BaseModel):
    """Structured response from visual perception."""

    target_visible: bool
    confidence: float = Field(ge=0.0, le=1.0)
    box_2d: list[int] | None = None  # [ymin, xmin, ymax, xmax], 0-1000
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
                        target_visible=False, confidence=0.0, box_2d=None, path_clear=True,
                    )
                logger.debug(
                    "Perception: visible=%s conf=%.2f box=%s path_clear=%s",
                    result.target_visible,
                    result.confidence,
                    result.box_2d,
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

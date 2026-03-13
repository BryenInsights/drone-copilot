# Perception-Driven Approach Redesign

## Context

The current inspection mission uses a hardcoded `ViewingAngle` enum + L-maneuver (scripted geometry) for repositioning. This is the wrong abstraction. Instead, Flash API should reason about spatial context during the existing approach loop — e.g., "I see a box, the user wants the statue behind it, go right to clear the box."

A sister project (gemini-fly) validated this approach: Flash is great as a sensor, bad as a planner. Keep the proportional controller, make the perception smarter.

## What we have now

**`PerceptionResponse`** from Flash (`client/src/perception/visual.py`):
```python
target_visible: bool
confidence: float
box_2d: list[int] | None    # [ymin, xmin, ymax, xmax], 0-1000 scale
path_clear: bool
```

**The detect prompt** (~`visual.py:43-55`) only asks "where is this object?" When Flash says `target_visible=False`, the approach loop falls into a **blind 3-step search recovery** (CCW 30deg, CW 60deg, CCW 30deg) — no intelligence, no spatial reasoning about *why* the target isn't visible.

**The approach loop** (`_run_approach_phase` in `inspection.py`, ~400 lines) is a solid proportional controller: EMA-smoothed offsets to strafe/rotate/forward commands. When the target is visible, this works well. The problem is only the `target_visible=False` branch.

## What changes

Three surgical changes:

### Change 1: Expand `PerceptionResponse` with movement suggestion

Add two fields:

```python
class PerceptionResponse(BaseModel):
    target_visible: bool
    confidence: float = Field(ge=0.0, le=1.0)
    box_2d: list[int] | None = None
    path_clear: bool = True
    # NEW — spatial reasoning when target not directly visible
    suggested_direction: Literal["left", "right", "forward", "back", "up", "down", ""] = ""
    suggested_reason: str = ""
```

Structured fields, not free-text. `suggested_direction` is constrained. `suggested_reason` is for logging/narration only. Flash fills these **only when `target_visible=False`** — when the target is visible, the existing proportional controller handles everything.

### Change 2: Enrich the detect prompt

Current prompt only asks "find the object." New prompt adds spatial reasoning context:

```
Look at this drone camera image. Find the object matching this description: "{target}"

Return a JSON object with:
- target_visible: true ONLY if the described object is clearly visible...
- confidence: ...
- box_2d: ...
- path_clear: ...
- suggested_direction: When target_visible is false BUT you can infer where the target
  likely is from visual context (e.g., it's behind another object, around a corner,
  obscured by something you can see), suggest a single direction the drone should move
  to reveal it: "left", "right", "forward", "back", "up", "down".
  Empty string if target is visible or you cannot infer its location.
- suggested_reason: Brief explanation of why you suggest this direction.
  Empty string if suggested_direction is empty.
```

This is the critical piece. Flash already sees the scene — we're just asking it to reason about what's not visible based on what is.

### Change 3: Use suggestion in approach loop before blind search

In `_run_approach_phase`, the current `target_visible=False` branch (lines ~693-732):

```
Current flow:
  not visible -> increment blind count -> if too many -> blind search recovery -> if fails -> full search

New flow:
  not visible -> check suggested_direction
    -> if non-empty: execute one capped movement in that direction (max 40cm)
                     -> re-detect -> if now visible, continue approach normally
    -> if empty OR suggestion didn't work: fall through to existing blind search recovery
```

**Key constraints** (per gemini-fly advice):
- **Cap movement distance** — don't trust Flash's distance estimates. Use a fixed magnitude (e.g., 40cm for lateral, 30cm for vertical) regardless of what Flash implies
- **One attempt only** — try the suggestion once, then fall through to blind search. Don't loop on suggestions
- **Blind search stays as fallback** — the existing `_search_recovery()` is untouched, it's just no longer the first resort

## What gets removed (revert prior work)

The `ViewingAngle` enum, L-maneuver code, `REPOSITIONING` status, and the 4 L-maneuver config params that were added. All of it. The spatial reasoning now lives in Flash's perception, not in hardcoded Python geometry.

The user no longer says "inspect from behind" — they say "check the statue behind the box" and Flash figures out the navigation at each approach step.

### Files to revert:
- `client/src/models/tool_calls.py` — remove `ViewingAngle` enum and `viewing_angle` field from `StartInspectionParams`
- `client/src/models/mission.py` — remove `REPOSITIONING` status and its transitions
- `client/src/config.py` — remove 4 `LMANEUVER_*` params
- `client/src/mission/inspection.py` — remove `_run_l_maneuver`, `_reacquire_after_l_maneuver`, `viewing_angle` params from `run()` and `_run_inspection_phase()`, revert label logic in `_run_inspection_phase`
- `client/src/tool_handler.py` — remove `viewing_angle` threading
- `backend/src/models/tools.py` — remove `viewing_angle` from Gemini tool declaration
- `client/src/dashboard/static/app.js` — remove `repositioning` from mission-active check

## What stays unchanged

- The proportional controller (strafe/rotate/forward logic for visible targets)
- `_search_recovery()` — stays as fallback
- `_run_search_phase()` — stays as last resort
- `_run_inspection_phase()` — stays as-is (the 3-angle strafe capture)
- `_final_centering()` — stays as-is
- Report generation — unchanged

## File changes summary (new work)

| File | Change |
|---|---|
| `client/src/perception/visual.py` | Add 2 fields to `PerceptionResponse`, enrich detect prompt, update JSON schema |
| `client/src/mission/inspection.py` | Insert suggestion-follow branch before blind search in `_run_approach_phase` |
| `client/src/config.py` | Add 1 param: `SUGGESTION_MOVE_DISTANCE: int = 40` |

## Risk mitigations

1. **Flash hallucinating directions** — mitigated by capping distance and limiting to one attempt before fallback
2. **Slower approach** — adds one extra Flash API call when target not visible (before blind search). Acceptable since Flash is ~2-4s
3. **Prompt regression** — enriched prompt might degrade normal visible-target detection. Mitigate by keeping the new fields clearly conditional ("only fill when target_visible is false")

## Implementation order

1. Revert the ViewingAngle / L-maneuver code (all 7 files listed above)
2. Add `suggested_direction` + `suggested_reason` to `PerceptionResponse`
3. Update the detect prompt in `visual.py`
4. Update the JSON schema passed to Gemini's `generate_content()` to include new fields
5. Add suggestion-follow logic in `_run_approach_phase` before blind search
6. Add `SUGGESTION_MOVE_DISTANCE` config param
7. Test with simple case first ("inspect the bottle" — ensure no regression)
8. Test with spatial case ("inspect the statue behind the box")

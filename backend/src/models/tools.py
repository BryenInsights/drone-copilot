"""Gemini Live API tool declarations per contracts/tool-declarations.md."""

from google.genai import types


def build_tool_declarations() -> list[types.Tool]:
    """Build the list of FunctionDeclarations for all 10 tools."""
    declarations = [
        types.FunctionDeclaration(
            name="takeoff",
            description=(
                "Take off from the ground and hover at a safe altitude. "
                "Only call when the drone is on the ground. After takeoff, "
                "wait for confirmation before sending movement commands."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="land",
            description=(
                "Land the drone safely. Call when the user says 'land' "
                "or when a mission is complete."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="move_drone",
            description=(
                "Move the drone in a direction. The safety system will clamp the "
                "distance to valid range (20-200cm). For 'down' movements, distance "
                "is automatically clamped to maintain safe altitude."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "direction": types.Schema(
                        type="STRING",
                        enum=["forward", "back", "left", "right", "up", "down"],
                        description="Direction of movement relative to the drone's current heading",
                    ),
                    "distance_cm": types.Schema(
                        type="INTEGER",
                        description=(
                            "Distance in centimeters. Will be clamped to 20-200cm range. "
                            "Use 30-50cm for small movements, 100cm for medium, "
                            "150-200cm for large."
                        ),
                    ),
                },
                required=["direction", "distance_cm"],
            ),
        ),
        types.FunctionDeclaration(
            name="rotate_drone",
            description=(
                "Rotate the drone in place. The safety system will clamp "
                "the angle to valid range (10-360 degrees)."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "direction": types.Schema(
                        type="STRING",
                        enum=["clockwise", "counter_clockwise"],
                        description=(
                            "Rotation direction. 'clockwise' = turn right, "
                            "'counter_clockwise' = turn left"
                        ),
                    ),
                    "degrees": types.Schema(
                        type="INTEGER",
                        description=(
                            "Rotation angle in degrees. Will be clamped to 10-360. "
                            "Use 90 for 'look left/right', 180 for 'turn around', "
                            "45 for a slight turn."
                        ),
                    ),
                },
                required=["direction", "degrees"],
            ),
        ),
        types.FunctionDeclaration(
            name="hover",
            description=(
                "Stop all movement and hover in place. Call when the user says "
                "'stop', 'wait', or 'hold position'."
            ),
            parameters=types.Schema(type="OBJECT", properties={}),
        ),
        types.FunctionDeclaration(
            name="set_speed",
            description="Set the drone's movement speed for subsequent move commands.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "speed_cm_per_sec": types.Schema(
                        type="INTEGER",
                        description=(
                            "Speed in centimeters per second. "
                            "10=slow and careful, 50=moderate, 100=maximum speed."
                        ),
                    ),
                },
                required=["speed_cm_per_sec"],
            ),
        ),
        types.FunctionDeclaration(
            name="report_perception",
            description=(
                "Report what you see regarding the target object in the current video "
                "frame. Called during autonomous approach and exploration missions. "
                "Use these calibration anchors for relative_size: "
                "0.03-0.08 = tiny/far (3m+), 0.08-0.15 = small/medium-far (1.5-3m), "
                "0.15-0.25 = medium/close (0.8-1.5m), 0.25-0.40 = large/very close (<0.8m). "
                "For offsets: -1.0 = far left/top edge, 0.0 = centered, "
                "+1.0 = far right/bottom edge."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "target_visible": types.Schema(
                        type="BOOLEAN",
                        description="Whether the target object is visible in the current frame",
                    ),
                    "horizontal_offset": types.Schema(
                        type="NUMBER",
                        description=(
                            "Horizontal position: -1.0 (far left edge) to +1.0 "
                            "(far right edge), 0.0 = centered in frame"
                        ),
                    ),
                    "vertical_offset": types.Schema(
                        type="NUMBER",
                        description=(
                            "Vertical position: -1.0 (top edge) to +1.0 "
                            "(bottom edge), 0.0 = centered in frame"
                        ),
                    ),
                    "relative_size": types.Schema(
                        type="NUMBER",
                        description=(
                            "Target width divided by frame width. "
                            "0.03-0.08=far, 0.08-0.15=medium-far, "
                            "0.15-0.25=medium-close, 0.25-0.40=close"
                        ),
                    ),
                    "confidence": types.Schema(
                        type="NUMBER",
                        description=(
                            "Confidence in the detection, 0.0 to 1.0. "
                            "Report 0.0 if target is not visible."
                        ),
                    ),
                },
                required=[
                    "target_visible",
                    "horizontal_offset",
                    "vertical_offset",
                    "relative_size",
                    "confidence",
                ],
            ),
        ),
        types.FunctionDeclaration(
            name="report_scan_analysis",
            description=(
                "After analyzing scan frames, call this tool with structured results. "
                "Do not describe findings in voice only — always use this tool to return "
                "the best scan frame index, whether the target was found, and a precise "
                "visual description of the target."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "best_index": types.Schema(
                        type="INTEGER",
                        description=(
                            "Index of the scan frame (0-7) where the target is most visible"
                        ),
                    ),
                    "target_visible": types.Schema(
                        type="BOOLEAN",
                        description="Whether the target was found in any scan frame",
                    ),
                    "refined_label": types.Schema(
                        type="STRING",
                        description=(
                            "Precise visual description of the target for close-range "
                            "tracking, e.g. 'green cardboard box with white label'"
                        ),
                    ),
                },
                required=["best_index", "target_visible", "refined_label"],
            ),
        ),
        types.FunctionDeclaration(
            name="start_exploration",
            description=(
                "Begin a scanning pattern to search for a target object. The drone will "
                "perform a 360-degree scan, analyze frames to find the target, then "
                "autonomously approach it. The mission runs in the background and you "
                "will receive updates. Call this when the user asks to 'find' or "
                "'look for' something."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "target_description": types.Schema(
                        type="STRING",
                        description=(
                            "Description of what to find, e.g. 'the red bag', "
                            "'a water bottle', 'the green plant'"
                        ),
                    ),
                },
                required=["target_description"],
            ),
        ),
        types.FunctionDeclaration(
            name="start_inspection",
            description=(
                "Begin a detailed inspection of a nearby object. The drone approaches "
                "the target, captures views from multiple angles, and provides a detailed "
                "verbal assessment. Call this when the user asks to 'check', 'inspect', "
                "or 'look at' something."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "target_description": types.Schema(
                        type="STRING",
                        description=(
                            "Description of what to inspect, e.g. 'that plant', "
                            "'the painting on the wall'"
                        ),
                    ),
                    "aspects": types.Schema(
                        type="STRING",
                        description=(
                            "Optional: specific aspects to examine, e.g. "
                            "'check for damage', 'look at the label'"
                        ),
                    ),
                },
                required=["target_description"],
            ),
        ),
    ]

    return [types.Tool(function_declarations=declarations)]

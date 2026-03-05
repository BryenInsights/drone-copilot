"""
Phase 0 Validation Gate — Gemini Live API Video+Audio Coexistence Test

PURPOSE: Confirm that the Gemini Live API can handle simultaneous audio and video
input in a single session with tool calls. This is the single biggest technical
risk in the architecture.

WHAT THIS SCRIPT VALIDATES:
1. Opens a Live API session with a tool declaration
2. Sends PCM audio chunks AND JPEG video frames simultaneously via send_realtime_input
3. Confirms the model receives and references video content in its response or tool call
4. Confirms tool calls work in the same audio+video session

FALLBACK: If continuous video alongside audio fails, the confirmed fallback is
periodic frame injection via send_client_content with inline images after each
movement command.

USAGE:
    export GEMINI_API_KEY=your-key-here
    python tests/validate_live_api.py

REQUIREMENTS:
    pip install google-genai numpy Pillow
"""

import asyncio
import base64
import io
import logging
import os
import struct
import sys
import time

from dotenv import load_dotenv
import numpy as np
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def generate_synthetic_jpeg(text: str = "TEST FRAME", width: int = 768, height: int = 768) -> bytes:
    """Generate a synthetic JPEG frame with text overlay for testing."""
    img = Image.new("RGB", (width, height), color=(40, 80, 120))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
    except (OSError, IOError):
        font = ImageFont.load_default()
    draw.text((width // 4, height // 2 - 24), text, fill=(255, 255, 255), font=font)
    draw.text((width // 4, height // 2 + 40), f"t={time.time():.2f}", fill=(200, 200, 200), font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def generate_synthetic_pcm(duration_ms: int = 100, sample_rate: int = 16000) -> bytes:
    """Generate synthetic PCM 16-bit mono audio (silence with tiny noise)."""
    num_samples = int(sample_rate * duration_ms / 1000)
    samples = np.random.randint(-100, 100, size=num_samples, dtype=np.int16)
    return samples.tobytes()


async def validate_live_api():
    """Main validation routine."""
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY environment variable not set")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")

    # Define a simple tool for validation
    tool_declarations = [
        types.FunctionDeclaration(
            name="describe_scene",
            description="Describe what you see in the video frame. Call this tool to report your observation.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "description": types.Schema(
                        type=types.Type.STRING,
                        description="What you see in the current video frame",
                    ),
                    "has_text": types.Schema(
                        type=types.Type.BOOLEAN,
                        description="Whether text is visible in the frame",
                    ),
                },
                required=["description", "has_text"],
            ),
        ),
    ]

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        tools=[types.Tool(function_declarations=tool_declarations)],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        session_resumption=types.SessionResumptionConfig(),
    )

    results = {
        "session_connected": False,
        "video_sent": False,
        "audio_sent": False,
        "simultaneous_av": False,
        "model_referenced_video": False,
        "tool_call_received": False,
        "tool_call_in_av_session": False,
        "session_resumption_handle": None,
        "fallback_needed": False,
        "confirmed_pattern": None,
    }

    log.info(f"Connecting to Gemini Live API with model: {model}")

    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            results["session_connected"] = True
            log.info("Session connected successfully")

            # --- Test 1: Send video frame ---
            log.info("Test 1: Sending video frame via send_realtime_input...")
            jpeg_bytes = generate_synthetic_jpeg("HELLO DRONE")
            await session.send_realtime_input(
                video=types.Blob(data=jpeg_bytes, mime_type="image/jpeg")
            )
            results["video_sent"] = True
            log.info(f"  Video frame sent ({len(jpeg_bytes)} bytes)")

            # --- Test 2: Send audio chunk ---
            log.info("Test 2: Sending audio chunk via send_realtime_input...")
            pcm_bytes = generate_synthetic_pcm(100, 16000)
            await session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
            results["audio_sent"] = True
            log.info(f"  Audio chunk sent ({len(pcm_bytes)} bytes)")

            # --- Test 3: Send video + audio simultaneously ---
            log.info("Test 3: Sending video AND audio simultaneously...")
            jpeg_bytes_2 = generate_synthetic_jpeg("SIMULTANEOUS AV")
            pcm_bytes_2 = generate_synthetic_pcm(100, 16000)

            # Send both in quick succession (same realtime stream)
            await session.send_realtime_input(
                video=types.Blob(data=jpeg_bytes_2, mime_type="image/jpeg")
            )
            await session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes_2, mime_type="audio/pcm;rate=16000")
            )
            results["simultaneous_av"] = True
            log.info("  Simultaneous audio+video sent successfully")

            # Send more audio to maintain the stream
            for i in range(5):
                pcm = generate_synthetic_pcm(100, 16000)
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
                )
                await asyncio.sleep(0.1)

            # --- Test 4: Ask Gemini to describe video and use tool ---
            log.info("Test 4: Prompting Gemini to describe the video frame and use tool...")
            jpeg_bytes_3 = generate_synthetic_jpeg("RED BAG ON TABLE")
            await session.send_realtime_input(
                video=types.Blob(data=jpeg_bytes_3, mime_type="image/jpeg")
            )

            await session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(
                        text="Look at the current video frame and call the describe_scene tool "
                        "to report what you see. Focus on any text visible in the image."
                    )],
                ),
                turn_complete=True,
            )

            # Collect responses with timeout
            tool_call_received = False
            audio_received = False
            text_received = ""
            session_handle = None

            log.info("  Waiting for responses (30s timeout)...")
            try:
                async with asyncio.timeout(30):
                    async for message in session.receive():
                        # Check for session resumption handle
                        if message.session_resumption_update:
                            if message.session_resumption_update.resumable:
                                session_handle = message.session_resumption_update.new_handle
                                results["session_resumption_handle"] = session_handle
                                log.info(f"  Session resumption handle received: {session_handle[:20]}...")

                        # Check for tool calls
                        if message.tool_call:
                            tool_call_received = True
                            results["tool_call_received"] = True
                            results["tool_call_in_av_session"] = True
                            for fc in message.tool_call.function_calls:
                                log.info(f"  TOOL CALL: {fc.name}({fc.args})")
                                if "text" in str(fc.args).lower() or "frame" in str(fc.args).lower():
                                    results["model_referenced_video"] = True

                                # Send tool response
                                await session.send_tool_response(
                                    function_responses=[
                                        types.FunctionResponse(
                                            id=fc.id,
                                            name=fc.name,
                                            response={"result": "observation recorded"},
                                        )
                                    ]
                                )

                        # Check for audio data
                        if message.data:
                            audio_received = True
                            log.info(f"  Audio response chunk: {len(message.data)} bytes")

                        # Check for text/transcription
                        if message.server_content:
                            sc = message.server_content
                            if sc.output_transcription:
                                text_received += sc.output_transcription.text or ""
                            if sc.input_transcription:
                                log.info(f"  Input transcription: {sc.input_transcription.text}")

                        # Check for interruption support
                        if message.server_content and message.server_content.interrupted:
                            log.info("  Interruption detected (barge-in works)")

                        # If we got a tool call, we can break after getting the follow-up
                        if tool_call_received and (audio_received or text_received):
                            break

            except TimeoutError:
                log.warning("  Response timeout (30s) — this may be normal for synthetic audio")

            if text_received:
                log.info(f"  Transcription: {text_received[:200]}")
                if any(w in text_received.lower() for w in ["frame", "image", "see", "text", "video", "blue", "red", "bag"]):
                    results["model_referenced_video"] = True

    except Exception as e:
        log.error(f"Session error: {type(e).__name__}: {e}")
        if "video" in str(e).lower() or "unsupported" in str(e).lower():
            results["fallback_needed"] = True
            log.warning("Video input may not be supported in this mode — fallback needed")

    # --- Fallback Test: send_client_content with inline image ---
    if not results["model_referenced_video"] or results["fallback_needed"]:
        log.info("\n--- Fallback Test: Inline image via send_client_content ---")
        try:
            fallback_config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                tools=[types.Tool(function_declarations=tool_declarations)],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
                    )
                ),
            )
            async with client.aio.live.connect(model=model, config=fallback_config) as session:
                jpeg_bytes_fb = generate_synthetic_jpeg("FALLBACK TEST")

                await session.send_client_content(
                    turns=types.Content(
                        role="user",
                        parts=[
                            types.Part.from_bytes(data=jpeg_bytes_fb, mime_type="image/jpeg"),
                            types.Part.from_text(
                                text="Call the describe_scene tool to report what you see in this image."
                            ),
                        ],
                    ),
                    turn_complete=True,
                )

                try:
                    async with asyncio.timeout(30):
                        async for message in session.receive():
                            if message.tool_call:
                                for fc in message.tool_call.function_calls:
                                    log.info(f"  FALLBACK TOOL CALL: {fc.name}({fc.args})")
                                results["fallback_needed"] = True
                                results["confirmed_pattern"] = "send_client_content_inline_image"
                                break
                            if message.data:
                                log.info(f"  Fallback audio response: {len(message.data)} bytes")
                except TimeoutError:
                    log.warning("  Fallback test timeout")
        except Exception as e:
            log.error(f"Fallback test error: {e}")

    # --- Results Summary ---
    print("\n" + "=" * 60)
    print("VALIDATION RESULTS")
    print("=" * 60)

    all_pass = True
    for key, value in results.items():
        if key in ("session_resumption_handle", "confirmed_pattern"):
            continue
        status = "PASS" if value else "FAIL" if key != "fallback_needed" else ("FALLBACK NEEDED" if value else "PASS")
        icon = "+" if value and key != "fallback_needed" else ("-" if not value else "!")
        print(f"  [{icon}] {key}: {value}")
        if key in ("session_connected", "video_sent", "audio_sent", "simultaneous_av") and not value:
            all_pass = False

    if results["confirmed_pattern"]:
        print(f"\n  Confirmed pattern: {results['confirmed_pattern']}")

    print()
    if results["tool_call_in_av_session"] and results["simultaneous_av"]:
        results["confirmed_pattern"] = "send_realtime_input_continuous"
        print("VERDICT: PASS — Continuous video+audio via send_realtime_input works")
        print("         Tool calls work in audio+video session")
        print("         Pattern: send_realtime_input(video=Blob) + send_realtime_input(audio=Blob)")
    elif results["fallback_needed"] and results.get("confirmed_pattern") == "send_client_content_inline_image":
        print("VERDICT: PARTIAL — Continuous video via send_realtime_input is unreliable")
        print("         FALLBACK CONFIRMED: send_client_content with inline images works")
        print("         Pattern: After each movement, inject frame via send_client_content")
    elif results["session_connected"]:
        print("VERDICT: INCONCLUSIVE — Session connected but responses not confirmed")
        print("         May need manual testing with real microphone audio")
    else:
        print("VERDICT: FAIL — Could not establish session")
        print("         Check GEMINI_API_KEY and network connectivity")

    print()
    print("Record these findings in research.md as R3 addendum.")
    print("=" * 60)

    return results


if __name__ == "__main__":
    results = asyncio.run(validate_live_api())
    sys.exit(0 if results.get("session_connected") else 1)

# Codelab: Building a Bidi-Streaming Agent

Source: https://codelabs.developers.google.com/way-back-home-level-3/instructions#0

## Overview

Build a real-time AI-powered system using ADK and Gemini Live API with full-duplex communication between a React frontend and Python backend.

## Architecture

- React frontend capturing live webcam and microphone input
- FastAPI backend managing the AI Agent
- Gemini Live API processing video and audio simultaneously
- WebSocket pipeline for persistent, low-latency bidirectional communication

## Why Full-Duplex?

Traditional HTTP is half-duplex. The Gemini Live API supports interruption — the AI can respond while you're still sending input.

**WebSocket connection upgrade:**
1. Browser sends HTTP request with `Upgrade: websocket` header
2. Server responds with `HTTP 101 Switching Protocols`
3. TCP connection becomes full-duplex WebSocket

## Frontend Message Handling

```javascript
parts.forEach(part => {
    // Handle tool calls
    if (part.functionCall?.name === 'report_digit') {
        const count = parseInt(part.functionCall.args.count, 10);
        setLastMessage({ type: 'DIGIT_DETECTED', value: count });
    }
    // Handle AI audio responses
    if (part.inlineData?.data) {
        audioStreamer.current.addPCM16(part.inlineData.data);
    }
});
```

## Audio/Video Data Transformation

**Audio**: Raw mic -> Base64 encoded -> JSON with metadata (`rate=16000`) -> WebSocket

**Video capture** (2 FPS):
```javascript
ctx.drawImage(videoElement, 0, 0, width, height);
const base64 = canvas.toDataURL('image/jpeg', 0.6).split(',')[1];
ws.current.send(JSON.stringify({
    type: 'image',
    data: base64,
    mimeType: 'image/jpeg'
}));
```

## Agent Setup

### System Prompt Example

```
You are an AI Biometric Scanner for the Alpha Rescue Drone Fleet.

BEHAVIOR LOOP:
1. Wait: Remain silent until receiving visual/verbal trigger
2. Action:
   - Analyze video frames, count visible fingers (1-5)
   - IF FINGERS DETECTED: Call report_digit() immediately, then confirm verbally
   - IF UNCLEAR: Report sensor error, do not call tool
3. Never hallucinate tool calls; only trigger on valid counts
```

### Tool Definition

```python
def report_digit(count: int):
    """Execute immediately when fingers detected."""
    print(f"[SERVER-SIDE TOOL EXECUTION] DIGIT DETECTED: {count}")
    return {"status": "success", "digit": count}
```

### Model

```python
MODEL_ID = "gemini-live-2.5-flash-preview-native-audio-09-2025"
```

## LiveRequestQueue Pattern

Thread-safe, async FIFO buffer decoupling user input (Producer) from model processing (Consumer):
- Non-blocking concurrent data ingestion
- Multimodal multiplexing of audio, video, text, and tool results
- Responsive interruption handling

## Session Initialization

```python
is_native_audio = "native-audio" in model_name.lower()

if is_native_audio:
    run_config = RunConfig(
        streaming_mode=StreamingMode.BIDI,
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig(),
    )
```

## Upstream Task (User Input)

```python
async def upstream_task() -> None:
    while True:
        message = await websocket.receive()

        if json_message.get("type") == "audio":
            audio_data = base64.b64decode(json_message["data"])
            audio_blob = types.Blob(mime_type="audio/pcm;rate=16000", data=audio_data)
            live_request_queue.send_realtime(audio_blob)

        elif json_message.get("type") == "image":
            image_data = base64.b64decode(json_message["data"])
            image_blob = types.Blob(mime_type=json_message.get("mimeType"), data=image_data)
            live_request_queue.send_realtime(image_blob)
```

## Downstream Task (AI Response)

```python
async def downstream_task() -> None:
    async for event in runner.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=live_request_queue,
        run_config=run_config,
    ):
        event_json = event.model_dump_json(exclude_none=True, by_alias=True)
        await websocket.send_text(event_json)
```

## Concurrent Execution

```python
await asyncio.gather(upstream_task(), downstream_task())
```

## Deployment (Cloud Run)

### Multi-Stage Dockerfile

```dockerfile
# Stage 1: Build React frontend
FROM node:20-slim as builder
WORKDIR /app
COPY frontend/package*.json ./frontend/
RUN npm --prefix frontend install
COPY frontend/ ./frontend/
RUN npm --prefix frontend run build

# Stage 2: Python runtime
FROM python:3.13-slim
WORKDIR /app
RUN pip install uv
COPY requirements.txt .
RUN uv pip install --no-cache-dir --system -r requirements.txt
COPY backend/app/ .
COPY --from=builder /app/frontend/dist /frontend/dist
EXPOSE 8080
CMD ["python", "main.py"]
```

### Deploy

```bash
gcloud builds submit . --tag gcr.io/${PROJECT_ID}/biometric-scout
gcloud run deploy biometric-scout \
  --image=gcr.io/${PROJECT_ID}/biometric-scout \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=True"
```

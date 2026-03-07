# ADK Bidi-Streaming Development Guide

Source: https://google.github.io/adk-docs/streaming/dev-guide/part1/ (Parts 1-5)

## Table of Contents

- [Part 1: Intro to Streaming](#part-1-intro-to-streaming)
- [Part 2: Sending Messages](#part-2-sending-messages)
- [Part 3: Event Handling](#part-3-event-handling)
- [Part 4: Run Configuration](#part-4-run-configuration)
- [Part 5: Audio, Images, and Video](#part-5-audio-images-and-video)

---

## Part 1: Intro to Streaming

### What is Bidi-streaming?

Bidi-streaming enables real-time, two-way communication where both human and AI communicate simultaneously:

- **Two-way Communication**: Continuous data exchange without waiting for complete responses
- **Responsive Interruption**: Users can interrupt the agent mid-response
- **Multimodal Support**: Processes audio, video, and text through a single connection

### Live API Platforms

| Platform | Use Case | Session Limits | Concurrent |
|----------|----------|----------------|------------|
| **Gemini Live API** (AI Studio) | Dev/prototyping | 15min audio, 2min audio+video | 50-1000 |
| **Vertex AI Live API** (GCP) | Production | 10 minutes | Up to 1,000/project |

ADK abstracts differences via `GOOGLE_GENAI_USE_VERTEXAI` env var.

### Architecture Components

- **LiveRequestQueue**: Thread-safe async queue buffering user messages
- **Runner**: Execution engine with `run_live()` streaming interface
- **Agent**: Defines AI capabilities with model, tools, and instructions
- **RunConfig**: Configures streaming behavior (modalities, transcription, resumption)

### Application Lifecycle

#### Phase 1: Initialization (once)

```python
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

agent = Agent(
    name="search_agent",
    model="gemini-2.5-flash-native-audio-preview-12-2025",
    tools=[google_search],
    instruction="You are a helpful assistant."
)

session_service = InMemorySessionService()
runner = Runner(app_name="my-app", agent=agent, session_service=session_service)
```

#### Phase 2: Session Setup (per connection)

```python
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.genai import types

run_config = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=["AUDIO"],
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig()
)

live_request_queue = LiveRequestQueue()
```

#### Phase 3: Bidirectional Streaming

```python
async def upstream_task():
    """WebSocket -> LiveRequestQueue"""
    try:
        while True:
            data = await websocket.receive_text()
            content = types.Content(parts=[types.Part(text=data)])
            live_request_queue.send_content(content)
    except WebSocketDisconnect:
        pass

async def downstream_task():
    """run_live() -> WebSocket"""
    async for event in runner.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=live_request_queue,
        run_config=run_config
    ):
        await websocket.send_text(
            event.model_dump_json(exclude_none=True, by_alias=True)
        )

try:
    await asyncio.gather(upstream_task(), downstream_task(), return_exceptions=True)
finally:
    live_request_queue.close()
```

#### Phase 4: Termination

```python
live_request_queue.close()
```

---

## Part 2: Sending Messages

### LiveRequestQueue

Unified `LiveRequest` container:

```python
class LiveRequest(BaseModel):
    content: Optional[Content] = None       # Text-based content
    blob: Optional[Blob] = None             # Audio/video data
    activity_start: Optional[ActivityStart]  # User activity start
    activity_end: Optional[ActivityEnd]      # User activity end
    close: bool = False                     # Connection termination
```

**Important:** `content` and `blob` are mutually exclusive.

### Sending Text

```python
content = types.Content(parts=[types.Part(text="Your message")])
live_request_queue.send_content(content)
```

### Sending Audio/Video

```python
audio_blob = types.Blob(mime_type="audio/pcm;rate=16000", data=audio_data)
live_request_queue.send_realtime(audio_blob)
```

### Activity Signals (manual VAD)

```python
live_request_queue.send_activity_start()   # User started speaking
live_request_queue.send_activity_end()     # User finished speaking
```

### Control Signals

```python
live_request_queue.close()  # Terminate connection
```

**Important:** In BIDI mode, you MUST manually call `close()`.

### Best Practices

- Always create `LiveRequestQueue` within an async context
- FIFO ordering guaranteed, no coalescing
- Unbounded queue — monitor `_queue.qsize()` in production
- Always close in `finally` blocks

---

## Part 3: Event Handling

### run_live() Method

```python
async def run_live(
    self, *, user_id, session_id, live_request_queue, run_config, session
) -> AsyncGenerator[Event, None]:
```

### Event Types

| Type | Description |
|------|-------------|
| **Text Events** | Model responses with `partial`, `turn_complete`, `interrupted` flags |
| **Audio Events (Inline)** | Raw bytes streamed in real-time (ephemeral) |
| **Audio Events (File)** | Aggregated audio stored in artifacts |
| **Metadata Events** | Token usage info |
| **Transcription Events** | Speech-to-text for input and output |
| **Tool Call Events** | Function requests — ADK executes automatically |
| **Error Events** | Failures with `error_code` and `error_message` |

### Key Event Fields

- `content`: Text, audio, or function calls
- `author`: Agent name or "user"
- `partial`: True = streaming chunk, False = complete text
- `turn_complete`: Model finished its response
- `interrupted`: User interrupted model output
- `usage_metadata`: Token counts

### Flag Combinations

| Scenario | turn_complete | interrupted | Action |
|----------|---------------|-------------|--------|
| Normal completion | True | False | Enable input |
| User interrupted | False | True | Stop display |
| Interrupted at end | True | True | Same as completion |

### Audio Transmission Optimization

Base64 in JSON adds ~133% overhead. For production, use binary WebSocket frames:

```python
if has_audio:
    for part in event.content.parts:
        if part.inline_data:
            await websocket.send_bytes(part.inline_data.data)
    metadata = event.model_dump_json(
        exclude={'content': {'parts': {'__all__': {'inline_data'}}}},
        by_alias=True
    )
    await websocket.send_text(metadata)
```

### Automatic Tool Execution

ADK handles tool execution automatically:
1. Detects function calls in streaming responses
2. Executes tools in parallel
3. Formats responses per Live API requirements
4. Sends responses back transparently
5. Yields both call and response events

### Error Handling

**Terminal Errors** (break): `SAFETY`, `PROHIBITED_CONTENT`, `BLOCKLIST`, `MAX_TOKENS`

**Transient Errors** (continue with backoff): `UNAVAILABLE`, `DEADLINE_EXCEEDED`, `RESOURCE_EXHAUSTED`

### Multi-Agent Workflows (SequentialAgent)

- Each agent gets auto-added `task_completed()` function
- Single `LiveRequestQueue` shared across agents
- Track agent transitions via `event.author`

---

## Part 4: Run Configuration

### Key RunConfig Parameters

```python
run_config = RunConfig(
    response_modalities=["AUDIO"],            # TEXT or AUDIO
    streaming_mode=StreamingMode.BIDI,         # BIDI or SSE
    session_resumption=types.SessionResumptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        trigger_tokens=100000,
        sliding_window=types.SlidingWindow(target_tokens=80000)
    ),
    custom_metadata={"user_tier": "premium"},
    save_live_blob=True,
    support_cfc=False,  # Compositional function calling (experimental)
)
```

### BIDI vs SSE

- **BIDI**: Persistent WebSocket, real-time bidirectional, supports audio/video, auto-closes ~10min
- **SSE**: HTTP streaming, text-only, traditional request-response

### Session Resumption

When enabled, ADK auto-manages Live API session resumption handles, detecting connection closures around 10min and reconnecting transparently.

### Context Window Compression

Eliminates session duration limits by summarizing older conversation portions while preserving recent context.

### Quota Management

- **Direct Mapping**: One Live API session per user (simple)
- **Session Pooling with Queueing**: Track active sessions, queue when quota reached

---

## Part 5: Audio, Images, and Video

### Audio Input

- Format: 16-bit PCM at 16kHz mono
- No format conversion — mismatched audio causes degradation
- Chunk sizing:
  - Ultra-low latency: 10-20ms
  - Balanced (recommended): 50-100ms
  - Lower overhead: 100-200ms
- VAD processes audio continuously (no manual turn signals needed)

### Audio Output

- Format: 16-bit PCM at 24kHz mono
- MIME type: `audio/pcm;rate=24000`
- google.genai lib auto-decodes base64 to raw bytes

### Images and Video

- Format: JPEG images, frame-by-frame
- Recommended: 1 FPS maximum, 768x768 pixels
- Suitable for recognition, not motion tracking

### Audio Model Architectures

**Native Audio** (e.g., `gemini-2.5-flash-native-audio-preview-12-2025`):
- End-to-end audio processing, no text intermediate
- More natural prosody, affective dialog, proactive responses
- Extended voice library, automatic language detection

**Half-Cascade**:
- Native audio input + TTS-based output
- Better production reliability, explicit language control
- Supports TEXT alongside AUDIO response modality

### Audio Transcription

Enabled by default. Access via:
```python
event.input_transcription   # User's spoken words
event.output_transcription  # Model's spoken words
```

Each has `.text` and `.finished` fields. Always null-check both.

In multi-agent scenarios, transcription is auto-enabled regardless of RunConfig.

### Voice Configuration

```python
from google.genai import types

custom_llm = Gemini(
    model="gemini-2.5-flash-native-audio-preview-12-2025",
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
        )
    )
)
agent = Agent(model=custom_llm, ...)
```

### Voice Activity Detection (VAD)

- Enabled by default for hands-free conversation
- Disable only for push-to-talk or client-side VAD scenarios
- When disabled, use `send_activity_start()`/`send_activity_end()` signals

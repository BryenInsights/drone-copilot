# ADK Bidi-Streaming Demo - Reference Implementation

Source: https://github.com/google/adk-samples/tree/main/python/agents/bidi-demo

## Overview

FastAPI-based demo of Google's ADK showcasing real-time bidirectional WebSocket streaming with Gemini models. Supports text, audio, and image inputs with flexible response modes.

## Architecture

```
WebSocket Client -> LiveRequestQueue -> Live API Session
      ^                                       |
      <-------- run_live() Event Stream ------<
```

## Project Structure

```
bidi-demo/
├── app/
│   ├── main.py                          # FastAPI app, WebSocket at /ws/{user_id}/{session_id}
│   ├── google_search_agent/
│   │   └── agent.py                     # Agent config with google_search tool
│   ├── static/
│   │   ├── index.html                   # UI with text/audio modes
│   │   └── js/
│   │       ├── app.js                   # WebSocket communication
│   │       ├── audio-recorder.js        # Audio capture
│   │       └── audio-player.js          # Audio playback
│   └── .env                             # Configuration
└── pyproject.toml
```

## Environment Configuration

```env
GOOGLE_API_KEY=your_key           # For Gemini Live API
# OR
GOOGLE_GENAI_USE_VERTEXAI=TRUE    # For Vertex AI

DEMO_AGENT_MODEL=gemini-2.5-flash-native-audio-preview-12-2025
```

## WebSocket Message Formats

### Client -> Server

**Text:**
```json
{"type": "text", "text": "Your message"}
```

**Image:**
```json
{"type": "image", "data": "base64_data", "mimeType": "image/jpeg"}
```

**Audio:** Binary PCM frames (16kHz, 16-bit)

### Server -> Client

Events from `run_live()` serialized via `event.model_dump_json(exclude_none=True, by_alias=True)`

## Modality Detection

- **Native Audio Models**: `response_modalities=["AUDIO"]` with transcription
- **Half-Cascade Models**: `response_modalities=["TEXT"]` (no audio transcription)

## Key Features

- Automatic audio transcription
- Session resumption via `SessionResumptionConfig`
- Event console for monitoring Live API events
- Google Search integration via agent tools
- Concurrent upstream/downstream task handling

## Running

```bash
cd python/agents/bidi-demo
uv sync  # or: pip install -e .
cd app
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

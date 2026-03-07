# Gemini Live API - Official Documentation

Source: https://ai.google.dev/gemini-api/docs/live

## Overview

The Live API is a low-latency bidirectional streaming API that supports text, audio, and video input with audio and text output. It enables natural, interruptible conversations with video understanding capabilities.

## Key Features

- **Voice Activity Detection**: Automatic interruption handling
- **Tool Use & Function Calling**: Integrate tools with live conversations
- **Session Management**: Handle long-running conversations
- **Ephemeral Tokens**: Secure client-side authentication
- **Native Audio Support**: Direct audio processing at 24kHz output

## Implementation Approaches

**Server-to-Server**: Backend connects via WebSockets, forwarding client streams to Live API.

**Client-to-Server**: Frontend connects directly via WebSockets, offering better performance but requiring ephemeral tokens in production for security.

## Audio Specifications

- **Input format**: 16-bit PCM, 16kHz, mono
- **Output format**: 16-bit PCM, 24kHz, mono
- **Chunk size**: 1024 bytes typical

## Model Configuration

```python
MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
CONFIG = {
    "response_modalities": ["AUDIO"],
    "system_instruction": "You are a helpful and friendly AI assistant."
}
```

## Session Duration Limits

### Gemini Live API (Google AI Studio)
- Audio-only: 15 minutes
- Audio+video: 2 minutes
- Concurrent sessions: 50 (Tier 1) to 1,000 (Tier 2+)

### Vertex AI Live API (Google Cloud)
- All sessions: 10 minutes
- Concurrent sessions: Up to 1,000 per project

## Video Input Specifications

- Format: JPEG images (frame-by-frame, not video streaming)
- Recommended frame rate: 1 FPS maximum
- Recommended resolution: 768x768 pixels

## Partner Integrations

Pipecat, LiveKit, Fishjam, ADK, Vision Agents, Voximplant.

## Key Notebooks & Demos

- `intro_multimodal_live_api.ipynb` - Direct API access (text-to-text and text-to-audio)
- `intro_multimodal_live_api_genai_sdk.ipynb` - Gen AI SDK integration
- `live_api_quickstart.ipynb` - Quick start
- `real_time_rag_bank_loans_gemini_2_0.ipynb` - RAG use case
- `real_time_rag_retail_gemini_2_0.ipynb` - Retail RAG use case

Demo apps available at: https://github.com/GoogleCloudPlatform/generative-ai/tree/main/gemini/multimodal-live-api

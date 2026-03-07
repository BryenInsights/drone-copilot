# GenMedia Live - Sample App (Live API + Gen Media)

Source: https://github.com/GoogleCloudPlatform/generative-ai/tree/main/vision/sample-apps/genmedia-live

## Overview

Multimodal AI creation app extending Gemini Live API with image and video generation capabilities. Enables real-time voice/text interaction to generate and edit media.

## Capabilities

1. Real-time voice conversations (Gemini Live API)
2. Text-based messaging
3. Live camera feed sharing
4. Screen capture
5. Image file uploads
6. Generative image creation/modification (Gemini Pro Image)
7. AI-powered video synthesis (Veo)
8. Frame extraction from videos (ffmpeg)
9. Video concatenation (ffmpeg)
10. 30-minute session persistence with auto-reconnection

## Technical Requirements

- Python 3.10+
- Google Cloud project with Vertex AI API enabled
- FFmpeg installed

## Project Structure

```
genmedia-live/
├── app.py          # Flask backend
├── index.html      # UI
├── style.css
├── requirements.txt
├── outputs/        # Generated assets
└── src/
    ├── main.js
    ├── ui.js
    └── features/
        └── genmedia-chat.js
```

## Authentication

1. Enter Google Cloud Project ID
2. Get access token via Cloud Shell
3. Validate credentials in app
4. Tokens expire after ~1 hour

## Deployment

Supports Cloud Run deployment for serverless hosting.

**License:** Apache 2.0

# MCP Servers for Google Cloud GenMedia APIs

Source: https://github.com/GoogleCloudPlatform/vertex-ai-creative-studio/tree/main/experiments/mcp-genmedia

## Overview

Model Context Protocol (MCP) servers enabling AI agents to integrate Google Cloud's generative media APIs and audio/video compositing.

## Available Servers & Capabilities

| Server | Capability |
|--------|-----------|
| **Gemini** | Image generation/editing from prompts (Gemini 3 Pro & 2.5 Flash) |
| **Imagen** | Image generation/editing (Imagen 3 & 4) |
| **Veo** | Video creation from text or images (Veo 2 & 3.1) |
| **Chirp 3 HD** | High-quality speech synthesis |
| **Lyria** | Music generation |
| **AVTool** | Audio/video compositing, combining, concatenation, format conversion |

## Setup

```bash
git clone https://github.com/GoogleCloudPlatform/vertex-ai-creative-studio.git
cd vertex-ai-creative-studio/experiments/mcp-genmedia
```

## Environment Variables

```env
PROJECT_ID=your-gcp-project
LOCATION=us-central1
PORT=8080
GENMEDIA_BUCKET=your-gcs-bucket
```

## Authentication

```bash
# Option 1: Application default credentials
gcloud auth application-default login

# Option 2: Service account key
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json

# Grant bucket access
gcloud storage buckets add-iam-policy-binding gs://BUCKET_NAME \
  --member=user:user@email.com \
  --role=roles/storage.objectUser
```

## Running

```bash
# Default (stdio transport)
mcp-imagen-go

# HTTP transport
mcp-imagen-go --transport http
```

## Client Integrations

Sample agents provided for: geminicli, Google ADK, Google Firebase Genkit.

## Project Structure

```
experiments/mcp-genmedia/
├── assets/
├── mcp-genmedia-go/
├── sample-agents/
└── README.md
```

**License:** Apache 2.0 (not an officially supported Google product)

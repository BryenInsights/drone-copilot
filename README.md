# Gemi-fly

**Voice-controlled drone copilot powered by Gemini Live API**

[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://python.org)
[![Gemini Live API](https://img.shields.io/badge/Gemini-Live%20API-4285F4.svg)](https://ai.google.dev)
[![Cloud Run](https://img.shields.io/badge/Google%20Cloud-Run-4285F4.svg)](https://cloud.google.com/run)

Gemi-fly lets you have a natural voice conversation with a DJI Tello drone while it streams live video to Google's Gemini AI. Say "take off," ask "what do you see?", or command "find the red bag" — the AI responds verbally in real time, controls the drone, and autonomously searches for objects using computer vision.

<!-- ![Dashboard Screenshot](docs/dashboard-screenshot.png) -->

---

## Try it without a drone

Most judges won't have a Tello drone — **no hardware required**. The demo mode replays pre-recorded missions through the same dashboard used for live flights.

```bash
# Clone and install
git clone https://github.com/<your-org>/drone-copilot.git
cd drone-copilot
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r client/requirements.txt

# Launch demo dashboard
python -m client.src.dashboard.demo_main
```

Open **http://localhost:8081** in your browser, select a demo, and click **Start**.

---

## Watch the demo

<!-- 🎥 [Demo Video (< 4 min)](https://youtube.com/watch?v=PLACEHOLDER) -->

*Demo video link will be added before submission.*

---

## Architecture

```
┌─────────────────────┐     WebSocket      ┌──────────────────────┐
│   LOCAL CLIENT      │◄──────────────────►│   GCP BACKEND        │
│   (MacBook)         │  audio + video →   │   (Cloud Run)        │
│                     │  ← voice + tools   │                      │
│  ┌───────────────┐  │                    │  ┌────────────────┐  │
│  │ DJI Tello     │  │                    │  │ Gemini Live    │  │
│  │ Drone (WiFi)  │  │                    │  │ API Session    │  │
│  └───────────────┘  │                    │  └────────────────┘  │
│  ┌───────────────┐  │                    └──────────────────────┘
│  │ Mic + Speaker │  │
│  └───────────────┘  │
│  ┌───────────────┐  │
│  │ Web Dashboard │  │     Browser
│  │ (FastAPI)     │──────► Live video, telemetry, transcript,
│  └───────────────┘  │     mission status, AI performance
└─────────────────────┘
```

**Split architecture**: The GCP Cloud Run backend hosts a persistent Gemini Live API session for real-time bidirectional audio and video analysis. The local Python client connects to the DJI Tello drone, captures audio/video, streams them to the backend, and executes validated drone commands. A web dashboard provides real-time monitoring for observers.

---

## Setup & run

### Prerequisites

- Python 3.13+
- Google Gemini API key
- DJI Tello drone (optional — mock mode available)
- macOS with microphone and speakers
- Docker (for deployment)

### Quick start

```bash
# Clone
git clone https://github.com/<your-org>/drone-copilot.git
cd drone-copilot

# Virtual environment
python3.13 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r client/requirements.txt
pip install -r backend/requirements.txt

# Configure
cp .env.example .env
# Edit .env: set GEMINI_API_KEY, USE_MOCK_DRONE=true

# Start backend (terminal 1)
cd backend && uvicorn src.main:app --host 0.0.0.0 --port 8080

# Start client (terminal 2)
cd client && python -m src.main

# Open dashboard
open http://localhost:8081
```

### With a real drone

1. Power on the Tello and connect your Mac to `TELLO-XXXXXX` WiFi
2. Set `USE_MOCK_DRONE=false` in `.env`
3. Deploy the backend to Cloud Run (see below) since your Mac loses internet on Tello WiFi
4. Run the client as above

### Voice commands

| Command | Action |
|---------|--------|
| "Take off" | Drone takes off and stabilizes |
| "Land" | Safe landing |
| "Move forward / back / left / right" | Directional movement |
| "Look left / right" | Rotate drone |
| "What do you see?" | AI describes camera view |
| "Find the [object]" | Autonomous exploration mission |
| "Check that [object]" | Multi-angle inspection mission |
| "Stop" | Halt all movement |

---

## Google Cloud deployment

The backend is deployed on **Google Cloud Run** with WebSocket support for persistent bidirectional streaming.

```bash
# One-time GCP setup
cd deploy/scripts
./setup-gcp.sh <YOUR_PROJECT_ID>

# Deploy
./deploy.sh <YOUR_PROJECT_ID> <YOUR_GEMINI_API_KEY>
```

Or with Terraform:

```bash
cd deploy/terraform
terraform init
terraform apply -var="project_id=<ID>" -var="image=gcr.io/<ID>/drone-copilot-backend" -var="gemini_api_key=<KEY>"
```

**Cloud Run configuration**: `--timeout=3600` (60 min WebSocket), `--session-affinity`, `--min-instances=1`, `/healthz` health check.

<!-- ![Cloud Run Screenshot](docs/cloudrun-screenshot.png) -->

---

## Built with

| Technology | Purpose |
|-----------|---------|
| **Gemini Live API** | Real-time bidirectional audio + video AI session |
| **google-genai SDK** | Python SDK for Gemini Live API |
| **Tool Calls** | Structured AI-to-drone command interface (10 tools) |
| **Multimodal Input** | Simultaneous audio + video streaming to Gemini |
| **Context Window Compression** | Unlimited session duration (sliding window) |
| **Session Resumption** | Survive WebSocket reconnections seamlessly |
| **Google Cloud Run** | Serverless backend with WebSocket support |
| **FastAPI + WebSocket** | Backend relay and local dashboard server |
| **DJI Tello + djitellopy** | Drone hardware control |
| **sounddevice** | Local microphone capture and speaker playback |
| **OpenCV** | Video frame capture, encoding, and processing |
| **Pydantic** | Schema validation for all models and tool calls |

### Gemini features used

- **Live API** — persistent bidirectional streaming session
- **Tool calls** — structured drone commands with schema validation
- **Multimodal** — simultaneous audio input + video frames + voice output
- **Context window compression** — sliding window for unlimited session length
- **Session resumption** — reconnect without losing conversation context
- **Voice configuration** — natural copilot persona (Puck voice)

---

## Project structure

```
drone-copilot/
├── backend/          # GCP Cloud Run backend (Gemini Live API relay)
│   ├── src/          # FastAPI, WebSocket relay, Gemini session
│   ├── Dockerfile
│   └── requirements.txt
├── client/           # Local client (drone + audio + dashboard)
│   ├── src/
│   │   ├── drone/    # Controller, safety guard, command executor
│   │   ├── audio/    # Mic capture, speaker playback
│   │   ├── video/    # Frame capture, encoding, streaming
│   │   ├── mission/  # Exploration and inspection missions
│   │   └── dashboard/ # Web UI, broadcaster, demo replay
│   ├── demos/        # Pre-recorded demo sessions
│   └── requirements.txt
├── deploy/           # Cloud Run deployment (scripts, Terraform)
└── specs/            # Feature specifications and design docs
```

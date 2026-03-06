# Tasks: Drone Copilot — Voice-Controlled Live Agent

**Input**: Design documents from `/specs/001-drone-copilot/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Not included — not explicitly requested in feature specification. Add test tasks separately if TDD is desired.

**Organization**: Tasks grouped by user story. Each story is independently testable after completion.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story (US1, US2, US3, US4)
- All paths relative to repository root

---

## Phase 0: Validation Gate (Blocking)

**Purpose**: Confirm the single biggest technical risk before any implementation begins

**CRITICAL**: Phase 1 MUST NOT start until T047 passes with a confirmed working pattern.

- [x] T047 [GATE] Validate Gemini Live API video+audio coexistence — create a minimal test script (`tests/validate_live_api.py`) that: (1) opens a Live API session with a simple tool declaration, (2) sends PCM audio chunks AND JPEG video frames simultaneously via `send_realtime_input`, (3) confirms the model receives and references the video content in its response or tool call, (4) confirms tool calls work in the same audio+video session. If continuous video alongside audio fails, document the confirmed fallback: periodic frame injection via `send_client_content` with inline images after each movement command. This resolves the "NEEDS CLARIFICATION" on Gemini Live API video input from plan.md. Record the confirmed working pattern (exact SDK calls, frame format, rate limits) in research.md as R3 addendum. Do NOT proceed to Phase 1 until this test passes.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization, directory structure, and dependency management

- [x] T001 Create project directory structure with all `__init__.py` files per plan.md: `backend/src/`, `backend/src/models/`, `backend/tests/`, `client/src/`, `client/src/drone/`, `client/src/video/`, `client/src/audio/`, `client/src/mission/`, `client/src/dashboard/`, `client/src/dashboard/static/`, `client/src/models/`, `client/tests/`, `client/demos/exploration_demo/frames/`, `client/demos/inspection_demo/frames/`, `deploy/scripts/`, `deploy/terraform/`, `docs/`, `tests/`
- [x] T002 [P] Create backend/requirements.txt with dependencies: fastapi, uvicorn[standard], websockets, google-genai, pydantic, pydantic-settings
- [x] T003 [P] Create client/requirements.txt with dependencies: djitellopy, opencv-python-headless, sounddevice, numpy, websockets, fastapi, uvicorn[standard], pydantic, pydantic-settings
- [x] T004 [P] Create .env.example with all configuration variables: GEMINI_API_KEY, GEMINI_MODEL (default gemini-2.0-flash-live-001), VOICE_NAME (default Puck), BACKEND_URL (default ws://localhost:8080/ws), USE_MOCK_DRONE (default true), DASHBOARD_PORT (default 8081)
- [x] T005 [P] Create pyproject.toml with ruff configuration (line-length=100, target Python 3.13, select=["E","F","I","W"]) and project metadata

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core Pydantic models, configuration, and drone control infrastructure shared by ALL user stories

**CRITICAL**: No user story work can begin until this phase is complete

### Pydantic Models (all parallelizable — different files)

- [x] T006 [P] Create client configuration module with all LESSONS_LEARNED constants (drone hardware thresholds, Gemini API settings, approach controller gains, timing delays) as Pydantic Settings model in client/src/config.py — load from .env file, include: MIN_MOVE_DISTANCE=20, MAX_MOVE_DISTANCE=200, POST_TAKEOFF_STABILIZATION=4.0, INTER_COMMAND_MOVE_DELAY=2.0, INTER_COMMAND_ROTATE_DELAY=2.5, HEARTBEAT_INTERVAL=10, BATTERY_MIN_CONTINUE=20, BATTERY_MIN_TAKEOFF=25, TEMPERATURE_MAX=80, all approach controller constants from data-model.md; also add structured logging setup: configure Python logging with DEBUG/INFO/WARNING/ERROR levels, format with ISO timestamps and module names, level configurable via LOG_LEVEL env var (default INFO), apply to root logger so all modules inherit
- [x] T007 [P] Create backend configuration module as Pydantic Settings model in backend/src/config.py — load GEMINI_API_KEY, GEMINI_MODEL, VOICE_NAME, system prompt text (copilot persona per FR-019), frame_rate_to_gemini=1.0, audio rates (16kHz in, 24kHz out), audio_chunk_ms=100
- [x] T008 [P] Create DroneState and Telemetry Pydantic models in client/src/models/drone_state.py — DroneState with battery (0-100), altitude (>=0), temperature (0-100), is_flying (derived from altitude>10), is_connected, speed (10-100), flight_time, wifi_snr, takeoff_time; Telemetry as dashboard-optimized subset per data-model.md
- [x] T009 [P] Create PerceptionResult and ScanFrame Pydantic models in client/src/models/perception.py — PerceptionResult with target_visible, horizontal_offset (-1.0 to 1.0), vertical_offset (-1.0 to 1.0), relative_size (0.0 to 1.0), confidence (0.0 to 1.0) with clamping validators; ScanFrame with index (0-7), heading_degrees, jpeg_bytes (min 1000 bytes), captured_at
- [x] T010 [P] Create Mission and MissionStatus Pydantic models in client/src/models/mission.py — MissionType enum (explore, inspect, freeform), MissionStatus enum (idle, scanning, analyzing, approaching, inspecting, complete, aborted), Mission with id (UUID), type, status, target_description, refined_label, approach_step (0-15), max_approach_steps=15, started_at, scan_frames (max 8), best_scan_index
- [x] T011 [P] Create all tool call parameter Pydantic models in client/src/models/tool_calls.py — TakeoffParams, LandParams, HoverParams, MoveDroneParams (direction enum + distance_cm with clamp 20-200), RotateDroneParams (direction enum + degrees with clamp 10-360), SetSpeedParams (speed 10-100), StartExplorationParams (target_description), StartInspectionParams (target_description + optional aspects), ReportPerceptionParams (same fields as PerceptionResult), ReportScanAnalysisParams (best_index: int 0-7, target_visible: bool, refined_label: str)
- [x] T012 [P] Create client-side WebSocket message types in client/src/models/messages.py — Pydantic models for all message types per contracts/backend-websocket.md: AudioInMsg, VideoFrameMsg, ToolResponseMsg (client→backend) and AudioOutMsg, ToolCallMsg (with id, name, args, validated, rejected_reason), TranscriptMsg, SessionStatusMsg, InterruptedMsg, ErrorMsg (backend→client); TranscriptEntry model with speaker enum (user/copilot/system), text, timestamp
- [x] T013 [P] Create backend-side WebSocket message types in backend/src/models/messages.py — Pydantic models for incoming messages (AudioInMsg, VideoFrameMsg, ToolResponseMsg) and outgoing messages (AudioOutMsg, ToolCallMsg, TranscriptMsg, SessionStatusMsg, InterruptedMsg, ErrorMsg) per contracts/backend-websocket.md
- [x] T014 [P] Create Gemini Live API tool declarations in backend/src/models/tools.py — build list of google.genai types.FunctionDeclaration for all 10 tools per contracts/tool-declarations.md: takeoff, land, move_drone, rotate_drone, hover, set_speed, report_perception, report_scan_analysis, start_exploration, start_inspection with full parameter JSON schemas and calibrated descriptions; report_perception description must include size-to-distance calibration anchors from lesson K1 (0.03-0.08 = far 3m+, 0.08-0.15 = medium-far 1.5-3m, 0.15-0.25 = medium 0.8-1.5m, 0.25-0.40 = close <0.8m); add report_scan_analysis tool declaration with params: best_index (int, 0-7, index of scan frame where target is most visible), target_visible (bool, whether target was found in any frame), refined_label (str, precise visual description like "green cardboard box with white label" per lesson B5) — this tool is called by Gemini after analyzing scan frames to return structured results instead of voice

### Core Drone Infrastructure (sequential dependencies)

- [x] T015 [P] Implement MockDrone in client/src/drone/mock_drone.py — simulate djitellopy.Tello interface: connect(), takeoff(), land(), move_forward/back/left/right/up/down(cm), rotate_clockwise/counter_clockwise(deg), set_speed(val), get_battery(), get_height(), get_temperature(), get_flight_time(), streamon(), get_frame_read(); maintain simulated state (battery drains, altitude changes); generate synthetic video frames (solid color with text overlay)
- [x] T016 [P] Implement SafetyGuard in client/src/drone/safety_guard.py — validate_takeoff (battery>=25%, temp<80, not flying), validate_command (battery>=20%, temp<80, is_flying, post-takeoff stabilization elapsed), clamp_move_distance (20-200, down clamped to altitude-min_altitude), clamp_rotation (10-360), check_battery_critical (auto-land at <20%), check_temperature_critical (auto-land at >80), all thresholds from config.py
- [x] T017 Implement CommandExecutor in client/src/drone/command_executor.py — threading.Lock for command serialization (FR-013) since the heartbeat runs in a separate thread and asyncio.Lock is not thread-safe; heartbeat uses acquire(blocking=False) to skip if a command is in progress (lesson C5); CancellationToken class for aborting in-flight commands (FR-014); heartbeat thread sending drone query every 10s to prevent 15s auto-land (FR-005); inter-command delay enforcement (2.0s move, 2.5s rotate, 1.0s/1.5s approach); execute_command method that acquires lock + checks cancellation + enforces delay + calls drone SDK
- [x] T018 Implement DroneController (high-level operations) in client/src/drone/controller.py — wraps CommandExecutor with SafetyGuard validation; methods: takeoff (validate + execute + 4.0s stabilization wait, record takeoff_time), land (graceful with retry), emergency_land (FR-007: land() → raw SDK land → motor stop; check time-since-takeoff — if <5s, wait remaining stabilization before landing per lesson D2, as landing within 5s of takeoff is unreliable), move (validate + clamp + execute), rotate (validate + clamp + execute), hover (send stop), set_speed; maintains DroneState via telemetry polling; accepts real Tello or MockDrone

### Error Handling Infrastructure

- [x] T046 Implement error handling framework in client/src/error_handler.py — define ErrorCategory enum (CONNECTION, COMMAND, AI, SAFETY, HARDWARE, UNKNOWN); implement keyword-based error categorization from exception messages (lesson J1); implement category-specific recovery strategies: SAFETY → always land immediately, HARDWARE → abort mission and land, CONNECTION + in-flight → land, CONNECTION + on-ground → retry connect, AI → retry up to 2 times then skip, COMMAND → log and continue; integrate with DroneController (wrap command execution), mission threads (catch and categorize), and BackendClient (connection errors); log all errors with category, context, and recovery action taken

**Checkpoint**: Foundation ready — all shared models and drone control infrastructure complete. User story implementation can begin.

---

## Phase 3: User Story 1 — Freeform Voice Conversation with Drone (Priority: P1) MVP

**Goal**: User speaks naturally, AI responds verbally in real time describing what the drone camera sees, user issues basic commands (takeoff, land, move, rotate, stop) and the drone executes them immediately.

**Independent Test**: Connect to drone (or mock), start voice session, say "take off," "look left," "what do you see?," "land." Verify AI responds verbally and drone executes each command within 5 seconds.

### Backend Implementation

- [x] T019 [P] [US1] Implement FastAPI application with WebSocket endpoint (/ws) and health check (/healthz) in backend/src/main.py — accept WebSocket connections, parse incoming JSON messages by type, route audio_in and video_frame to Gemini session, route tool_response to Gemini, forward Gemini responses (audio, tool calls, transcripts, interrupts) back to client; include CORS middleware; health check returns {"status": "healthy"}
- [x] T020 [P] [US1] Implement Gemini Live API session manager in backend/src/gemini_session.py — create google.genai.Client, connect via client.aio.live.connect() with LiveConnectConfig including: model from config, system_instruction (copilot persona per FR-019: confident wingman, casual-professional, mission-focused; include approach-phase perception clause: "During active approach to a target, call report_perception on every video frame you receive to report the target's position, size, and visibility"; include scan analysis clause: "After analyzing scan frames, call report_scan_analysis with structured results — do not describe findings in voice only"), all 10 tool declarations from tools.py, voice_config with prebuilt voice name, context_window_compression with SlidingWindow (R5), session_resumption with handle tracking (R5); methods: send_audio(pcm_bytes), send_video(jpeg_bytes), send_tool_response(id, name, result), receive() async iterator; handle GoAway messages for reconnection; handle session resumption on disconnect
- [x] T021 [US1] Implement WebSocket relay logic in backend/src/relay.py — bridge between client WebSocket and Gemini Live session: forward audio_in → session.send_realtime_input(audio=Blob), forward video_frame → session.send_realtime_input(video=Blob), forward tool_response → session.send_tool_response(); receive loop: session.receive() yields messages, route response.data → audio_out message, route response.tool_call → tool_call message with all function_calls, route response.server_content.input_transcription → transcript(speaker=user), route response.server_content.output_transcription → transcript(speaker=copilot), detect response.server_content.interrupted → interrupted message; handle errors with retry and backoff per R5

### Client Audio/Video Implementation

- [x] T022 [P] [US1] Implement microphone audio capture in client/src/audio/capture.py — sounddevice.RawInputStream at 16kHz, mono, int16, 100ms chunks (3200 bytes); callback pushes PCM bytes to asyncio.Queue via loop.call_soon_threadsafe; start/stop methods; context manager support
- [x] T023 [P] [US1] Implement audio playback with barge-in support in client/src/audio/playback.py — sounddevice.RawOutputStream at 24kHz, mono, int16; callback pulls from asyncio.Queue; clear_queue() method for handling interrupts (R6: when interrupted message received, drain queue and stop current playback); start/stop methods; context manager support
- [x] T024 [P] [US1] Implement thread-safe video frame capture with validation in client/src/video/frame_capture.py — run drone.get_frame_read() in background thread; threading.Lock for frame access with copy semantics (FR-011); validate frames: reject if None, reject if dimensions < MIN_FRAME_WIDTH x MIN_FRAME_HEIGHT, reject black frames (mean < BLACK_FRAME_THRESHOLD per LESSONS_LEARNED L1), reject if JPEG bytes < MIN_FRAME_BYTES; apply cv2.cvtColor(frame, cv2.COLOR_RGB2BGR) before JPEG encoding since Tello streams RGB but OpenCV imencode expects BGR (lesson G1); provide get_frame() returning validated frame or None
- [x] T025 [P] [US1] Implement video frame encoding and streaming in client/src/video/frame_streamer.py — get frame from FrameCapture, resize to PERCEPTION_FRAME_WIDTH (768px) maintaining aspect ratio, JPEG encode with cv2.imencode (ensure frame is BGR before encoding — FrameCapture should already convert per T024, but verify color space at this layer too); provide get_perception_frame() returning base64-encoded JPEG for backend; provide get_dashboard_frame() returning 960x720 JPEG for dashboard; rate limiting at config frame_rate_to_gemini (1 FPS) for backend stream

### Client Core Implementation

- [x] T026 [US1] Implement WebSocket client for GCP backend in client/src/backend_client.py — connect to backend_url via websockets library; send_audio(pcm_bytes) builds AudioInMsg and sends JSON; send_video(jpeg_bytes, timestamp) builds VideoFrameMsg and sends JSON; send_tool_response(id, name, response) builds ToolResponseMsg and sends JSON; receive() async iterator that parses incoming JSON, dispatches by type to registered handlers (on_audio_out, on_tool_call, on_transcript, on_interrupted, on_session_status, on_error); auto-reconnect with backoff on connection loss
- [x] T027 [US1] Implement tool call handler in client/src/tool_handler.py — receive ToolCallMsg from backend client, look up tool name in dispatch table, validate args with corresponding Pydantic model from tool_calls.py, on validation failure: build error response with rejected_reason; on success: dispatch to DroneController method (takeoff→controller.takeoff, land→controller.land, move_drone→controller.move, rotate_drone→controller.rotate, hover→controller.hover, set_speed→controller.set_speed); build ToolResponseMsg with success/failure + current drone_state; handle start_exploration and start_inspection by launching mission threads (placeholder for Phase 4/5); handle report_perception by storing result for approach controller (placeholder for Phase 4); handle report_scan_analysis by storing structured scan results (best_index, target_visible, refined_label) for exploration mission (placeholder for Phase 4)
- [x] T028 [US1] Implement client entry point with signal handlers and thread orchestration in client/src/main.py — parse config from .env; initialize drone (real Tello with retry_count=1 or MockDrone based on USE_MOCK_DRONE); connect drone and start video stream; create DroneController, BackendClient, AudioCapture, AudioPlayback, FrameCapture, FrameStreamer, ToolHandler; register signal handlers for SIGTERM/SIGHUP/SIGINT that trigger emergency_land before exit (FR-008); launch mission thread with daemon=False (lesson C2 — daemon thread dying mid-flight means uncontrolled drone; add 15s join timeout with forced emergency landing if thread is stuck); launch concurrent tasks: audio capture→send loop, video frame→send loop (1 FPS), backend receive→dispatch loop, audio playback loop; graceful shutdown on exit
- [x] T029 [US1] Create backend Dockerfile in backend/Dockerfile — Python 3.13-slim base, copy requirements.txt and install, copy src/, expose port 8080, CMD uvicorn with host 0.0.0.0 and port 8080, no --reload in production; include HEALTHCHECK for /healthz

### Early Deployment Validation

- [x] T040 [P] [US1] Create automated deployment script in deploy/scripts/deploy.sh — build Docker image via gcloud builds submit, deploy to Cloud Run with: --timeout=3600, --concurrency=50, --session-affinity, --min-instances=1, --max-instances=3, --cpu=1, --memory=512Mi, --port=8080 per research.md R7; accept PROJECT_ID and GEMINI_API_KEY as parameters. Validate the backend on Cloud Run as soon as US1 MVP works — do not defer to Phase 7.

**Checkpoint**: User Story 1 complete. Voice conversation with drone is fully functional. User can take off, move, rotate, ask what the AI sees, and land — all via voice. This is the MVP. Backend validated on Cloud Run.

---

## Phase 4: User Story 2 — Exploration Mission (Priority: P2)

**Goal**: User says "find the red bag" and the drone autonomously scans the room, identifies the target, navigates toward it with proportional control, and verbally confirms when found.

**Independent Test**: Place a distinct object in a room, say "find the [object]," verify the drone scans, locates, approaches, and verbally confirms the target — with continuous verbal updates during the search.

**Depends on**: Phase 3 (US1) for full voice+drone pipeline

### Implementation

- [x] T045 [US2] Define approach loop perception strategy in client/src/mission/exploration.py and backend system instruction — in the Live API model, perception comes from report_perception tool calls that Gemini pushes, not from client-side API calls. Define the mechanism that ensures Gemini analyzes every video frame during active approach: add a system instruction clause telling Gemini "During active approach, call report_perception on every video frame you receive to report target position"; additionally implement a client-side fallback that sends a text prompt via session.send_client_content (e.g., "Analyze the current frame and report_perception for the target") after each movement command if no report_perception tool call arrives within 3 seconds. Wire the approach controller to consume report_perception results and block until a perception is available (with timeout) before computing the next movement. Without this task, the approach controller receives perception randomly or never.
- [x] T030 [P] [US2] Implement scan pattern and target acquisition in client/src/mission/exploration.py — ScanPattern class: 8-position 360-degree recon scan (45-degree increments), at each position: rotate drone with retry logic (up to 3 attempts per rotation, lesson A2 — Tello says OK but may not move), wait stabilization, capture frame as ScanFrame (JPEG + heading), store in Mission.scan_frames; land drone after scan (F1 pattern from data-model.md); send all scan frames to Gemini via send_client_content for analysis; receive structured results via report_scan_analysis tool call (NOT voice/text parsing — the Live API returns audio, not JSON; Gemini calls report_scan_analysis with best_index, target_visible, and refined_label per lesson B5); if target found: take off, rotate to target heading using best_scan_index, set Mission.refined_label from tool call, transition to approach; if not found: report verbally and return to idle
- [x] T031 [US2] Implement proportional approach controller in client/src/mission/exploration.py — ApproachController class: consume PerceptionResult from report_perception tool calls; apply EMA smoothing (alpha=0.5) to all fields; horizontal alignment: if abs(h_offset) > CENTERING_THRESHOLD and far → rotate (KP_ROTATION * h_offset, skip if < SKIP_ROTATION_THRESHOLD), if close → lateral strafe (KP_LATERAL * h_offset, skip if < SKIP_LATERAL_THRESHOLD); vertical alignment: up/down (KP_VERTICAL * v_offset, skip if < SKIP_VERTICAL_THRESHOLD); forward movement: only if abs(smoothed_h_offset) < CENTERING_THRESHOLD, zone-based max distance (far=40cm, medium=30cm, close=20cm), distance = KP_FORWARD * (COMPLETION_SIZE - relative_size); completion: relative_size >= COMPLETION_SIZE; search recovery: 3 consecutive not-visible → small search sweep; watchdog: abort after APPROACH_WATCHDOG_S (120s); max steps: MAX_APPROACH_STEPS (15)
- [x] T032 [US2] Integrate exploration mission with tool handler in client/src/tool_handler.py — wire start_exploration tool call to launch exploration mission in background thread; wire report_perception tool call to feed PerceptionResult to ApproachController during active approach; wire report_scan_analysis tool call to deliver structured scan results (best_index, target_visible, refined_label) to the exploration mission's scan-analysis phase; broadcast mission status changes to dashboard (via broadcaster if available); update Mission model state transitions per data-model.md; handle user interrupt ("stop") by aborting mission and transitioning to aborted state

**Checkpoint**: Exploration mission works end-to-end. User can ask the drone to find objects and it autonomously searches and approaches them.

---

## Phase 5: User Story 3 — Inspection Mission (Priority: P2)

**Goal**: User says "check that plant for issues" and the drone approaches the target, observes from multiple angles, and delivers a detailed verbal assessment.

**Independent Test**: Position drone near an object, say "inspect that [object]," verify drone captures multiple angles and AI provides relevant verbal assessment.

**Depends on**: Phase 3 (US1) for voice+drone pipeline; may reuse approach controller from Phase 4

### Implementation

- [x] T033 [P] [US3] Implement multi-angle observation in client/src/mission/inspection.py — InspectionMission class: approach target (reuse ApproachController from exploration.py if approach needed), capture frames from 3-4 angles (front, left 45, right 45, optionally above); at each angle: rotate/reposition drone, wait stabilization, capture high-res frame; land drone; send all captured frames to Gemini via send_client_content with inspection prompt including target_description and aspects; AI generates detailed verbal report of findings
- [x] T034 [US3] Implement verbal report generation flow in client/src/mission/inspection.py — after multi-angle capture and landing, construct Gemini prompt with all frames + target_description + aspects; AI responds verbally with structured observations (e.g., "the leaves look healthy, no visible damage"); transition mission status to complete; handle follow-up questions by maintaining inspection context
- [x] T035 [US3] Integrate inspection mission with tool handler in client/src/tool_handler.py — wire start_inspection tool call to launch InspectionMission in background thread; broadcast mission status changes; handle user interrupts; update Mission model state transitions

**Checkpoint**: Inspection mission works end-to-end. Drone approaches objects, captures multiple views, and AI delivers detailed verbal reports.

---

## Phase 6: User Story 4 — Web Dashboard Monitoring (Priority: P3)

**Goal**: Observer opens a web dashboard showing live drone video, scrolling conversation transcript, real-time telemetry (battery, height, temperature), and mission status — all updating in real time via WebSocket.

**Independent Test**: Open dashboard in browser during active session, verify video stream, transcript updates, telemetry values, and mission status all update in real time without page refresh.

**Depends on**: Phase 2 (Foundational) for models; integrates with Phase 3 (US1) main loop

### Implementation

- [x] T036 [P] [US4] Implement dashboard FastAPI server with WebSocket and REST endpoints in client/src/dashboard/server.py — per DASHBOARD_DESIGN.md Section 5: FastAPI app with uvicorn (async); serve static files from client/src/dashboard/static/; REST endpoints: GET / (serves index.html), GET /health (returns connection count, streaming status, demo mode flag), GET /api/demo-info (returns demo mode flag + available recordings with metadata per Section 7); WebSocket endpoint at /ws; ConnectionManager class: maintain active connections set, add on connect, remove on disconnect, broadcast_json() to all clients; on new WebSocket connect: accept, add to set, send cached last status message for late-joiner replay (only status messages cached, not full history — per Section 5 and Pattern #2); background tasks started at server lifespan: frame streaming (~10 FPS polls frame adapter, broadcasts base64 JPEG) and telemetry streaming (1 Hz polls safety guard) — both DISABLED when server is in demo mode; support multiple concurrent browser clients; accept demo_mode flag at startup to toggle behavior
- [x] T037 [P] [US4] Implement DashboardBroadcaster in client/src/dashboard/broadcaster.py — per DASHBOARD_DESIGN.md Section 10 Pattern #1 (single broadcast point): both live and demo modes feed through the same broadcast_json() method; bridge between synchronous drone control threads and async dashboard WebSocket using fire-and-forget via asyncio.run_coroutine_threadsafe() (Pattern #10 — mission thread broadcasts are non-blocking, drone doesn't stall if WebSocket is slow); methods for all message types per Section 3: broadcast_frame(jpeg_bytes), broadcast_telemetry(data), broadcast_status(data), broadcast_perception(data), broadcast_log(level, message), broadcast_ai_activity(data), broadcast_ai_result(data), broadcast_report_data(data); each method builds the envelope {type, data, timestamp} and calls broadcast_json(); recording middleware hook: if a DemoRecorder is attached (see T048), every call to broadcast_json() also records the message with relative timestamp t = now - start_time before sending to clients (Pattern #7 — transparent recording, no changes to mission code); send_status_sync() wrapper for calling from sync mission threads
- [x] T038 [US4] Create full dashboard web interface per DASHBOARD_DESIGN.md Section 2 in client/src/dashboard/static/ — **Technology**: vanilla JavaScript (ES6+ classes), HTML5 Canvas, CSS custom properties, WebSocket, Google Fonts (Instrument Serif, DM Sans, JetBrains Mono); **index.html**: three-tier layout — (A) header bar with logo, app name, drone model, connection status dot with animated glow, (B) phase timeline with 5-step horizontal progress indicator (Recon→Analysis→Acquire→Approach→Inspection) with pending/active/complete visual states and animated connectors, (C) main content split: left=video section with 960x720 HTML5 Canvas (4:3 aspect ratio), placeholder with drone icon, perception overlay (center crosshair, target crosshair, confidence circle, box corners, age indicator — overlay fades after 1.5s per Pattern #9), controls row (Start/Pause/Land/E-Stop/Generate Report), target input field; right=info panels with telemetry (status badge, battery % with progress bar, altitude, temperature), perception panel (model badge, focus object, visible dot, confidence bar, distance bar), AI activity panel (call count badge, Flash/Pro model badges with glow, spinner with operation name and elapsed timer, stats grid), mission log panel (scrollable timestamped color-coded entries with expandable AI result cards with JSON syntax highlighting per Section 2I); **app.js**: WebSocket connection with auto-reconnect every 3s, message handlers for all types per Section 3 (frame→canvas draw, telemetry→panel update, status→phase timeline + status badge, perception→canvas overlay + perception panel, log→mission log append with slide-in animation, ai_activity→spinner/model badges/stats, ai_result→expandable accordion in log, report_data→enable Generate Report button), PDF report generation in new window per Section 8; **style.css**: design system per Section 2 — warm color palette (#f6f4f1 foundation, accent colors with 8% opacity backgrounds), responsive breakpoints (1024px→single column, 640px→mobile compact); **boot animation**: polished startup sequence per Section 2 (splash screen→logo animate to header→panels stagger in→~3.5s total)
- [x] T039 [US4] Integrate dashboard server and broadcaster with main client loop in client/src/main.py — start dashboard FastAPI server on configured port in background thread (uvicorn); create Broadcaster instance; hook broadcaster into: FrameStreamer (broadcast frames at 10 FPS), DroneController telemetry updates (broadcast at 1 Hz), BackendClient transcript handler (broadcast transcript entries), ToolHandler (broadcast tool activity on each tool call/response), Mission status changes (broadcast status updates)

**Checkpoint**: Dashboard fully functional. Observers can monitor the drone session in real time via browser.

---

## Phase 6B: Demo Replay System (Priority: CRITICAL for Judging)

**Purpose**: Enable judges and users to experience pre-recorded drone sessions without hardware. Demo & Presentation is 30% of the judging score, and most judges will NOT have a Tello drone.

**Depends on**: Phase 6 (Dashboard) — uses the same broadcaster, server, and UI infrastructure

### Recording System

- [x] T048 [P] [US4] Implement demo recording system in client/src/dashboard/recorder.py — per DASHBOARD_DESIGN.md Section 6: DemoRecorder class that attaches as transparent middleware on the DashboardBroadcaster (Pattern #7); `start(target, mode)` writes metadata header to output file; during recording, every call to broadcaster's broadcast_json() also writes the message with relative timestamp `t = now - start_time`; `stop()` updates metadata with final duration and message count; output format: directory with session.json (JSONL — line 1 is metadata with `_meta: true`, version, target, mode, duration_sec, recorded_at, message_count; lines 2+ are timestamped messages `{t, type, data}`) and frames/ directory (JPEG files named by timestamp, e.g., `0.1001.jpg` — frame data written to disk instead of embedded in JSON to keep session.json manageable); enable via `--record-demo` CLI flag on client main; no changes needed to mission code, AI client, or frontend — recording is invisible to them; what gets recorded: frame (~10/sec), telemetry (~1/sec), status (per transition), log (per event), ai_activity (per AI call), ai_result (per AI output), perception (per detection)

### Playback Engine

- [x] T049 [US4] Implement demo playback engine in client/src/dashboard/demo_player.py — per DASHBOARD_DESIGN.md Section 7: DemoPlayer class that reads a demo directory (session.json + frames/); **absolute-time scheduling** (Pattern #3): `playback_start = time.monotonic()`, for each message compute `target_time = playback_start + message.t`, wait the delta using an interruptible async event (Pattern #4); for frame messages: load JPEG from frames/ directory and broadcast via broadcaster; for all other messages: broadcast directly; **skip functionality** per Section 7: phase-level skip (recon, analysis, acquire) fast-forwards entire phase — status/log/ai_result messages still broadcast, frame/telemetry dropped; step-level skip (approach, inspection) skips to next step within phase; **timer resync after skip** (Pattern #5): recalculate `playback_start = now - message.t` so subsequent messages play at correct relative pace; **synthetic report data** (Pattern #6): capture last frame, first acquisition frame, and last inspection result during playback, synthesize report_data message at playback end so Generate Report button works for every demo; pause/resume support; clean stop on user "Land" command
- [x] T050 [P] [US4] Implement demo mode server configuration and entry point in client/src/dashboard/demo_main.py — per DASHBOARD_DESIGN.md Section 7: `python -m client.src.dashboard.demo_main` as zero-dependency entry point (no drone, no API key, no hardware); parse available demo recordings from client/demos/; set server to demo_mode=True (disables live frame streaming and telemetry background tasks); register available demos with server for /api/demo-info endpoint (returns `{demo_mode: true, demos: [{id, label, target, duration_sec}]}`); start FastAPI server; wait for user to select a demo and click Start in dashboard; on WebSocket `{type: "command", action: "start", demo_id: "..."}` create new DemoPlayer for selected recording; on "land" or "skip_phase" commands, relay to DemoPlayer; also support single custom recording path as CLI argument: `python -m client.src.dashboard.demo_main path/to/recording/`

### Dashboard Demo Mode UI

- [x] T051 [US4] Add demo mode UI features to dashboard per DASHBOARD_DESIGN.md Sections 2D-2E — in client/src/dashboard/static/app.js and index.html: on page load, fetch /api/demo-info; if demo_mode is true: (1) add violet "DEMO" badge with pulsing white dot to header bar, (2) replace target text input with `<select>` dropdown populated from demo list, label changes to "Select Demo", dropdown disabled during active playback, re-enabled when stopped, (3) show "Skip Phase" button (demo only) that sends skip_phase command — label changes contextually: "Skip Phase" during recon/analysis/acquire, "Next Step" during approach, "Skip Wait" during inspection per Section 2D; when no drone is connected (non-demo live mode), default the landing page to show a message directing users to demo mode with instructions; ensure Start button sends `{type: "command", action: "start", demo_id: selected_value}` including the selected demo ID

### Pre-Recorded Demo Data

- [x] T052 [P] [US4] Create pre-recorded demo data structure and recording guide — create directory structure: `client/demos/exploration_demo/` (session.json + frames/) and `client/demos/inspection_demo/` (session.json + frames/); create `client/demos/README.md` with instructions for recording new demos using `--record-demo` flag; create placeholder session.json files with the metadata schema so the demo selector works even before real recordings exist (demo_mode shows "No recordings available — see README to record demos"); add .gitkeep files in frames/ directories; NOTE: actual demo recordings will be captured during integration testing with a real drone or generated from mock drone sessions — this task creates the structure and tooling, not the content

**Checkpoint**: Demo replay system complete. Users can run `python -m client.src.dashboard.demo_main` to experience pre-recorded missions without any hardware.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Infrastructure-as-code, CI/CD automation, and end-to-end validation (T040 deploy script moved to Phase 3 for early validation)

- [x] T041 [P] Create GCP project setup script in deploy/scripts/setup-gcp.sh — enable required APIs (Cloud Run, Cloud Build, Container Registry), create service account, set IAM roles, configure billing alerts
- [x] T042 [P] Create Cloud Build configuration in deploy/cloudbuild.yaml — build backend Docker image, push to Container Registry, deploy to Cloud Run; trigger on push to main branch
- [x] T043 [P] Create Terraform configuration in deploy/terraform/ — main.tf, variables.tf, outputs.tf for Cloud Run service, IAM bindings, and secret management for GEMINI_API_KEY
- [x] T044 Run quickstart.md validation end-to-end — follow all steps in specs/001-drone-copilot/quickstart.md with USE_MOCK_DRONE=true: install dependencies, configure .env, start backend locally, start client, open dashboard, verify voice interaction works with mock drone
- [x] T053 [P] Create README.md structured for hackathon judges — the README is the first thing judges see and must be optimized for their evaluation criteria (Demo & Presentation = 30%); structure per user specification: (1) **What it does** — one paragraph + embedded GIF or screenshot of dashboard, (2) **Try it without a drone** — prominent section AT THE TOP before any hardware setup, link/instructions to run `python -m client.src.dashboard.demo_main` with pre-recorded demos, emphasize zero hardware required, (3) **Watch the demo** — placeholder for YouTube demo video link (<4 min per hackathon rules), (4) **Architecture** — embedded diagram from docs/architecture.png showing split architecture (local client ↔ Cloud Run backend ↔ Gemini Live API), (5) **Setup & run** — complete spin-up instructions (required by hackathon rules) for both demo mode and live drone mode, (6) **Google Cloud deployment** — proof of GCP hosting (Cloud Run URL, deployment instructions, screenshot of running service), (7) **Built with** — tech stack and specific Gemini features used (Live API, tool calls, multimodal audio+video, context window compression, session resumption); include badges for Python, Gemini, Cloud Run; do NOT bury the demo experience under developer setup

---

## Dependencies & Execution Order

### Phase Dependencies

- **Validation (Phase 0)**: No dependencies — MUST complete before Phase 1. BLOCKING GATE.
- **Setup (Phase 1)**: Depends on Phase 0 passing
- **Foundational (Phase 2)**: Depends on Phase 1 completion — BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Phase 2 completion — delivers the MVP
- **US2 (Phase 4)**: Depends on Phase 3 (US1) — uses full voice+drone pipeline
- **US3 (Phase 5)**: Depends on Phase 3 (US1) — uses full voice+drone pipeline; may reuse approach controller from Phase 4
- **US4 (Phase 6)**: Depends on Phase 2 (models); integration (T039) depends on Phase 3 (US1 main.py)
- **Demo Replay (Phase 6B)**: Depends on Phase 6 (Dashboard) — uses broadcaster, server, and UI infrastructure
- **Polish (Phase 7)**: Depends on Phase 3 (US1) minimum; T053 README best after Phase 6B (demo system) is complete

### Full Dependency Graph

```
Phase 0 (Validation — T047 GATE)
    │
    v
Phase 1 (Setup)
    │
    v
Phase 2 (Foundational) ──── BLOCKS ALL ────┐
    │                                        │
    v                                        v
Phase 3 (US1: Voice+Drone + T040 deploy) ◄──── Phase 6 (US4: Dashboard — T036-T038 can start)
    │           │                                     │
    │           └───── T039 (Dashboard integration) ◄─── T036-T038
    v                                                 │
Phase 4 (US2: Exploration — T045 then T030/T031/T032) │
    │                                                 v
    v (optional — reuse approach controller)    Phase 6B (Demo Replay — T048-T052)
Phase 5 (US3: Inspection)                            │
    │                                                 v
    └──────────────────┬──────────────────────────────┘
                       v
                 Phase 7 (Polish — T041-T044, T053 README)
```

### Within Each User Story

- Backend tasks (T019-T020) can run in parallel with client audio/video tasks (T022-T025)
- Relay (T021) depends on backend app (T019) and session manager (T020)
- Backend client (T026) can start after backend is running
- Tool handler (T027) depends on DroneController (T018) and BackendClient (T026)
- Client main.py (T028) depends on all other US1 tasks
- T046 (Error handler) depends on T018 (DroneController) — integrates with command execution and mission threads
- T045 (Approach loop strategy) should be completed before T031 (ApproachController) — defines how perception flows
- T030 (Scan) and T031 (Approach) can run in parallel after T045

### Within Demo Replay System

- T048 (Recorder) and T050 (Demo mode server) can run in parallel — different files
- T049 (DemoPlayer) depends on T050 (needs server infrastructure for demo mode)
- T051 (Demo UI) depends on T038 (base dashboard) and T050 (demo mode server)
- T052 (Demo data structure) can run in parallel with anything in Phase 6B

### Parallel Opportunities

**Phase 2 — 10 model tasks + error handler can run simultaneously:**
```
T006 (client config+logging) | T007 (backend config) | T008 (DroneState) | T009 (Perception)
T010 (Mission)       | T011 (ToolCalls)      | T012 (Client msgs) | T013 (Backend msgs)
T014 (Tool decls)    | T015 (MockDrone)      | T016 (SafetyGuard)
→ Then: T017 (CommandExecutor) → T018 (DroneController) → T046 (Error handler)
```

**Phase 3 — Backend and client audio/video in parallel:**
```
T019 (FastAPI) | T020 (Gemini session) | T022 (mic capture) | T023 (playback)
T024 (frame capture) | T025 (frame stream)
→ Then: T021 (relay) | T026 (WS client) → T027 (tool handler) → T028 (main.py)
→ Also: T029 (Dockerfile) and T040 (deploy script) can run anytime
```

**Phase 6 — Server, broadcaster, and recorder in parallel:**
```
T036 (dashboard server) | T037 (broadcaster) | T048 (recorder — different file)
→ Then: T038 (web interface) → T039 (integration)
```

**Phase 6B — Demo replay system:**
```
T050 (demo mode server) | T052 (demo data structure)
→ Then: T049 (DemoPlayer) | T051 (demo UI enhancements)
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. **Phase 0**: Validate Gemini Live API video+audio — MUST pass before anything else
2. Complete Phase 1: Setup
3. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
4. Complete Phase 3: User Story 1
5. **STOP and VALIDATE**: Test voice conversation with mock drone
6. Deploy backend to Cloud Run, test with real drone
7. This alone is a compelling hackathon demo

### Incremental Delivery

1. Phase 0 Validation → Technical risk eliminated
2. Setup + Foundational → Foundation ready
3. Add US1 (Voice Conversation) → **MVP demo-ready**
4. Add US2 (Exploration) → Signature "find the object" demo
5. Add US3 (Inspection) → Detailed observation capability
6. Add US4 (Dashboard) → Real-time monitoring for observers
7. Add Demo Replay (Phase 6B) → **Judges can experience the project without hardware**
8. Add README + Polish → Submission-ready

### Hackathon Priority

Given deadline 2026-03-16:
- **Day 1**: Phase 0 (validate Live API video+audio) — if this fails, pivot immediately
- **Week 1**: Phase 1 + Phase 2 + Phase 3 (US1) → working voice-controlled drone
- **Week 2**: Phase 4 (US2 exploration) + Phase 6 (Dashboard) → hero demo feature + monitoring
- **Week 3**: Phase 5 (US3) + Phase 6B (Demo Replay) + Phase 7 (README + polish)

### Demo-First Mindset

The dashboard and demo replay system are NOT afterthoughts. Demo & Presentation is **30% of the judging score** and most judges will not have a Tello drone. The README must lead with "Try Without a Drone" and the dashboard must default to showing the demo selector when no drone is connected. Pre-recorded demos are the primary way judges will experience this project.

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- [Story] label maps task to specific user story for traceability
- All Pydantic models use strict validation with field constraints from data-model.md
- All drone thresholds sourced from LESSONS_LEARNED.md via config.py
- Audio: 16kHz PCM input (mic), 24kHz PCM output (speakers) — no resampling needed
- Video: 768px JPEG for Gemini, 960x720 for dashboard
- Critical risk: 2-min audio+video session limit mitigated by context window compression + session resumption (research.md R5)
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently

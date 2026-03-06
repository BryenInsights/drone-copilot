<!--
  Sync Impact Report
  ===================
  Version change: 1.1.0 → 1.1.1
  Bump rationale: PATCH — added DASHBOARD_DESIGN.md reference to
  section II (Lessons Learned as Ground Truth).

  Modified principles: II expanded (new bullet for DASHBOARD_DESIGN.md)

  Added sections: None
  Expanded sections:
    - II. Lessons Learned as Ground Truth: added DASHBOARD_DESIGN.md
      consultation guidance for dashboard, WebSocket, and replay work.
  Removed sections: None

  Templates requiring updates:
    - .specify/templates/plan-template.md ✅ no update needed
    - .specify/templates/spec-template.md ✅ no update needed
    - .specify/templates/tasks-template.md ✅ no update needed
    - .specify/templates/checklist-template.md ✅ no update needed
    - .specify/templates/agent-file-template.md ✅ no update needed

  Follow-up TODOs: None
-->

# Drone Copilot Constitution

## Core Principles

### I. Safety First

This project controls physical hardware. A bug means a crashed drone.

- Every flight operation MUST include error handling with emergency
  landing as the fallback.
- Emergency landing MUST be multi-layered: `land()` with retry,
  raw SDK `tello.land()`, then `tello.emergency()` motor stop.
- Signal handlers (SIGTERM, SIGHUP) MUST trigger emergency landing.
- Post-takeoff stabilization delays MUST be enforced before sending
  movement commands (minimum 4.0 seconds).
- A heartbeat thread MUST keep the drone alive during long-running
  operations (10-second interval via `query_battery()`).
- Threading rules: mission thread MUST be non-daemon; command lock
  MUST serialize all drone commands; cancellation tokens MUST allow
  immediate abort of in-flight commands.
- Descent commands MUST be clamped to safe altitude, never rejected
  outright.

**Rationale**: The Tello auto-lands after 15 seconds of silence,
ignores commands below minimum thresholds, and reports success for
commands it did not execute. Defensive programming is not optional.

### II. Lessons Learned as Ground Truth

`LESSONS_LEARNED.md` in the project root contains tuned thresholds,
timing values, failure modes, and workarounds from real drone flight
testing.

- Before making ANY decision involving the Tello drone, Gemini API,
  video streaming, or macOS compatibility, the developer MUST read
  and follow `LESSONS_LEARNED.md`.
- Thresholds and timing values in that document are empirically
  derived and MUST NOT be changed without flight-test validation.
- When `LESSONS_LEARNED.md` contradicts external documentation or
  assumptions, `LESSONS_LEARNED.md` wins.
- `DASHBOARD_DESIGN.md` contains the architectural design for the
  web dashboard and demo replay system. Consult it when implementing
  dashboard, WebSocket protocol, or replay functionality.

**Rationale**: Every number in that file was earned through real-world
flight testing. Theoretical values fail in practice.

### III. Library Source of Truth

- For `djitellopy`: read the actual source code in the installed
  package (`site-packages/djitellopy`) as ground truth for available
  methods, parameters, and behavior. Do not rely on third-party
  tutorials or outdated documentation.
- For Google GenAI SDK and Gemini Live API: use the Context7 MCP
  plugin to check official documentation. Always verify API
  signatures, parameters, and capabilities against the latest docs
  before implementing. Use Context7 sparingly — only when verifying
  a specific API signature, parameter, or capability you are unsure
  about. Do not call it for general knowledge or information already
  in `LESSONS_LEARNED.md`.
- API timeout units MUST be verified before use (Google GenAI SDK
  `HttpOptions.timeout` expects milliseconds, not seconds).

**Rationale**: Library APIs change between versions. Assumptions
from documentation or memory lead to silent failures (e.g., passing
`timeout=60` instead of `timeout=60000`).

### IV. AI is Perception Only

Gemini reports what it sees. Deterministic Python code converts
perception into drone commands.

- The LLM MUST NEVER generate drone commands directly.
- All Gemini API calls MUST use structured output
  (`response_schema=PydanticModel`) so the AI-to-action pipeline
  is deterministic and parseable.
- A proportional controller with tuned gains converts perception
  offsets into movement commands.
- Perception values MUST be EMA-smoothed (alpha=0.5) before use in
  control decisions to filter Gemini noise.

**Rationale**: LLMs are nondeterministic. Asking them for motor
commands directly produces unpredictable, untestable behavior.
Structured output with deterministic control is tunable, testable,
and safe.

### V. Modularity

Flight control, video streaming, AI interaction, and web UI MUST
be independent modules testable in isolation.

- Each module MUST have clear interfaces and no circular
  dependencies.
- A mock drone implementation MUST exist for testing without
  hardware.
- Sync-to-async bridges MUST be used when synchronous drone threads
  communicate with async web infrastructure.
- On macOS, the main thread MUST own the OpenCV display loop;
  mission logic MUST run in a background thread.

**Rationale**: Independent modules enable testing without a physical
drone, parallel development, and safe refactoring of individual
subsystems.

### VI. Code Quality

- Type hints MUST be used on all function signatures and return
  types.
- Pydantic models MUST be used for all structured data (API
  responses, configuration, perception results).
- Comprehensive logging MUST be present for flight operations,
  API calls, and error recovery paths.
- Pydantic `Field(description=...)` strings serve as both
  documentation and Gemini prompt instructions — they MUST be
  detailed and precise.

**Rationale**: In a robotics context, type errors and malformed data
cause crashes. Pydantic schemas enforce contracts between AI output
and control logic. Logging is the only debugger available during
autonomous flight.

## Hackathon Constraints

The project is built for the Google Cloud Gemini Live Agent
Challenge (Live Agents category). The following constraints are
externally imposed and non-negotiable:

- **Category**: Live Agents — real-time interaction with
  audio/vision.
- MUST use Gemini Live API or ADK (Agent Development Kit).
- MUST use Google GenAI SDK or ADK.
- MUST use at least one Google Cloud service (backend on GCP).
- MUST be a completely new codebase — no code reuse from prior
  projects.
- **Submission deadline**: 2026-03-16.
- Demo video MUST be under 4 minutes showing real software working
  (no mockups).
- Submission MUST include an architecture diagram, public repo with
  README spin-up instructions, and GCP deployment proof (screen
  recording or code link).
- MUST abide by Google Cloud Acceptable Use Policy.

## Judging Criteria

Development decisions MUST optimize for these scoring weights:

### Innovation & Multimodal UX (40%)

- Break the "text box" paradigm. Interaction MUST feel natural and
  immersive.
- Handle interruptions (barge-in) naturally.
- The copilot MUST have a distinct persona/voice.
- Interaction MUST feel "live" and context-aware rather than
  turn-based.

### Technical Implementation & Agent Architecture (30%)

- Effective use of GenAI SDK/ADK.
- Robust GCP hosting.
- Sound agent logic with graceful error/timeout handling.
- Grounding to avoid hallucinations.

### Demo & Presentation (30%)

- Clear problem/solution story.
- Clear architecture diagram.
- Visual proof of cloud deployment.
- Show actual software working.

## Bonus Points

All of the following SHOULD be completed to maximize score:

- **Content publication (+0.6 pts)**: Publish a blog post or video
  about building the project with `#GeminiLiveAgentChallenge`.
- **IaC automation (+0.2 pts)**: Automate cloud deployment with
  Infrastructure-as-Code scripts committed to the repo.
- **GDG membership (+0.2 pts)**: Join a Google Developer Group and
  link profile in submission.

## Technology Stack

The following technology choices are fixed for this project:

| Component | Choice | Notes |
|-----------|--------|-------|
| Language | Python 3.13 | |
| Drone SDK | djitellopy | `retry_count=1` on init |
| Computer Vision | opencv-python-headless | NOT opencv-python |
| AI SDK | google-genai | Timeout in milliseconds |
| Web Framework | FastAPI | Async with WebSocket |
| Data Validation | Pydantic | Structured output schemas |
| Containerization | Docker | |
| Cloud Deployment | Google Cloud Run | |

- `opencv-python-headless` MUST be used instead of `opencv-python`
  to avoid GUI dependency conflicts in containerized environments.
- `djitellopy` MUST be initialized with `retry_count=1` to prevent
  non-idempotent command retries (double-takeoff = crash).

## Governance

- This constitution supersedes all other development practices and
  conventions for this project.
- All code changes MUST be verified against these principles before
  merging.
- Amendments to this constitution MUST include:
  1. A description of the change and its rationale.
  2. An updated version number following semantic versioning:
     - MAJOR: Principle removal, redefinition, or backward-incompatible
       governance change.
     - MINOR: New principle or section added, or materially expanded
       guidance.
     - PATCH: Clarifications, wording, typo fixes, non-semantic
       refinements.
  3. Updated `Last Amended` date.
- Safety-related principles (I, II) MUST NOT be weakened without
  flight-test evidence justifying the change.
- Use `LESSONS_LEARNED.md` as the runtime development guidance
  document for domain-specific implementation decisions.

**Version**: 1.1.1 | **Ratified**: 2026-02-24 | **Last Amended**: 2026-02-24

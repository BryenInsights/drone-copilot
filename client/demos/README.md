# Demo Recordings

Pre-recorded drone mission sessions for the demo replay dashboard.

## Recording a New Demo

1. Start the backend and client in live mode (real or mock drone)
2. Add the `--record-demo` flag to the client:
   ```bash
   python -m client.src.main --record-demo
   ```
3. Run a mission (e.g., "find the red bag")
4. The recording is saved to a timestamped directory under `client/demos/`

## Recording Format

Each recording is a directory containing:

```
<recording_name>/
  session.json    # JSONL file — line 1 is metadata, lines 2+ are timestamped messages
  frames/         # JPEG files named by relative timestamp (e.g., 0.1001.jpg)
```

### Metadata (line 1 of session.json)
```json
{
  "_meta": true,
  "version": 1,
  "target": "red bag",
  "mode": "exploration",
  "duration_sec": 95.3,
  "recorded_at": 1740000000.0,
  "message_count": 1234
}
```

### Message entries (lines 2+ of session.json)
```json
{"t": 0.1001, "type": "frame", "data": "0.1001.jpg"}
{"t": 0.5, "type": "telemetry", "data": {"battery": 95, "altitude": 0, ...}}
{"t": 1.2, "type": "status", "data": {"state": "TAKEOFF", "phase": "recon", ...}}
{"t": 1.5, "type": "log", "data": {"level": "INFO", "message": "Taking off..."}}
```

Frame messages reference JPEG files in the `frames/` directory instead of
embedding base64 data directly in the JSON, keeping session.json manageable.

## Playing Back a Demo

```bash
# Play all demos in client/demos/
python -m client.src.dashboard.demo_main

# Play a specific recording
python -m client.src.dashboard.demo_main client/demos/exploration_demo/
```

Then open http://localhost:8081 in your browser.

## Placeholder Demos

The `exploration_demo/` and `inspection_demo/` directories contain placeholder
metadata files. Record real sessions to populate them with actual data.

# Codelab: Multi-Agent Real-Time Monitoring System

Source: https://codelabs.developers.google.com/way-back-home-level-4/instructions#0

## Overview

Build a live bidirectional multi-agent system with specialized agents that communicate in real-time, featuring streaming tools and Agent-to-Agent (A2A) protocol.

## Architecture: Three Components

### 1. Architect Agent (Specialist)

- Connects to Redis database storing schematics
- Implements `lookup_schematic_tool` for part retrieval
- Operates as A2A server broadcasting capabilities via Agent Card

### 2. Dispatch Agent (Primary Hub)

- Manages bidirectional streaming with live voice/video input
- Delegates queries to Architect via Agent-as-a-Tool pattern
- Hosts `monitor_for_hazard` streaming tool for continuous visual analysis

### 3. Frontend Interface

- React-based UI capturing screen/microphone streams
- Sends multimodal data via WebSocket
- Displays real-time agent responses

## Key Patterns

### Streaming Tools

Async generators that `yield` intermediate results continuously:

```python
async def monitor_for_hazard(input_stream: LiveRequestQueue):
    """Monitor video frames for glowing parts"""
    while True:
        if last_valid_req is not None:
            response = await client.aio.models.generate_content(...)
            if hazard_detected:
                yield f"Hazard detected place {part_name} to the {color} bin"
        await asyncio.sleep(5)
```

### Agent-to-Agent Protocol (A2A)

Standardized agent discovery and communication. The Architect broadcasts a JSON Agent Card.

```python
# Architect as A2A server
# server.py uses to_a2a() middleware at port 8081

# Dispatch integrates Architect as remote agent
architect_agent = RemoteA2aAgent(
    name="execute_architect",
    agent_card=f"{ARCHITECT_URL}/.well-known/agent.json",
    httpx_client=insecure_client
)
```

### Agent-as-a-Tool

The Dispatch agent keeps control while delegating to the Architect — receiving data without handing off conversation.

## Database Setup

```bash
docker run -d --name ozymandias-vault -p 6379:6379 redis:8.6-rc1-alpine
```

## Tool Implementation

```python
def lookup_schematic_tool(drive_name: str) -> list[str]:
    """Returns ordered parts list from Redis"""
    clean_name = drive_name.replace("TARGET:", "").strip()
    result = r.lrange(clean_name, 0, -1)
    return result if result else ["ERROR: Drive ID not found."]
```

## Proactive Audio

```python
run_config = RunConfig(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=[...],
    proactivity=types.ProactivityConfig(proactive_audio=True)
)
```

## Key Insights

1. **Agent-as-a-Tool** keeps primary agent in charge
2. **Streaming tools** enable proactive monitoring without blocking conversation
3. **Bidirectional model** mirrors real phone conversations with natural interruption
4. **A2A protocol** standardizes multi-agent discovery and communication

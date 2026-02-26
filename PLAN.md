# Fergusson Project Plan: Omnipotent Personal Assistant

## Project Overview
A centralized message broker (Gateway) with ingress from multiple channels (Discord, CLI, Cron). Messages are processed by a core Pydantic-AI agent that can delegate tasks to specialized sub-agents based on dynamic "skills" stored in the workspace.

## Core Technologies
- **Package Manager**: `uv`
- **LLM/Agent Framework**: `pydantic-ai`
- **Data Validation**: `pydantic`
- **Broker**: Redis (via `compose.yaml`)
- **Persistence**: SQLite with SQLAlchemy ORM
- **Channels**: Raw WebSocket/REST for Discord (adapted from HKUDS/nanobot)

## Architecture

### 1. Ingress/Egress & Broker
- **Redis Broker**: Acts as the central nervous system. Channels push `InboundMessage` to Redis; the agent consumes them, processes, and pushes `OutboundMessage` back.
- **Discord Channel**: Detached async process using WebSockets for events and HTTP for sending. Handles attachments and rate-limiting.
- **CLI/Cron**: Simple publishers to the Redis queue.

### 2. Agent System
- **Core Agent**: The "Omnipotent" router. Has access to all tools and is responsible for intent recognition.
- **Sub-Agents (Skills)**:
    - Located in `workspace/skills/` (Codex Standard: `SKILL.md` + `openai.yaml`).
    - Dynamically instantiated. `SKILL.md` acts as the system prompt.
- **A2A Delegation**: Core agent uses a `delegate_to_expert` tool to call sub-agents in-process, passing context and returning the expert's findings.

### 3. Tools (`src/tools/`)
- **Bash Tool**: Executes shell commands. Hazardous commands require explicit user confirmation via a guardrail mechanism.
- **Filesystem Tool**: Standard I/O operations.
- **Custom Tools**: Defined according to Pydantic-AI specification.

### 4. Folder Structure
```text
fergusson/
├── compose.yaml                # Redis
├── PLAN.md                     # This document
├── main.py                     # Entrypoint
├── src/
│   ├── agent/                  # Core & Skill Logic
│   ├── broker/                 # Redis Pub/Sub
│   ├── channels/               # Discord, CLI, etc.
│   ├── db/                     # SQLAlchemy Models
│   └── tools/                  # Bash, FS, etc.
└── workspace/
    ├── config/                 # Pydantic Settings
    ├── db/                     # state.db
    ├── media/                  # Attachments
    └── skills/                 # Codex Skills
```

## Implementation Phases

### Phase 1: Foundation
- [x] Initialize `uv` project.
- [x] Create folder structure.
- [x] Configure `compose.yaml` for Redis.
- [x] Implement SQLAlchemy models (`User`, `Conversation`, `Message`).

### Phase 2: Broker & Channels
- [x] Implement Redis `MessageBus`.
- [x] Port/Adapt Discord channel logic.
- [x] Create a mock CLI publisher for testing.

### Phase 3: Core Agent & Tools
- [ ] Implement Core Pydantic-AI Agent.
- [ ] Build Bash and FS tools with permission guardrails.
- [ ] Implement Skill discovery and dynamic Sub-Agent factory.

### Phase 4: A2A & Assembly
- [ ] Implement A2A delegation logic.
- [ ] Create the main execution loop in `main.py`.
- [ ] Final end-to-end testing (Discord -> Core -> Sub-agent -> Discord).

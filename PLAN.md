# Fergusson Project Plan: Omnipotent Personal Assistant

## Project Overview
A centralized message broker (Gateway) with ingress from multiple channels (Discord, CLI, Cron). Messages are processed by a core Pydantic-AI agent that can apply dynamic "skills" stored in the workspace alongside its shared toolset.

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
- **Skills**:
    - Located in `workspace/skills/` (Codex Standard: `SKILL.md` + `openai.yaml`).
    - Loaded into agent context. `SKILL.md` provides reusable instructions.
- **Skill Usage**: The agent applies matching skill instructions directly instead of instantiating a separate expert per skill.

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
- [x] Implement Core Pydantic-AI Agent.
- [x] Build Bash and FS tools with permission guardrails.
- [x] Implement Skill discovery and prompt-based skill loading.

### Phase 4: Agent Assembly
- [x] Implement shared skill loading logic.
- [x] Create the main execution loop in `main.py`.
- [x] Final end-to-end testing (Discord -> Core -> skill/tool usage -> Discord).

### Phase 5: Advanced Memory & Tooling
- [ ] Implement Graph-based Memory using Neo4j.
- [x] Create a local scratchpad (MEMORY.md) for transient context.
- [x] Upgrade the CLI tool (richer TUI/UX).
- [ ] Implement tool-call visibility (notifying the user about tool executions).
- [x] Expand tools to include web content fetching (httpx + markitdown).
- [ ] Expand tools to include web search.
- [x] Co-manage AGENTS.md (dynamic creation and updating of skills).
- [x] Fix relational-memory data quality in Neo4j by adding exact deduplication, prompt-led extractor rules with explicit do/don't examples, similarity lookup before extraction writes, and `replace_existing` handling for corrections.
- [ ] Fix duplicate writes into `MEMORY.md` by adding deduplication and/or stricter write rules.

### Phase 6: Scheduled & Background Tasks
- [x] Implement periodic background tasks via ROUTINE.md.
- [x] Create a Cron-like scheduler for executing tasks at exact times.

### Phase 7: MCP Server Runtime Integration
- [ ] Wire `workspace/config/config.json` `mcp_servers` into the core agent runtime so configured MCP tools are actually available.
- [ ] Implement MCP server factory logic for stdio (`command` + `args`) and HTTP (`url`) transports with explicit validation.
- [ ] Add deterministic transport selection support (`sse` vs `streamable_http`) while keeping backward compatibility for existing configs.
- [ ] Attach MCP servers/toolsets to `CoreAgent` with collision-safe tool prefixes based on server name.
- [ ] Add lifecycle management: open/connect on startup path and close MCP servers on shutdown.
- [ ] Add tests for MCP config parsing, server creation, agent wiring, and shutdown behavior.
- [ ] Update `AGENTS.md`/architecture docs to reflect MCP runtime behavior and config expectations.

### Phase 8: Public Repository README & Workspace Examples
- [ ] Refactor `README.md` for public onboarding so a new user can clone, configure, and run Fergusson without prior project context.
- [ ] Add clear prerequisites and environment setup instructions (`uv`, Redis, Python version, API keys, optional Neo4j).
- [ ] Add quick-start flow (install, configure `.env`, start services, run CLI/Discord workers, smoke test).
- [ ] Document configuration model clearly (`SMART_MODEL`, `FAST_MODEL`, `workspace/config/config.json`, channels, MCP servers).
- [ ] Add security/privacy guidance for secrets, permissions, and safe defaults for first run.
- [ ] Provide `workspace` examples suitable for new users (example `config.json`, starter `PERSONALITY.md`, starter `MEMORY.md`, sample `ROUTINE.md`, and at least one example skill).
- [ ] Document how to customize/replace workspace examples for personal use and how to keep sensitive data out of git.
- [ ] Add troubleshooting section for common setup failures (Redis connection, model auth, Discord token issues, MCP server startup issues).

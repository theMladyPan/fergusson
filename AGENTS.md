# AGENTS.md — Repository Operating Guide

This document is binding for the entire repository (`/workspace/fergusson`).

## 1) Project Goal (brief)
Fergusson is a modular AI assistant with an event-driven architecture:
- **channels** (CLI, Discord, future inputs) receive messages,
- the **broker** (Redis) distributes them,
- the **core agent** applies native tools and reusable skills directly,
- **memory** is layered: one shared SQLite thread for recent human conversation (`cli`/`discord`), one dedicated SQLite thread for cron turns, optional Neo4j graph memory for durable structured facts/preferences/entities, and `MEMORY.md` for a tiny set of human-readable anchor identifiers. Prompt guidance uses tiered memory placement: key IDs and similar anchor objects in `MEMORY.md`, richer structured detail in graph memory. Outbound delivery remains channel-specific.
  Graph-memory creation is explicit via core-agent memory tools; there is no separate post-turn extractor agent.

## 2) Architecture by Directory
- `src/agent/` — agent core, orchestration, SQLite short-term memory with separate user and cron threads, Neo4j graph-memory capability, skill loading, archiver, and an auto-routing first stage. **Auto-routing**: a cheap `ROUTER_MODEL` (default `google-gla:gemini-3.5-flash-lite`) classifies each inbound turn as `answer` (reply directly, no tools beyond read-only `read_file_content` + Exa `web_search`) or `escalate` (run the full core agent on `SMART_MODEL`, default `google-gla:gemini-3.6-flash`, with all tools). The router is fail-fast: any tool error/timeout/invalid output collapses to `escalate`. Routing is gated by `Settings.router.enabled` and tunable via `Settings.router` (tool_timeout, retries=1, request_limit, history_window). Web search is powered by the Exa API (`src/tools/exa.py`), configured via `EXA_API_KEY` and `Settings.exa` (`ExaConfig`): the core agent uses `search_type` (default `auto`) and the router uses `router_search_type` (default `fast`) via one shared `_exa_search` helper; both expose the tool as `web_search` to the model. Graph memory uses `neo4j-agent-memory` as a thin wrapper for long-term facts/preferences/entities/relations with a small tool surface: `search_memory`, `store_fact`, `store_preference`, `store_entity`, and `store_relation`. Fact, preference, and relation writes use exact checks, semantic candidate search, and a fast-model tie-breaker before inserting; entity dedup remains library-backed. User/cron continuity stays in their respective SQLite threads; `MEMORY.md` is reserved for sparse anchor identifiers such as channel IDs and emails. Skill loading now returns one requested skill at a time; prerequisites are metadata hints that the agent must load explicitly. Shared agent dependency types live in `src/agent/deps.py` (`AgentDeps`) to avoid a circular import between `core.py` and `router.py`.
- `src/broker/` — message bus and message schemas between channels and runtime.
- `src/channels/` — integration inputs/outputs (e.g., Discord, CLI adapters) that keep transport-specific `chat_id`s for delivery.
- `src/config.py` — environment-backed runtime settings; model selection uses `SMART_MODEL` / `FAST_MODEL` / `ROUTER_MODEL` as PydanticAI `provider:model` strings, the Google Workspace summary model uses `SUMMARY_MODEL` (default `openrouter:openai/gpt-oss-20b:nitro`) with `SUMMARY_REASONING_EFFORT` (default `medium`), Neo4j uses `NEO4J_*` env vars, Exa web search uses `EXA_*` env vars (`Settings.exa`), Cartesia TTS uses `CARTESIA_*` env vars (`Settings.cartesia`), Google Chirp 3 STT uses `STT_*` env vars (`Settings.stt`), including a dedicated `STT_CREDENTIALS_FILE` service-account key that is loaded explicitly instead of process-wide ADC, and memory settings are grouped under `Settings.memory` (`MemoryConfig`) with nested `Settings.memory.embedding` (`EmbeddingConfig`). Router tunables are grouped under `Settings.router` (`RouterConfig`). `gws` tool tunables (binary, per-call timeout, export char limit) are grouped under `Settings.gws` (`GwsConfig`). Memory envs are resolved directly by nested settings classes (for example `MEMORY_EMBEDDING_PROVIDER`). `workspace/config/config.json` remains for non-model app config.
- `src/tools/` — tools invoked by the agent (bash, filesystem, web fetch via `get_content_from_url`, web search via Exa `web_search` in `src/tools/exa.py`, native speech tools in `src/tools/audio.py`: `transcribe_audio` wraps Chirp 3 STT and `synthesize_speech` wraps Cartesia TTS so the agent never shells out to Whisper/ffmpeg or ad-hoc Python for audio work, and native Google Workspace tools in `src/tools/gws.py`). The gws tools wrap the `gws` CLI with a sanitized environment (STT ADC stripped) and a flash-lite summary model (`settings.summary_model`, default `openrouter:openai/gpt-oss-20b:nitro` with medium reasoning): `list_inbox_emails`, `get_contact`, `list_upcoming_events`, `search_drive_docs` are read-only and registered on BOTH the router and the Core Agent; `create_calendar_event` (with optional attendees + Google Meet) is a write and stays on the Core Agent. Auth failures fail fast via `ModelRetry` pointing to the `gws-debug` skill. The router escalates all speech tasks to the Core Agent, and generated speech can be delivered by passing its returned path to `send_message_to_channel(media_paths=[...])`.
- `src/services/` — external speech services invoked from the run loop (`src/runners.py`): `cartesia.py` (Text-to-Speech via the `cartesia` SDK's `AsyncCartesia`, mp3 output) and `chirp3.py` (Speech-to-Text via `google-cloud-speech` v2, `chirp_3` model, online inline `recognize`, `language_codes=["auto"]`; expands `~` in `STT_CREDENTIALS_FILE` and passes explicitly loaded service-account credentials to `SpeechAsyncClient`, preventing generic subprocesses such as `gws` from inheriting the STT identity). Both replace the former ElevenLabs single-service path and degrade to a no-op (`None`) when unconfigured or on error, so the agent loop keeps working without voice. TTS runs only for voice-in turns; STT runs only for audio media attachments. When the pre-pipeline STT returns no transcript, `runners.py` replaces the bare `[attachment: <path>]` marker with a clear 'transcription unavailable' note so the agent tells the user instead of shelling out to bash; the raw audio path is never passed to the model. Agent startup logs STT/TTS config status once (`AgentManager._log_voice_config_status`) so missing config is visible in `journalctl` without a test voice message.
- `src/db/` — DB models and session layer for state persistence.
- `src/prompt/` — Jinja templates for system prompts (`core.md`, `archiver.md`). `core.md` enforces a mandatory **fail-fast** execution policy: an approach gets at most 2 attempts (first try + one analyzed retry), then the agent must STOP and ask the user a very concise question (1–2 sentences, user language) offering short options (keep trying / different approach / skip). Guess-and-check loops (repeated env probes, near-identical tool calls) are forbidden. The request-limit recovery agent (`src/agent/core.py`) mirrors this by asking whether to keep trying, switch approach, or drop the task.
  Prompt policy for memory is decision-oriented rather than hard imperative: the agent can choose whether to keep concise anchors in `MEMORY.md`, store detail in graph memory, and condense/relocate over-detailed `MEMORY.md` content into graph memory.
  Core communication policy should favor natural conversational phrasing by default (including Slovak when user speaks Slovak), avoid administrative/report-style confirmations for routine chat, and keep memory-save acknowledgments implicit unless explicit confirmation is needed.
  `core.md` should remain user-agnostic operational policy; `workspace/PERSONALITY.md` is for subjective user personalization (name/style/channel intent), while concrete routing identifiers like channel IDs belong in `MEMORY.md`.
- `workspace/skills/` — dynamic skills following the `SKILL.md` standard. `gws-debug` owns Google Workspace CLI authentication, scope, cache, and headless OAuth troubleshooting; it explicitly separates consumer Gmail user OAuth from service-account ADC. `gws-setup-assistant` is a compatibility handoff that requires `gws-debug`, while `common-gws-opeartions` keeps stable service command patterns.
  Shared reusable skills should hold stable command patterns, while task-specific skills should reference them via `required_skills` instead of duplicating long command playbooks.
- `docs/` — longer technical documentation of architecture and decisions.
- `tests/` — automated tests.

## 3) Rules for Implementing Changes
For every non-trivial change, the agent **must update the relevant part of this file (`AGENTS.md`)**.

“Relevant part” primarily means:
1. changes to module responsibilities,
2. adding/removing a directory or significant component,
3. changing the message flow between channels, broker, and agent,
4. changing how memory persistence works,
5. changing tool contracts or skill registration,
6. new operational rules that other agents need to know.

If the architecture changes in more detail, also synchronize `docs/ARCHITECTURE.md`.

## 4) Definition of Done for Documentation
Before handing off the implementation, check:
- **what** changed is described,
- **where** the change is located (directory/module) is described,
- **how** data flow or responsibilities changed is described,
- AGENTS.md remains concise but up to date.

## 5) Practical Guidelines
- Do not do a broad documentation refactor unless needed; edit only affected sections.
- For larger changes, add a short “Migration note” (if behavior changes).
- If you add a new subsystem, include it in the “Architecture by Directory” section.
- Skills that wrap external CLIs must keep example commands aligned with the current CLI shape, include a `--help`/schema fallback for validation errors, and only reference prerequisite skill IDs that actually exist under `workspace/skills/`.
- When multiple skills use the same external CLI workflow, keep the reusable command patterns in a shared tracked skill and let task-specific skills add only domain policy, routing rules, and edge-case decisions.

## Migration Note
- Short-term memory is no longer partitioned by per-channel `chat_id`. New work should route conversational turns to the shared user history thread and cron turns to the dedicated cron history thread configured in `src/config.py`.
- Original channel and delivery `chat_id` still matter for outbound routing and should be preserved in message metadata when persisting history.
- Model selection no longer comes from `workspace/config/config.json`. New work should use env variables `SMART_MODEL`, `FAST_MODEL`, and `ROUTER_MODEL` with native PydanticAI `provider:model` strings.
- Skill registries no longer auto-bundle prerequisite skill bodies. If a skill lists `required_skills`, the agent must call `load_skill_details` separately for each prerequisite it needs.
- Runtime loop protection now uses a request-count cap (`request_limit`) on the main conversational agent instead of tool-call or token caps by default.
- Neo4j graph memory is optional. When `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` are present, the core agent attaches a PydanticAI capability that injects relevant graph-memory context and exposes a small library-backed read/write surface for durable facts, preferences, entities, and relations.
- Memory embeddings use PydanticAI embedder models configured via env (`MEMORY_EMBEDDING_PROVIDER`, `MEMORY_EMBEDDING_MODEL`, `MEMORY_EMBEDDING_DIMENSIONS`). Current default is Google Gemini embeddings (`google-gla:gemini-embedding-001`).
- Memory quality is controlled mainly by explicit tool usage policy and keeping the repo wrapper thin. The repository does not implement custom relation semantics or correction workflows.
- Voice services replaced ElevenLabs with two providers: TTS is now Cartesia (`cartesia` SDK, `AsyncCartesia`, mp3 output) and STT is now Google Chirp 3 (`google-cloud-speech` v2, online inline `recognize`, `language_codes=["auto"]`). Config moved from `ELEVENLABS_*` / `Settings.elevenlabs` to `CARTESIA_*` / `Settings.cartesia` and `STT_*` / `Settings.stt`. STT now requires `STT_CREDENTIALS_FILE`; remove `GOOGLE_APPLICATION_CREDENTIALS` from the Fergusson service environment so `gws` cannot fall back to the Chirp service account. Function names `speech_to_text` and `text_to_speech` and their `runners.py` call sites are unchanged; only the import path changed (`src.services.chirp3` / `src.services.cartesia`).
- **Auto-routing first stage**: `AgentManager.run` now consults a cheap `ROUTER_MODEL` before the full core agent. The router returns one of three actions: `answer` (reply returned directly), `clarify` (a disambiguation question returned directly, no core-agent cost), or `escalate` (run the full core agent unchanged). Vague requests are clarified at the router stage to save the expensive escalation round trip. The router has read-only Google Workspace tools (`list_inbox_emails`, `get_contact`, `list_upcoming_events`, `search_drive_docs`) so it can answer/prepare context cheaply; writes (e.g. `create_calendar_event`) escalate to the Core Agent. The router is fail-fast (any failure -> escalate) and gated by `Settings.router.enabled` (default on). Disable by setting `ROUTER_ENABLED=false` to restore previous direct-core-agent behavior.


## ExecPlans
When writing complex features or significant refactors, use an ExecPlan (as described in .agent/PLANS.md) from design to implementation.

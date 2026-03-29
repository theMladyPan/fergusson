# AGENTS.md — Repository Operating Guide

This document is binding for the entire repository (`/workspace/fergusson`).

## 1) Project Goal (brief)
Fergusson is a modular AI assistant with an event-driven architecture:
- **channels** (CLI, Discord, future inputs) receive messages,
- the **broker** (Redis) distributes them,
- the **core agent** applies native tools and reusable skills directly,
- **memory** is layered: one shared SQLite thread for recent conversation, optional Neo4j graph memory for durable structured facts/preferences, and `MEMORY.md` for human-readable long-term notes. Prompt guidance uses tiered memory placement: high-signal anchor facts in `MEMORY.md`, richer structured detail in graph memory, with compact graph-detail references in `MEMORY.md` when relevant. Graph memory is backed by `neo4j-agent-memory` with semantic/exact duplicate suppression and extractor prompt rules with do/don't examples. Outbound delivery remains channel-specific.
  Memory extraction policy prefers `store_preference` for tastes/interests/favorites and enforces first-person subject canonicalization through extractor prompt rules.

## 2) Architecture by Directory
- `src/agent/` — agent core, orchestration, shared-thread memory, Neo4j graph-memory capability, skill loading, archiver. Graph memory uses `neo4j-agent-memory` (long-term facts/preferences), library-style tools (`search_memory`, `get_memory_context`, `store_fact`, `store_preference`), semantic dedup, and temporal correction handling (`correction=true` closes previous conflicting facts by setting `valid_until`). Skill loading now returns one requested skill at a time; prerequisites are metadata hints that the agent must load explicitly.
- `src/broker/` — message bus and message schemas between channels and runtime.
- `src/channels/` — integration inputs/outputs (e.g., Discord, CLI adapters) that keep transport-specific `chat_id`s for delivery.
- `src/config.py` — environment-backed runtime settings; model selection uses `SMART_MODEL` / `FAST_MODEL` as PydanticAI `provider:model` strings, Neo4j uses `NEO4J_*` env vars, and `workspace/config/config.json` remains for non-model app config.
- `src/tools/` — tools invoked by the agent (bash, filesystem, web).
- `src/db/` — DB models and session layer for state persistence.
- `src/prompt/` — Jinja templates for system prompts (`core.md`, `archiver.md`) and extractor prompts (for example relational-memory extraction policy/examples).
  Prompt policy for memory is decision-oriented rather than hard imperative: the agent can choose whether to keep concise anchors in `MEMORY.md`, store detail in graph memory, and condense/relocate over-detailed `MEMORY.md` content into graph memory.
  Core communication policy should favor natural conversational phrasing by default (including Slovak when user speaks Slovak), avoid administrative/report-style confirmations for routine chat, and keep memory-save acknowledgments implicit unless explicit confirmation is needed.
  `core.md` should remain user-agnostic operational policy; `workspace/PERSONALITY.md` is for subjective user personalization (name/style/channel intent), while concrete routing identifiers like channel IDs belong in `MEMORY.md`.
- `workspace/skills/` — dynamic skills following the `SKILL.md` standard.
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
- Short-term memory is no longer partitioned by per-channel `chat_id`. New work should use the shared history thread configured in `src/config.py`.
- Original channel and delivery `chat_id` still matter for outbound routing and should be preserved in message metadata when persisting history.
- Model selection no longer comes from `workspace/config/config.json`. New work should use env variables `SMART_MODEL` and `FAST_MODEL` with native PydanticAI `provider:model` strings.
- Skill registries no longer auto-bundle prerequisite skill bodies. If a skill lists `required_skills`, the agent must call `load_skill_details` separately for each prerequisite it needs.
- Runtime loop protection now uses a request-count cap (`request_limit`) on the main conversational agent instead of tool-call or token caps by default.
- Neo4j graph memory is optional. When `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` are present, the core agent attaches a PydanticAI capability that injects relevant graph-memory context, exposes memory read/write tools, auto-extracts durable facts/preferences after successful turns, and suppresses duplicate writes with exact + semantic checks.
- Memory embeddings use PydanticAI embedder models configured via env (`MEMORY_EMBEDDING_PROVIDER`, `MEMORY_EMBEDDING_MODEL`, `MEMORY_EMBEDDING_DIMENSIONS`). Current default is Google Gemini embeddings (`google-gla:gemini-embedding-001`).
- Extraction quality is controlled primarily via extractor prompt rules and examples; code-level policy focuses on storage integrity (dedup, correction, provenance), not a hardcoded predicate whitelist.


## ExecPlans
When writing complex features or significant refactors, use an ExecPlan (as described in .agent/PLANS.md) from design to implementation.

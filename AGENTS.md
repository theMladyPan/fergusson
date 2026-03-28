# AGENTS.md — Repository Operating Guide

This document is binding for the entire repository (`/workspace/fergusson`).

## 1) Project Goal (brief)
Fergusson is a modular AI assistant with an event-driven architecture:
- **channels** (CLI, Discord, future inputs) receive messages,
- the **broker** (Redis) distributes them,
- the **core agent** decides between direct handling and delegating to a skill,
- **memory** is one shared SQLite thread across CLI, Discord, and Cron, while outbound delivery remains channel-specific.

## 2) Architecture by Directory
- `src/agent/` — agent core, orchestration, shared-thread memory, skill loading, archiver. Skill loading now returns one requested skill at a time; prerequisites are metadata hints that the agent must load explicitly.
- `src/broker/` — message bus and message schemas between channels and runtime.
- `src/channels/` — integration inputs/outputs (e.g., Discord, CLI adapters) that keep transport-specific `chat_id`s for delivery.
- `src/config.py` — environment-backed runtime settings; model selection uses `SMART_MODEL` / `FAST_MODEL` as PydanticAI `provider:model` strings, while `workspace/config/config.json` remains for non-model app config.
- `src/tools/` — tools invoked by the agent (bash, filesystem, web).
- `src/db/` — DB models and session layer for state persistence.
- `src/prompt/` — Jinja templates for system prompts.
- `workspace/skills/` — dynamic skills following the `SKILL.md` standard.
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

## Migration Note
- Short-term memory is no longer partitioned by per-channel `chat_id`. New work should use the shared history thread configured in `src/config.py`.
- Original channel and delivery `chat_id` still matter for outbound routing and should be preserved in message metadata when persisting history.
- Model selection no longer comes from `workspace/config/config.json`. New work should use env variables `SMART_MODEL` and `FAST_MODEL` with native PydanticAI `provider:model` strings.
- Skill registries no longer auto-bundle prerequisite skill bodies. If a skill lists `required_skills`, the agent must call `load_skill_details` separately for each prerequisite it needs.


## ExecPlans
When writing complex features or significant refactors, use an ExecPlan (as described in .agent/PLANS.md) from design to implementation.

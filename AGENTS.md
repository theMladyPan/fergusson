# AGENTS.md — Repository Operating Guide

This document is binding for the entire repository (`/workspace/fergusson`).

## 1) Project Goal (brief)
Fergusson is a modular AI assistant with an event-driven architecture:
- **channels** (CLI, Discord, future inputs) receive messages,
- the **broker** (Redis) distributes them,
- the **core agent** decides between direct handling and delegating to a skill,
- **memory** is per-chat in SQLite.

## 2) Architecture by Directory
- `src/agent/` — agent core, orchestration, memory, skill loading, archiver.
- `src/broker/` — message bus and message schemas between channels and runtime.
- `src/channels/` — integration inputs/outputs (e.g., Discord, CLI adapters).
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

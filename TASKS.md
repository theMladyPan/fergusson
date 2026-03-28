# Unified Single-Thread History Plan

## Architectural Decision Summary
- Fergusson should behave like one continuous conversation with the user.
- All inbound messages from `cli`, `discord`, and `cron` belong to one shared history thread.
- `cron` participates in the same shared history; when useful, cron-originated entries may be stored as system-context messages rather than normal user messages.
- Outbound delivery remains channel-specific. History is unified; transport is not.
- No backward-compatibility or data migration is required. Resetting SQLite state is acceptable because durable facts live in `MEMORY.md`.

## Repository Analysis
- Current short-term memory is keyed only by `chat_id` in [src/agent/memory.py](/home/odroid/fergusson/src/agent/memory.py).
- Persistent message and summary storage also key by `chat_id` in [src/db/models.py](/home/odroid/fergusson/src/db/models.py).
- Inbound identity is currently channel-local:
  - CLI always publishes `chat_id="cli_chat"` in [cli.py](/home/odroid/fergusson/cli.py).
  - Discord uses Discord `channel_id` as `chat_id` in [src/channels/discord.py](/home/odroid/fergusson/src/channels/discord.py).
  - Cron publishes either Discord default channel id or `cron_chat` in [src/runners.py](/home/odroid/fergusson/src/runners.py).
- The agent prompt and docs explicitly describe memory as isolated by `chat_id` in [src/prompt/core.j2](/home/odroid/fergusson/src/prompt/core.j2), [AGENTS.md](/home/odroid/fergusson/AGENTS.md), and [docs/ARCHITECTURE.md](/home/odroid/fergusson/docs/ARCHITECTURE.md).
- There are currently no tests covering cross-channel history identity, compaction behavior for shared history, or cron semantics.

## Phase 1: Analysis And Data Gathering
- Confirm every place where history identity is read, written, or inferred:
  - [src/agent/memory.py](/home/odroid/fergusson/src/agent/memory.py)
  - [src/db/models.py](/home/odroid/fergusson/src/db/models.py)
  - [src/broker/schemas.py](/home/odroid/fergusson/src/broker/schemas.py)
  - [src/runners.py](/home/odroid/fergusson/src/runners.py)
  - [cli.py](/home/odroid/fergusson/cli.py)
  - [src/channels/discord.py](/home/odroid/fergusson/src/channels/discord.py)
  - [src/agent/core.py](/home/odroid/fergusson/src/agent/core.py)
- Decide the minimum viable model change:
  - Prefer one global thread key over introducing conversation/session tables.
  - Keep channel metadata for observability and reply routing.
  - Decide whether cron messages need a first-class `role` distinction beyond current `user` / `assistant`.
- Review compaction assumptions:
  - Ensure compaction still works when all channels append to one history stream.
  - Ensure summary lookup also resolves against the same global thread key.
- Review prompt and tool implications:
  - Remove or rewrite prompt text that claims CLI and Discord are isolated.
  - Reassess whether `get_recent_chats()` still makes sense once memory is global.
- Prepare implementation baseline:
  - Snapshot current tests.
  - Clear or recreate SQLite state before verification if schema/data mismatch makes old data noisy.

## Phase 2: Implementation Plan
- Introduce one canonical history identifier used everywhere short-term memory is accessed.
  - Recommended approach: define a constant such as `GLOBAL_CHAT_ID = "main"` or equivalent central helper.
  - Keep `chat_id` on broker messages for channel delivery, but stop using channel-local ids as memory partition keys.
- Update message persistence and retrieval in [src/agent/memory.py](/home/odroid/fergusson/src/agent/memory.py):
  - Make `add_message`, `get_history`, and `check_and_compact` operate on the shared global thread key.
  - Preserve original inbound channel in stored metadata or existing `channel` column.
  - If cron should sometimes be treated as system context, add a controlled path for storing cron entries differently without creating a second thread.
- Update runtime flow in [src/runners.py](/home/odroid/fergusson/src/runners.py):
  - Resolve inbound messages to the global history key before loading history.
  - Persist all user, assistant, and cron-driven exchanges into the same thread.
  - Keep outbound replies addressed to the source channel and source `chat_id`.
  - Ensure background compaction runs against the shared thread key.
- Update schemas and identity semantics in [src/broker/schemas.py](/home/odroid/fergusson/src/broker/schemas.py) only if needed:
  - Keep transport schema stable unless a separate field is needed to distinguish delivery `chat_id` from storage thread id.
  - If added, prefer an explicit `history_thread_id` or similar instead of overloading `chat_id`.
- Update channel producers:
  - [cli.py](/home/odroid/fergusson/cli.py): continue sending CLI delivery metadata, but stop assuming CLI owns a separate history.
  - [src/channels/discord.py](/home/odroid/fergusson/src/channels/discord.py): continue using Discord `channel_id` for delivery/reply routing only.
  - [src/runners.py](/home/odroid/fergusson/src/runners.py): keep cron outbound behavior channel-specific while storing its effect in shared history.
- Update agent/tool behavior in [src/agent/core.py](/home/odroid/fergusson/src/agent/core.py):
  - Rewrite `get_recent_chats()` to mean recent delivery destinations, not separate memory threads.
  - Ensure `send_message_to_channel()` remains unchanged as a delivery tool.
  - Update dynamic context text if it currently implies channel-local memory.
- Update prompts and docs:
  - [src/prompt/core.j2](/home/odroid/fergusson/src/prompt/core.j2): replace memory-isolation language with single-thread language.
  - [AGENTS.md](/home/odroid/fergusson/AGENTS.md): update the project goal and persistence wording to reflect unified cross-channel history.
  - [docs/ARCHITECTURE.md](/home/odroid/fergusson/docs/ARCHITECTURE.md): synchronize memory and cross-channel sections with the new design.
- Add tests:
  - Unit tests for shared-history reads/writes across mixed channels.
  - Regression test proving outbound routing remains channel-specific.
  - Compaction test for the single shared thread.
  - Cron test covering shared history participation and any system-message treatment.

## Phase 3: Auto Testing And Final Review
- Automated test execution:
  - Run the existing test suite.
  - Add and run focused tests for cross-channel shared history.
  - If the DB schema changes materially, recreate the SQLite file during tests or use isolated temp DBs.
- Manual verification queries:
  - In CLI, send: `Remember that my favorite editor is Helix.`
  - In Discord, send: `What editor did I tell you I prefer?`
  - Expected result: the assistant answers from shared short-term history without needing `MEMORY.md`.
  - Trigger cron or simulate routine execution, then ask in CLI: `What did the routine just tell you to do?`
  - Expected result: cron context is visible in the same thread.
  - In CLI, ask for a Discord notification; verify the outbound message is delivered to Discord while the conversation remains one shared history.
- Review gate before finishing:
  - Inspect failing or flaky tests.
  - Manually review stored SQLite rows to confirm one thread key is used for history.
  - Confirm docs were updated because this is an architectural and persistence behavior change.
  - Share test results and the exact manual queries used before closing the task.

# Fergusson Agent Architecture & Skills Strategy

This document outlines the architectural decisions and logic behind the agent system in the Fergusson project. It serves as a guide for how the core agent operates, how sub-agents (skills) are structured, and how cross-channel communication is handled.

## 1. The Core Agent (The "Omnipotent" Router)
The Core Agent (`src/agent/core.py`) is the primary interface for all incoming user requests. It acts as an intelligent router and orchestrator.

**Logic & Capabilities:**
*   **Intent Recognition:** It analyzes the user's message to determine if it can handle the request directly using its built-in tools (Bash, Filesystem) or if the task requires specialized expertise.
*   **Guardrails:** Given its access to bash execution (`src/tools/bash.py`), it is configured to intercept hazardous commands (like `rm`, `sudo`) and explicitly request user permission before execution.
*   **Memory Integration:** It maintains a persistent context of the conversation using SQLite (`state.db`). It retrieves history based on the active `chat_id` (e.g., a specific Discord thread or the CLI session).

## 2. Agent-to-Agent (A2A) Delegation
To prevent the Core Agent from becoming bloated with too many instructions and tools, Fergusson uses an Agent-to-Agent (A2A) delegation model based on the [Pydantic-AI specification](https://ai.pydantic.dev/a2a/).

**How it works:**
*   The Core Agent possesses a specialized tool: `delegate_to_expert(expert_id, task)`.
*   When a complex task is identified (e.g., managing a Google Calendar), the Core Agent formulates a precise sub-task and invokes the relevant expert.
*   The "expert" is dynamically instantiated as an independent `pydantic-ai` Agent within the same async process. This ensures low latency while maintaining strict context boundaries.
*   The result of the expert's work is returned to the Core Agent, which synthesizes the final response for the user.

## 3. The Skills Standard (Sub-Agents)
Sub-agents are defined dynamically using the **Claude Code Skills Standard**. They are stored in `workspace/skills/`.

**Structure of a Skill:**
*   **`SKILL.md`:** The primary definition file.
    *   **YAML Frontmatter:** Located between `---` at the top of the file. It must contain at least `name` and `description`. This description is what the Core Agent reads to understand what the expert is capable of.
    *   **Markdown Body:** The system instructions for the sub-agent. This defines its persona, rules, and how it should approach tasks.

**Why this standard?**
Using a file-based standard allows us to hot-swap, update, or add new capabilities to the system without modifying the core python code. The `SkillRegistry` (`src/agent/skills.py`) parses these directories at startup and injects their metadata into the Core Agent's system prompt.

## 4. Cross-Channel Awareness
Fergusson operates across multiple channels (CLI, Discord, Cron) via a centralized Redis message broker.

**Architectural Choice:**
*   By default, the SQLite memory (`src/agent/memory.py`) isolates conversations strictly by their `chat_id`. A conversation happening in the CLI is unaware of a conversation happening in Discord. This prevents context contamination.
*   **Proactive Messaging:** To allow the agent to send messages across boundaries (e.g., asking it in the CLI to ping you on Discord), the Core Agent is equipped with two specific tools:
    1.  `get_recent_chats()`: Queries the database for the user's active `chat_id`s across different channels.
    2.  `send_message_to_channel(channel, chat_id, message)`: Injects a message directly into the Redis outbound queue for the target channel.

## 5. Future Expansions (Phase 5 & 6)
*   **Graph Memory (Neo4j):** Transitioning from isolated SQLite threads to a semantic graph database to connect concepts, entities, and long-term facts across all conversations.
*   **`MEMORY.md` Scratchpad:** A local file where the agent can write down transient state or plans that survive across reboots.
*   **`ROUTINE.md`:** Defining background tasks that the agent should evaluate periodically without explicit user prompts.

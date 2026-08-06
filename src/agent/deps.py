"""Shared agent dependency types.

Kept in a separate module so the router (`src.agent.router`) and the core agent
(`src.agent.core`) can both depend on `AgentDeps` without creating a circular
import between them.
"""

from dataclasses import dataclass


@dataclass
class AgentDeps:
    """Runtime dependencies injected into PydanticAI agents for a single turn.

    Attributes:
        chat_id: Transport-specific chat id used for outbound delivery.
        channel: Channel name (e.g. "cli", "discord", "cron").
        history_thread_id: Short-term history thread this turn belongs to.
        sender_id: Optional sender identifier for routing/memory scoping.
    """

    chat_id: str
    channel: str
    history_thread_id: str
    sender_id: str | None = None
    router_context: str | None = None

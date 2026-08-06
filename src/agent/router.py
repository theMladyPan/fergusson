"""Auto-routing first stage.

A cheap model (`router_model`, e.g. gemini-3.5-flash-lite) inspects the inbound
turn and decides whether it is simple enough to answer directly, or whether it
must be escalated to the full core agent (smart model + all tools).

Design decisions (see AGENTS.md):
- Structured classifier: the router returns ``RouteDecision`` with
  ``action`` in {"answer", "escalate"}.
- Read-only lightweight tools only (``read_file_content`` + DuckDuckGo search);
  no writes/skills. Tools must be fast; on any failure the router escalates
  instead of retrying (``retries=0``).
- Fail-fast: any exception during the router run (tool error, timeout, invalid
  structured output, usage limit) is swallowed and converted into an
  ``escalate`` decision. Escalating is always safe.
- Same conversation history is passed to both router and core agent; the router
  only sees the most recent ``history_window`` messages to stay cheap.
"""

from dataclasses import dataclass
from typing import Any, Literal

import anyio.to_thread
import logfire
from ddgs.ddgs import DDGS
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from src.agent.deps import AgentDeps
from src.config import settings
from src.tools.fs import read_file_content


class RouteDecision(BaseModel):
    """Structured decision returned by the router agent.

    Attributes:
        action: "answer" to reply directly from the router, or "escalate" to run
            the full core agent.
        reply: Final user-facing text when action == "answer". Empty on escalate.
    """

    action: Literal["answer", "escalate"] = Field(description="Whether to answer directly or escalate")
    reply: str = Field(default="", description="Final reply when action is 'answer'; empty on 'escalate'")


ROUTER_INSTRUCTIONS = """\
You are the router stage of the Fergusson assistant. Your job is to decide whether
the user's request is simple enough to answer directly, or must be escalated to the
larger model that has the full tool set (file edits, skills, bash, web fetch).

ANSWER directly (action="answer") ONLY for simple, plain requests such as:
- Greetings, small talk, acknowledgments.
- Factual recall, short rephrasings, clarifications.
- Questions you can fully resolve with at most one quick read-only lookup using
  read_file_content or web_search.

ESCALATE (action="escalate") for anything that needs:
- File writes/edits, multi-step tool chains, skills, bash, or URL content fetch.
- Long reasoning, code generation, planning, or deep analysis.
- Anything you are not confident you can fully and quickly resolve.

Rules:
- When unsure, ALWAYS escalate. Escalating is always safe; an over-eager direct
  answer that is wrong is worse than a cheap escalation.
- Your tools are read-only and must be FAST. If a tool call fails or would be slow,
  do NOT retry it — escalate immediately.
- When action="answer", put the complete user-facing reply in `reply` and match a
  natural, concise assistant tone. When action="escalate", leave `reply` empty.
"""


async def _web_search(query: str) -> str:
    """Read-only DuckDuckGo search returning a compact text digest.

    Implemented as a local wrapper (instead of ``pydantic_ai.common_tools.duckduckgo``)
    so the router agent's tool schema only sees a plain ``(query: str) -> str``
    signature. The upstream tool exposes the ``DDGS``/``DuckDuckGoResult`` types
    in its annotation globals; when a PydanticAI ``Agent`` is built inside a
    method *and* given a structured ``output_type``, pydantic resolves those
    foreign types against the caller's globals and fails schema generation.
    This wrapper keeps the cheap path fail-fast: any error surfaces as a tool
    failure and the router escalates instead of answering.
    """

    def _sync_search() -> list[dict]:
        with DDGS() as client:
            return list(client.text(query, max_results=5))

    results = await anyio.to_thread.run_sync(_sync_search)
    if not results:
        return "No web results found."
    return "\n\n".join(
        f"- {r.get('title', '')}: {r.get('body', '')} ({r.get('href', '')})" for r in results
    )


@dataclass
class RoutedResult:
    """Minimal result shim exposing the attributes runners.py consumes.

    This mirrors the subset of ``pydantic_ai.AgentRunResult`` that the agent loop
    uses: ``.output`` (the final text) and ``.usage()`` (token accounting). Used
    on the cheap "answer" path so the caller does not need to branch on the
    router vs. core-agent result types.
    """

    output: str
    _usage: Any

    def usage(self) -> Any:
        return self._usage


class RouterAgent:
    """Wraps the cheap PydanticAI router agent and exposes a fail-fast `route`."""

    def __init__(self, model: Any) -> None:
        self.agent = Agent(
            model=model,
            name="RouterAgent",
            deps_type=AgentDeps,
            output_type=RouteDecision,
            instructions=ROUTER_INSTRUCTIONS,
            tool_timeout=settings.router.tool_timeout,
            retries=settings.router.retries,
            tools=[read_file_content, _web_search],
        )

    async def route(
        self,
        user_input: str,
        history: list | None,
        deps: AgentDeps,
    ) -> tuple[RouteDecision, Any]:
        """Run the router and return (decision, usage).

        Any failure (tool error, timeout, invalid output, usage limit) is logged
        and converted into an ``escalate`` decision with empty usage, so the
        caller always falls back to the full core agent.

        Args:
            user_input: The latest user message text.
            history: Full conversation history; only the most recent
                ``router.history_window`` messages are forwarded to keep the
                router cheap.
            deps: AgentDeps for the current turn.

        Returns:
            A tuple of (RouteDecision, usage-object-or-None).
        """
        # Keep the router cheap: feed only the tail of the conversation.
        window = settings.router.history_window
        recent_history = list(history or [])[-window:] if window > 0 else list(history or [])

        try:
            with logfire.span("router.decide") as span:
                result = await self.agent.run(
                    user_input,
                    deps=deps,
                    message_history=recent_history,
                    usage_limits=UsageLimits(request_limit=settings.router.request_limit),
                )
                decision: RouteDecision = result.output
                span.set_attributes({"router.action": decision.action})
                logfire.info("router.decision", action=decision.action)
                return decision, result.usage()
        except Exception as exc:
            # Fail fast into escalation. Escalating is always safe.
            logfire.warning(f"Router failed, escalating to core agent: {exc}")
            return RouteDecision(action="escalate", reply=""), None

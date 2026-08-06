"""Auto-routing first stage.

A cheap model (`router_model`, e.g. gemini-3.5-flash-lite) inspects the inbound
turn and decides whether it is simple enough to answer directly, or whether it
must be escalated to the full core agent (smart model + all tools).

Design decisions (see AGENTS.md):
- Structured classifier: the router returns ``RouteDecision`` with
  ``action`` in {"answer", "clarify", "escalate"}. "clarify" lets the router ask
  one concise clarifying question directly (no core-agent cost) when the request
  is too vague to act on, saving the expensive escalation round trip.
- Read-only lightweight tools only (``read_file_content`` + DuckDuckGo search);
  no writes/skills. Tools must be fast; on any failure the router escalates
  instead of retrying (``retries=1`` — one retry, then escalate).
- Fail-fast: any exception during the router run (tool error, timeout, invalid
  structured output, usage limit) is swallowed and converted into an
  ``escalate`` decision. Escalating is always safe.
- Same conversation history is passed to both router and core agent; the router
  only sees the most recent ``history_window`` messages to stay cheap.
"""

from dataclasses import dataclass
from typing import Any, Literal

import logfire
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.tools import Tool
from pydantic_ai.usage import UsageLimits

from src.agent.deps import AgentDeps
from src.config import settings
from src.tools.exa import _exa_search
from src.tools.fs import read_file_content
from src.tools.gws import get_contact, list_inbox_emails, list_upcoming_events, search_drive_docs


class RouteDecision(BaseModel):
    """Structured decision returned by the router agent.

    Attributes:
        action: "answer" to reply directly from the router, "clarify" to ask a
            concise clarifying question directly (also returned to the user,
            no core-agent cost), or "escalate" to run the full core agent.
        reply: Final user-facing text when action is "answer" or the clarifying
            question when action is "clarify". Empty on escalate.
    """

    action: Literal["answer", "clarify", "escalate"] = Field(
        description="Whether to answer directly, ask for clarification, or escalate"
    )
    reply: str = Field(
        default="",
        description="Final reply when action is 'answer' or the clarifying question when 'clarify'; empty on 'escalate'",
    )


ROUTER_INSTRUCTIONS = """\
You are the router stage of the Fergusson assistant. Your job is to decide whether
the user's request is simple enough to answer directly, needs clarification first,
or must be escalated to the larger model that has the full tool set (file edits,
skills, bash, web fetch).

ANSWER directly (action="answer") ONLY for simple, plain requests such as:
- Greetings, small talk, acknowledgments.
- Factual recall, short rephrasings, clarifications.
- Questions you can fully resolve with at most one quick read-only lookup using
  read_file_content or web_search.
- Routine/cron check-ins that surface no actionable item (no reminder to send,
  no file to update, no schedule to change, no task to run). These are
  informational status checks only — answer briefly and do not escalate.

You also have read-only Google Workspace tools (Gmail, Calendar, Drive). Use
them to answer or prepare context directly and cheaply:
- `list_inbox_emails(limit, summarize)` — recent inbox (summarize=True is slow;
  use only when the user wants email body summaries).
- `get_contact(query, kind)` — find an email address or phone number for a
  person by mining email threads.
- `list_upcoming_events(days, summarize)` — upcoming calendar agenda.
- `search_drive_docs(query, limit)` — Drive search with one-sentence doc summaries.
These reads are cheap; use them to answer directly when the user just wants a
quick inbox scan, agenda, contact lookup, or file search. Any WRITE (create/
invite an event, send mail, modify files) must ESCALATE — the Core Agent holds
those tools. If a read tool fails (auth/timeout), do not retry — escalate.

CLARIFY (action="clarify") when the request is too vague to act on confidently.
Ask ONE concise clarifying question in the user's language and, when possible,
offer concrete options or smart guesses drawn from conversation context instead
of open-ended questions. Put only the clarifying question in `reply`. Do NOT
escalate just to ask for clarification — asking directly saves the expensive
core-agent round trip. If the request is specific enough to act on, do not
clarify; escalate (or answer) instead.

Examples where clarification is required (match the user's language):
- "do I have contact for kubo?" — unclear contact type and full name. Ask:
  "Do you want the telephone number or email? Is it Jakub Rubint or another person?"
- "prečítaj mi emaily" — unclear account, count, and folder. Ask:
  "Chceš emaily pre themladypan@gmail.com, a z inboxu alebo len neprečítané? Koľko?"
- "pošli mu správu" — unclear recipient ("him"), channel, and content. Ask:
  "Komu to mám poslať a cez ktorý kanál (Discord, email, CLI)? Čo má správa obsahovať?"

ESCALATE (action="escalate") for anything that needs:
- File writes/edits, multi-step tool chains, skills, bash, or URL content fetch.
- Audio handling, including speech transcription or synthesis; the Core Agent has
  dedicated STT/TTS tools that must be used instead of shell commands.
- Long reasoning, code generation, planning, or deep analysis.
- A cron/routine check that surfaces a concrete action to take (send a reminder,
  update a file/schedule, run a task, notify a channel, fix something). Only
  escalate when there is real work to do — not for every routine check.
- Anything you are not confident you can fully and quickly resolve, AFTER you
  have already ruled out that a clarification would disambiguate it.

Rules:
- Prefer CLARIFY over ESCALATE when a single short question would remove the
  ambiguity. This saves time and tokens for the user.
- When unsure between answer and escalate, ALWAYS escalate. Escalating is always
  safe; an over-eager direct answer that is wrong is worse than a cheap escalation.
- Your tools are read-only and must be FAST. If a tool call fails or would be slow,
  do NOT retry it — escalate immediately.
- When action="answer" or action="clarify", put the complete user-facing reply
  (answer or the clarifying question) in `reply` and match a natural, concise
  assistant tone. When action="escalate", leave `reply` empty.
"""


async def _router_web_search(query: str) -> str:
    """Exa web search bound to the router's low-latency fast type.

    Thin wrapper over the shared ``_exa_search`` helper so the router reuses the
    same HTTP path as the core agent but with ``settings.exa.router_search_type``
    (default "fast") to keep the cheap path fast. Registered under the tool name
    ``web_search`` so the LLM sees the same tool surface in both agents.
    """

    return await _exa_search(query, settings.exa.router_search_type, settings.exa.num_results)


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
            tools=[
                read_file_content,
                Tool(
                    _router_web_search,
                    name="web_search",
                    description="Search the web via Exa and return a compact digest of relevant excerpts.",
                ),
                # Read-only Google Workspace tools so the router can answer and
                # prepare context cheaply. Writes stay on the Core Agent.
                list_inbox_emails,
                get_contact,
                list_upcoming_events,
                search_drive_docs,
            ],
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

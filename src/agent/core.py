import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import logfire
from httpx import AsyncClient, HTTPStatusError
from jinja2 import Template
from pydantic_ai import Agent, AgentRunResult, RunContext
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai import ModelRetry
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from pydantic_ai.usage import UsageLimits
from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential

from src.agent.memory import get_recent_delivery_destinations
from src.agent.skills import SkillRegistry
from src.broker.bus import MessageBus
from src.config import settings
from src.db.session import async_session
from src.tools import all_tools


def create_retrying_client() -> AsyncClient:
    """Create a client with smart retry handling for multiple error types."""

    def should_retry_status(response):
        """Raise exceptions for retryable HTTP status codes."""
        if response.status_code in (429, 502, 503, 504):
            response.raise_for_status()  # This will raise HTTPStatusError

    transport = AsyncTenacityTransport(
        config=RetryConfig(
            # Retry on HTTP errors and connection issues
            retry=retry_if_exception_type((HTTPStatusError, ConnectionError)),
            # Smart waiting: respects Retry-After headers, falls back to exponential backoff
            wait=wait_retry_after(fallback_strategy=wait_exponential(multiplier=1, max=30), max_wait=60),
            # Stop after 5 attempts
            stop=stop_after_attempt(5),
            # Re-raise the last exception if all retries fail
            reraise=True,
        ),
        validate_response=should_retry_status,
    )
    return AsyncClient(transport=transport, timeout=15)


def resolve_model_spec(model_spec: str, retrying_client: AsyncClient | None = None) -> Any:
    """Resolve an env-provided provider:model string into a model instance when wrapping is needed."""

    normalized_spec = model_spec.strip()
    if not normalized_spec or ":" not in normalized_spec:
        raise ValueError(f"Invalid model spec '{model_spec}'. Expected a non-empty PydanticAI provider:model string.")

    provider_name, model_name = normalized_spec.split(":", 1)

    if provider_name in {"openai", "openai-chat"}:
        client = retrying_client or create_retrying_client()
        if settings.debug:
            logfire.instrument_httpx(client=client)

        provider = OpenAIProvider(
            api_key=os.environ.get("OPENAI_API_KEY"),
            http_client=client,
        )

        # This is required for prompts and completions to be captured in the spans.
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
        logfire.instrument_openai(openai_client=provider.client)
        return OpenAIChatModel(
            model_name=model_name,
            provider=provider,
        )

    if provider_name == "google-gla":
        client = retrying_client or create_retrying_client()
        if settings.debug:
            logfire.instrument_httpx(client=client)

        provider = GoogleProvider(
            api_key=os.environ.get("GOOGLE_API_KEY"),
            http_client=client,
            vertexai=False,
        )
        logfire.instrument_google_genai(provider=provider.client)
        return GoogleModel(
            model_name=model_name,
            provider=provider,
        )

    # All other native PydanticAI model strings pass through unchanged.
    return normalized_spec


@dataclass
class AgentDeps:
    chat_id: str
    channel: str
    history_thread_id: str


class AgentManager:
    def _build_system_prompt(self) -> str:
        template_path = Path(__file__).parents[1] / "prompt" / "core.md"
        with open(template_path, "r") as f:
            template = Template(f.read())

        with open(settings.workspace_folder / "PERSONALITY.md", "r") as f:
            personality_md_content = f.read()

        with open(settings.workspace_folder / "MEMORY.md", "r") as f:
            memory_md_content = f.read()

        system_prompt = template.render(
            current_date=datetime.now().strftime("%B %d, %Y"),
            personality_md_content=personality_md_content,
            memory_md_content=memory_md_content,
            request_limit=settings.agent.request_limit,
        )
        skills_prompt = self.registry.get_skill_catalog_prompt()
        if skills_prompt:
            system_prompt = f"{system_prompt.rstrip()}\n\n{skills_prompt}"

        return system_prompt

    def __init__(self, bus: MessageBus):
        self.bus = bus

        self.smart_model = resolve_model_spec(settings.smart_model)
        self.fast_model = resolve_model_spec(settings.fast_model)

        # Use the smart model for the Core Agent
        self.model = self.smart_model

        self.registry = SkillRegistry()
        self.registry.discover()

        system_prompt = self._build_system_prompt()

        # Define the Core Agent
        self.core_agent = Agent(
            model=self.model,
            name="CoreAgent",
            deps_type=AgentDeps,
            instructions=system_prompt,
            tool_timeout=settings.agent.tool_timeout,
            retries=settings.agent.retries,
            tools=[duckduckgo_search_tool()],
        )

        self.request_limit_recovery_agent = Agent(
            model=self.model,
            name="RequestLimitRecoveryAgent",
            deps_type=AgentDeps,
            instructions=(
                f"{system_prompt}\n\n"
                "# Request Limit Recovery\n"
                "You are handling a turn where the runtime request limit was reached. "
                "You have no tools in this recovery mode. Respond directly to the user from the existing conversation context only. "
                "Explain briefly that this turn took too many attempts, avoid internal exception names, and ask for a narrower follow-up if needed."
            ),
            tool_timeout=settings.agent.tool_timeout,
            retries=settings.agent.retries,
        )

        @self.core_agent.system_prompt
        def dynamic_context_prompt(ctx: RunContext[AgentDeps]) -> str:
            return (
                "\n\nCURRENT CONTEXT:\n"
                f"You are currently operating in channel: '{ctx.deps.channel}' and chat_id: '{ctx.deps.chat_id}'. "
                f"All channels share the same short-term history thread: '{ctx.deps.history_thread_id}'."
            )

        @self.request_limit_recovery_agent.system_prompt
        def dynamic_recovery_context_prompt(ctx: RunContext[AgentDeps]) -> str:
            return (
                "\n\nCURRENT CONTEXT:\n"
                f"You are currently operating in channel: '{ctx.deps.channel}' and chat_id: '{ctx.deps.chat_id}'. "
                f"All channels share the same short-term history thread: '{ctx.deps.history_thread_id}'."
            )

        for tool in all_tools:
            self.core_agent.tool_plain(tool)

        @self.core_agent.tool_plain
        async def load_skill_details(skill_id: str) -> str:
            """
            Loads the full instructions for a specific skill only.
            Required skills are not loaded automatically and must be loaded separately if needed.
            Use this when a skill from the catalog is relevant and you need its full workflow guidance.
            """

            try:
                skill_details = self.registry.load_skill_details(skill_id)
            except KeyError:
                raise ModelRetry(self.registry.build_unknown_skill_message(skill_id))
            return (
                "Authoritative skill guidance for the requested skill is loaded below. "
                "Follow these instructions for the current task. Required skills listed below are hints only "
                "and are not loaded automatically.\n\n"
                f"{skill_details}"
            )

        @self.core_agent.tool_plain
        async def send_message_to_channel(channel: str, message: str, chat_id: str) -> str:
            """
            Sends a message proactively to a specific channel and chat_id (e.g., discord, cli).
            Use get_recent_chats to find the correct chat_id if you don't know it.
            """
            from src.broker.schemas import OutboundMessage

            reply = OutboundMessage(
                chat_id=chat_id,
                content=message,
                channel=channel,
            )
            await self.bus.publish_outbound(reply)
            return f"Message successfully sent to {channel} (chat_id: {chat_id})"

        @self.core_agent.tool_plain
        async def get_recent_chats() -> str:
            """
            Returns recent outbound destinations gathered from shared-history metadata.
            Use this to find the correct channel/chat_id pair when you need to send a proactive message.
            """

            async with async_session() as session:
                recent_chats = await get_recent_delivery_destinations(session)
                if not recent_chats:
                    return "No recent chats found."
                return "\n".join(recent_chats)

    async def run(
        self, user_input: str, history: list | None = None, chat_id: str = "cli", channel: str = "cli"
    ) -> AgentRunResult:
        """Runs the core agent loop."""

        deps = AgentDeps(
            chat_id=chat_id,
            channel=channel,
            history_thread_id=settings.shared_history_thread_id,
        )

        try:
            return await self.core_agent.run(
                user_input,
                deps=deps,
                message_history=history,
                usage_limits=UsageLimits(
                    request_limit=settings.agent.request_limit,
                ),
            )
        except UsageLimitExceeded as exc:
            logfire.warning(f"Agent usage limit exceeded: {exc}")
            recovery_prompt = (
                f"{user_input}\n\n"
                "[System notice: The previous attempt hit the runtime request limit for this turn. "
                "Respond without calling tools, based only on available context.]"
            )
            return await self.request_limit_recovery_agent.run(
                recovery_prompt,
                deps=deps,
                message_history=history,
            )

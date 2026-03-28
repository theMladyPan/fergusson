import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import logfire
from httpx import AsyncClient, HTTPStatusError
from jinja2 import Template
from pydantic_ai import Agent, AgentRunResult, RunContext
from pydantic_ai.common_tools.duckduckgo import duckduckgo_search_tool
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential

from src.agent.memory import get_recent_delivery_destinations
from src.agent.skills import SkillRegistry
from src.broker.bus import MessageBus
from src.config import app_config, settings
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


@dataclass
class AgentDeps:
    chat_id: str
    channel: str
    history_thread_id: str


class AgentManager:
    def _build_system_prompt(self) -> str:
        template_path = Path(__file__).parents[1] / "prompt" / "core.j2"
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
            tool_usage_limit=settings.agent.tool_call_limit,
        )
        skills_prompt = self.registry.get_skill_catalog_prompt()
        if skills_prompt:
            system_prompt = f"{system_prompt.rstrip()}\n\n{skills_prompt}"

        return system_prompt

    def _build_model(self, model_config):
        provider_name = model_config.provider
        provider_info = app_config.providers.get(provider_name)

        retrying_client = create_retrying_client()

        if settings.debug:
            logfire.instrument_httpx(
                client=retrying_client,
            )

        if not provider_info:
            logfire.warning(f"Provider '{provider_name}' not found in config. Defaulting to provider's type as name.")
            return f"{provider_name}:{model_config.model}"

        api_key = provider_info.api_key
        if not api_key and provider_info.api_key_env:
            api_key = os.environ.get(provider_info.api_key_env)

        if provider_info.type == "openai":
            provider = OpenAIProvider(
                base_url=provider_info.base_url,
                api_key=api_key,
                http_client=retrying_client,
            )

            # This is required for prompts and completions to be captured in the spans
            os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
            logfire.instrument_openai(openai_client=provider.client)

            return OpenAIChatModel(
                model_name=model_config.model,
                provider=provider,
            )
        elif provider_info.type == "gemini":
            provider = GoogleProvider(
                api_key=api_key,
                http_client=retrying_client,
            )
            logfire.instrument_google_genai(provider=provider.client)

            # Auto-instrument Gemini interactions for better observability
            return GoogleModel(
                model_name=model_config.model,
                provider=provider,
            )
        else:
            return f"{provider_info.type}:{model_config.model}"

    def __init__(self, bus: MessageBus):
        self.bus = bus

        self.smart_model = self._build_model(app_config.models.smart)
        self.fast_model = self._build_model(app_config.models.fast)

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

        @self.core_agent.system_prompt
        def dynamic_context_prompt(ctx: RunContext[AgentDeps]) -> str:
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
            Loads the full instructions for a specific skill and any prerequisite skills it declares.
            Use this when a skill from the catalog is relevant and you need its full workflow guidance.
            """

            bundle = self.registry.load_skill_bundle(skill_id)
            return (
                "Authoritative skill guidance loaded below. Follow these instructions for the current task, "
                "including any declared tool restrictions and prerequisite skills.\n\n"
                f"{bundle}"
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

        result = await self.core_agent.run(
            user_input,
            deps=deps,
            message_history=history,
        )

        return result

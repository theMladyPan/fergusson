import os
from datetime import datetime
from pathlib import Path

import logfire
from httpx import AsyncClient, HTTPStatusError
from jinja2 import Template
from pydantic_ai import Agent, ModelRetry, RunContext, AgentRunResult
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from sqlalchemy.future import select
from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential

from src.agent.skills import SkillRegistry
from src.broker.bus import MessageBus
from src.config import app_config, settings
from src.db.models import Message
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


class AgentManager:
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

        template_path = Path(__file__).parents[1] / "prompt" / "core.j2"
        with open(template_path, "r") as f:
            template = Template(f.read())

        with open(settings.workspace_folder / "AGENTS.md", "r") as f:
            agents_md_content = f.read()

        with open(settings.workspace_folder / "MEMORY.md", "r") as f:
            memory_md_content = f.read()

        system_prompt = template.render(
            current_date=datetime.now().strftime("%B %d, %Y"),
            agents_md_content=agents_md_content,
            memory_md_content=memory_md_content,
        )

        # Define the Core Agent
        self.core_agent = Agent(
            model=self.model,
            name="CoreAgent",
            instructions=system_prompt,
            tool_timeout=settings.agent.tool_timeout,
            retries=settings.agent.retries,
        )

        for tool in all_tools:
            self.core_agent.tool_plain(tool)

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
            Returns a list of recent chat_ids and their channels from the database history.
            Use this to find the correct chat_id when you need to send a message to another channel.
            """

            async with async_session() as session:
                # Get distinct chat_ids and their recent usage
                result = await session.execute(
                    select(Message.chat_id, Message.channel, Message.timestamp)
                    .order_by(Message.timestamp.desc())
                    .limit(50)
                )
                messages = result.all()

                # Deduplicate by chat_id, preserving the most recent timestamp
                seen = set()
                recent_chats = []
                for msg in messages:
                    if msg.chat_id not in seen:
                        seen.add(msg.chat_id)
                        recent_chats.append(
                            f"Channel: {msg.channel}, Chat ID: {msg.chat_id}, Last Active: {msg.timestamp}"
                        )

                if not recent_chats:
                    return "No recent chats found."
                return "\n".join(recent_chats)

        async def delegate_to_expert(ctx: RunContext[None], expert_id: str, task: str) -> str:
            """
            Delegates a specific task to a specialized sub-agent. Use this tool if the request topic is covered by one of the experts mentioned below.

            You can and should use this tool without asking for permission.

            Args:
                expert_id: The ID of the expert.
                task: A detailed description of what the expert should do. Always provide expected output format and any relevant context.

            Returns:
                The result from the expert agent after completing the task.
            """
            skill = self.registry.skills.get(expert_id)
            if not skill:
                raise ModelRetry(
                    f"Expert '{expert_id}' not found. Please check the list of available experts and their "
                    "capabilities in the tool's documentation."
                )

            # Create a dynamic sub-agent for this skill using the fast model
            expert_agent = Agent(
                model=self.fast_model,
                name=f"ExpertAgent-{expert_id}",
                system_prompt=skill.instructions,
                tool_timeout=settings.subagent.tool_timeout,
                retries=settings.subagent.retries,
            )

            # Sub-agents get the same toolset for now,
            # NOTE: think through later if we want to limit tools for sub-agents
            for tool in all_tools:
                expert_agent.tool_plain(tool)

            with logfire.span(f"Running expert agent '{expert_id}'", task=task) as _:
                result = await expert_agent.run(task)

            return result.output

        delegate_to_expert.__doc__ = f"{self.registry.get_skill_list_prompt()}"
        self.core_agent.tool(delegate_to_expert)

    async def run(self, user_input: str, history: list | None = None) -> AgentRunResult:
        """Runs the core agent loop."""

        result = await self.core_agent.run(
            user_input,
            message_history=history,
        )

        return result

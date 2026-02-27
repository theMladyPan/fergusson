import os
from datetime import datetime

import logfire
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.agent.skills import SkillRegistry
from src.broker.bus import MessageBus
from src.config import app_config
from src.tools.bash import run_bash_command
from src.tools.fs import list_files, read_file_content, write_file_content


class AgentManager:
    def __init__(self, bus: MessageBus):
        self.bus = bus
        # Initialize the model based on provider
        if app_config.llm.provider == "openai":
            api_key = app_config.llm.api_key or os.environ.get(app_config.llm.api_key_env)

            # Create a custom AsyncOpenAI client
            provider = OpenAIProvider(
                base_url=app_config.llm.base_url,
                api_key=api_key,
            )

            self.model = OpenAIChatModel(
                model_name=app_config.llm.model,
                provider=provider,
            )
        else:
            # Fallback or other providers
            self.model = f"{app_config.llm.provider}:{app_config.llm.model}"

        self.registry = SkillRegistry()
        self.registry.discover()

        system_prompt = f"""
You are Fergusson, an omnipotent personal assistant. 
You have full access to the user's filesystem and bash shell. 
Your goal is to be helpful, concise, and efficient.
Your knowledge cutoff is at December 2024.

# CRITICAL RULES:
- if it is possible to delegate task to a specialist, you MUST delegate using 'delegate_to_expert' tool instead of doing it yourself. You are provided with a list of specialists and their capabilities. 

### Environment:
Today is {datetime.now().strftime("%B %d, %Y")}.
"""
        logfire.debug(f"System prompt for Core Agent:\n{system_prompt}")

        # Define the Core Agent
        self.core_agent = Agent(
            model=self.model,
            instructions=system_prompt,
        )

        # Register tools to Core Agent
        self.core_agent.tool_plain(run_bash_command)
        self.core_agent.tool_plain(list_files)
        self.core_agent.tool_plain(read_file_content)
        self.core_agent.tool_plain(write_file_content)

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
            from sqlalchemy.future import select

            from src.db.models import Message
            from src.db.session import async_session

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

        # @self.core_agent.tool
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
                return f"Error: Expert '{expert_id}' not found."

            logfire.info(f"Delegating task to expert '{expert_id}': {task}")

            # Create a dynamic sub-agent for this skill
            expert_agent = Agent(self.model, system_prompt=skill.instructions)

            # Sub-agents get the same toolset for now
            expert_agent.tool_plain(run_bash_command)
            expert_agent.tool_plain(list_files)
            expert_agent.tool_plain(read_file_content)
            expert_agent.tool_plain(write_file_content)

            result = await expert_agent.run(task)
            return result.output

        delegate_to_expert.__doc__ = f""" 
Available experts you should delegate to when appropriate:
{self.registry.get_skill_list_prompt()}
"""
        self.core_agent.tool(delegate_to_expert)

    async def run(self, user_input: str, history: list | None = None) -> str:
        """Runs the core agent loop."""
        with logfire.span("core_agent_run", input=user_input):
            # Note: History conversion to pydantic-ai format would happen here
            result = await self.core_agent.run(user_input, message_history=history)
            return result.output

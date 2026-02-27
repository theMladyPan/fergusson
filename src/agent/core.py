import os

from loguru import logger
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.agent.skills import Skill, SkillRegistry
from src.config import app_config
from src.tools.bash import run_bash_command
from src.tools.fs import list_files, read_file_content, write_file_content


class AgentManager:
    def __init__(self):
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

        system_prompt = (
            "You are Fergusson, an omnipotent personal assistant. "
            "You have full access to the user's filesystem and bash shell. "
            "Your goal is to be helpful, concise, and efficient."
            "CRITICAL RULES:"
            "1. If you use a bool that is marked as hazardous, you MUST first ask the user for permission."
            "2. You can delegate complex specialized tasks to 'experts' (sub-agents) using the delegate_to_expert tool."
            f"{self.registry.get_skill_list_prompt()}"
        )
        logger.debug(f"System prompt for Core Agent:\n{system_prompt}")

        # Define the Core Agent
        self.core_agent = Agent(
            self.model,
            system_prompt=system_prompt,
        )

        # Register tools to Core Agent
        self.core_agent.tool_plain(run_bash_command)
        self.core_agent.tool_plain(list_files)
        self.core_agent.tool_plain(read_file_content)
        self.core_agent.tool_plain(write_file_content)

        @self.core_agent.tool
        async def delegate_to_expert(ctx: RunContext[None], expert_id: str, task: str) -> str:
            """
            Delegates a specific task to a specialized sub-agent.

            Args:
                expert_id: The ID of the expert.
                task: A detailed description of what the expert should do.
            """
            skill = self.registry.skills.get(expert_id)
            if not skill:
                return f"Error: Expert '{expert_id}' not found."

            logger.info(f"Delegating task to expert '{expert_id}': {task}")

            # Create a dynamic sub-agent for this skill
            expert_agent = Agent(self.model, system_prompt=skill.instructions)

            # Sub-agents get the same toolset for now
            expert_agent.tool_plain(run_bash_command)
            expert_agent.tool_plain(list_files)
            expert_agent.tool_plain(read_file_content)
            expert_agent.tool_plain(write_file_content)

            result = await expert_agent.run(task)
            return result.output

    async def run(self, user_input: str, history: list | None = None) -> str:
        """Runs the core agent loop."""
        # Note: History conversion to pydantic-ai format would happen here
        result = await self.core_agent.run(user_input, message_history=history)
        return result.output

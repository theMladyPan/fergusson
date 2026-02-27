from datetime import datetime
from pathlib import Path

import logfire
from jinja2 import Template
from pydantic_ai import Agent

from src.db.models import Message


class Archiver:
    def __init__(self, model):
        self.model = model
        self.agent = Agent(self.model)

        template_path = Path(__file__).parents[1] / "prompt" / "archiver.j2"
        with open(template_path, "r") as f:
            self.template = Template(f.read())

    async def summarize(self, messages: list[Message], previous_summary: str = None) -> str:
        """
        Generate a summary for a list of messages using the archiver agent.
        """
        if not messages:
            return ""

        context = {
            "current_date": datetime.now().strftime("%B %d, %Y"),
            "start_id": messages[0].id,
            "end_id": messages[-1].id,
            "messages": messages,
            "previous_summary": previous_summary,
        }

        prompt = self.template.render(**context)

        with logfire.span("archiver_summarize", start_id=messages[0].id, end_id=messages[-1].id):
            result = await self.agent.run(prompt)
            return result.output

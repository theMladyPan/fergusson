from pathlib import Path
from typing import Callable

import logfire
import yaml
from pydantic import BaseModel, Field

from src.config import settings


class SkillMetadata(BaseModel):
    name: str
    description: str
    version: str = "0.1.0"
    tools: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    id: str
    instructions: str
    metadata: SkillMetadata
    path: Path


class SkillRegistry:
    def __init__(self, skills_dir: str | Path = settings.workspace_folder / "skills"):
        self.skills_dir = skills_dir if isinstance(skills_dir, Path) else Path(skills_dir)
        self.skills: dict[str, Skill] = {}

    def _parse_skill_md(self, content: str) -> tuple[dict, str]:
        """Parses YAML frontmatter from the beginning of the Markdown content."""

        if content.startswith("---\n"):
            end_idx = content.find("\n---\n", 4)
            if end_idx != -1:
                frontmatter = content[4:end_idx]
                instructions = content[end_idx + 5 :].strip()
                try:
                    metadata = yaml.safe_load(frontmatter)
                    return metadata if isinstance(metadata, dict) else {}, instructions

                except Exception:
                    pass

        return {}, content

    def discover(self):
        """Scans the skills directory for valid skill packages."""

        if not self.skills_dir.exists():
            return

        for skill_path in self.skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            skill_md = skill_path / "SKILL.md"

            if skill_md.exists():
                raw_content = skill_md.read_text(encoding="utf-8")
                frontmatter_meta, instructions = self._parse_skill_md(raw_content)

                # Base metadata defaults
                metadata_dict = {"name": skill_path.name, "description": "No description provided."}

                # 1. Try to apply from frontmatter (Claude Code standard)
                if frontmatter_meta:
                    version = frontmatter_meta.get("version") or metadata_dict.get("version", "0.1.0")
                    metadata_dict["name"] = frontmatter_meta.get("name", metadata_dict["name"])
                    metadata_dict["description"] = frontmatter_meta.get("description", metadata_dict["description"])
                    metadata_dict["version"] = version
                    metadata_dict["tools"] = frontmatter_meta.get("tools", [])

                # 2. Fallback to openai.yaml if frontmatter didn't provide specific fields
                meta_yaml = skill_path / "agents" / "openai.yaml"
                if meta_yaml.exists():
                    try:
                        with open(meta_yaml, "r") as f:
                            raw_meta = yaml.safe_load(f)
                            if not frontmatter_meta.get("name"):
                                metadata_dict["name"] = raw_meta.get("name", metadata_dict["name"])
                            if not frontmatter_meta.get("description"):
                                metadata_dict["description"] = raw_meta.get("description", metadata_dict["description"])
                    except Exception:
                        pass

                skill_id = skill_path.name
                self.skills[skill_id] = Skill(
                    id=skill_id,
                    instructions=instructions,
                    metadata=SkillMetadata(**metadata_dict),
                    path=skill_path,
                )
                logfire.info(f"Discovered skill: {skill_id} - {metadata_dict['description']}")

    def get_tools_for_skill(self, skill_id: str, available_tools: list[Callable]) -> list[Callable]:
        """Return the subset of tools a skill is allowed to use."""

        skill = self.skills[skill_id]
        requested_tools = skill.metadata.tools
        if not requested_tools:
            return available_tools

        tools_by_name = {tool.__name__: tool for tool in available_tools}
        selected_tools = []
        seen_tools = set()
        unknown_tools = []

        for tool_name in requested_tools:
            tool = tools_by_name.get(tool_name)
            if tool is None:
                unknown_tools.append(tool_name)
                continue
            if tool_name in seen_tools:
                continue

            seen_tools.add(tool_name)
            selected_tools.append(tool)

        if unknown_tools:
            logfire.warning(
                "Skill requested unknown tools; ignoring them.",
                skill_id=skill_id,
                unknown_tools=unknown_tools,
            )

        return selected_tools

    def get_skill_list_prompt(self) -> str:
        """Return a markdown table describing available sub-agents for the core agent's context."""

        if not self.skills:
            return "No specialized sub-agents available."

        lines = [
            "Available specialized experts:",
            "",
            "| Expert ID | Description |",
            "|-----------|-------------|",
        ]
        for s in self.skills.values():
            # Escape pipe characters to maintain table structure
            description = s.metadata.description.replace("|", "\\|")
            lines.append(f"| {s.id} | {description} |")
        return "\n".join(lines)

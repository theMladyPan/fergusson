from pathlib import Path

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

        discovered_skills: dict[str, Skill] = {}

        if not self.skills_dir.exists():
            self.skills = discovered_skills
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
                    tools = frontmatter_meta.get("tools", [])
                    metadata_dict["tools"] = tools if isinstance(tools, list) else []

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
                discovered_skills[skill_id] = Skill(
                    id=skill_id,
                    instructions=instructions,
                    metadata=SkillMetadata(**metadata_dict),
                    path=skill_path,
                )
                logfire.info(f"Discovered skill: {skill_id} - {metadata_dict['description']}")

        self.skills = discovered_skills

    def get_skill_list_prompt(self) -> str:
        """Return a markdown table describing the available skills."""

        if not self.skills:
            return "No skills available."

        lines = [
            "Available skills:",
            "",
            "| Skill ID | Description |",
            "|----------|-------------|",
        ]
        for skill in sorted(self.skills.values(), key=lambda skill: skill.id):
            # Escape pipe characters to maintain table structure
            description = skill.metadata.description.replace("|", "\\|")
            lines.append(f"| {skill.id} | {description} |")
        return "\n".join(lines)

    def get_skill_instructions_prompt(self) -> str | None:
        """Return a prompt section that exposes all discovered skills to an agent."""

        if not self.skills:
            return None

        lines = [
            "# Available Skills",
            "You can use any of the following skills directly when they match the user's request.",
            "Do not treat skills as separate agents; they are reusable instructions available to you.",
            "If a skill declares a `tools` list, only use those built-in tools while applying that skill.",
            "",
            self.get_skill_list_prompt(),
        ]

        for skill in sorted(self.skills.values(), key=lambda entry: entry.id):
            lines.extend(
                [
                    "",
                    f"## Skill: {skill.metadata.name} (`{skill.id}`)",
                    (
                        f"Allowed tools for this skill: {', '.join(skill.metadata.tools)}"
                        if skill.metadata.tools
                        else "Allowed tools for this skill: all built-in tools"
                    ),
                    skill.instructions.strip(),
                ]
            )

        return "\n".join(lines)

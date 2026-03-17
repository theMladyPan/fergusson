from pathlib import Path
from difflib import get_close_matches

import logfire
import yaml
from pydantic import BaseModel, Field

from src.config import settings


class SkillMetadata(BaseModel):
    name: str
    description: str
    version: str = "0.1.0"
    tools: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    required_bins: list[str] = Field(default_factory=list)


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

    def _extract_openclaw_requirements(self, frontmatter_meta: dict) -> tuple[list[str], list[str]]:
        """Extract prerequisite skill and binary requirements from frontmatter metadata."""

        metadata = frontmatter_meta.get("metadata", {})
        if not isinstance(metadata, dict):
            return [], []

        openclaw = metadata.get("openclaw", {})
        if not isinstance(openclaw, dict):
            return [], []

        requires = openclaw.get("requires", {})
        if not isinstance(requires, dict):
            return [], []

        required_skills = requires.get("skills", [])
        required_bins = requires.get("bins", [])

        return (
            required_skills if isinstance(required_skills, list) else [],
            required_bins if isinstance(required_bins, list) else [],
        )

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
                    required_skills, required_bins = self._extract_openclaw_requirements(frontmatter_meta)
                    metadata_dict["required_skills"] = required_skills
                    metadata_dict["required_bins"] = required_bins

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

    def _format_tool_list(self, tools: list[str]) -> str:
        return ", ".join(tools) if tools else "all built-in tools"

    def _format_value_list(self, values: list[str]) -> str:
        return ", ".join(values) if values else "none"

    def _render_skill_detail_block(self, skill: Skill) -> str:
        return "\n".join(
            [
                f"## Skill: {skill.metadata.name} (`{skill.id}`)",
                f"Allowed tools: {self._format_tool_list(skill.metadata.tools)}",
                f"Required skills: {self._format_value_list(skill.metadata.required_skills)}",
                f"Required binaries: {self._format_value_list(skill.metadata.required_bins)}",
                "",
                skill.instructions.strip(),
            ]
        )

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

    def get_skill_catalog_prompt(self) -> str | None:
        """Return a prompt section that exposes only skill headers to an agent."""

        if not self.skills:
            return None

        lines = [
            "# Available Skills",
            "You can discover and apply the following skills when they match the user's request.",
            "Do not treat skills as separate agents; they are reusable instructions available to you.",
            "This catalog only includes routing headers. Call `load_skill_details` before executing a non-trivial skill workflow.",
            "",
            self.get_skill_list_prompt(),
        ]

        for skill in sorted(self.skills.values(), key=lambda entry: entry.id):
            lines.extend(
                [
                    "",
                    f"## Skill: {skill.metadata.name} (`{skill.id}`)",
                    f"Description: {skill.metadata.description}",
                    f"Allowed tools: {self._format_tool_list(skill.metadata.tools)}",
                    f"Required skills: {self._format_value_list(skill.metadata.required_skills)}",
                    f"Required binaries: {self._format_value_list(skill.metadata.required_bins)}",
                ]
            )

        return "\n".join(lines)

    def load_skill_bundle(self, skill_id: str) -> str:
        """Return the full instructions for a skill and its prerequisites."""

        if skill_id not in self.skills:
            available = sorted(self.skills)
            suggestions = get_close_matches(skill_id, available, n=3)
            suggestion_text = f" Close matches: {', '.join(suggestions)}." if suggestions else ""
            raise KeyError(f"Unknown skill '{skill_id}'. Available skills: {', '.join(available)}.{suggestion_text}")

        ordered_skills: list[Skill] = []
        visiting: list[str] = []
        seen: set[str] = set()

        def visit(current_skill_id: str) -> None:
            if current_skill_id in seen:
                return
            if current_skill_id in visiting:
                cycle_start = visiting.index(current_skill_id)
                cycle = visiting[cycle_start:] + [current_skill_id]
                raise ValueError(f"Cycle detected in skill prerequisites: {' -> '.join(cycle)}")
            if current_skill_id not in self.skills:
                raise KeyError(
                    f"Skill '{skill_id}' requires missing skill '{current_skill_id}'."
                )

            visiting.append(current_skill_id)
            skill = self.skills[current_skill_id]
            for required_skill_id in skill.metadata.required_skills:
                visit(required_skill_id)
            visiting.pop()
            seen.add(current_skill_id)
            ordered_skills.append(skill)

        visit(skill_id)

        return "\n\n".join(self._render_skill_detail_block(skill) for skill in ordered_skills)

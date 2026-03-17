from pathlib import Path

from src.agent.skills import SkillRegistry


def _write_skill(skill_dir: Path, skill_id: str, skill_md: str, openai_yaml: str | None = None) -> None:
    path = skill_dir / skill_id
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(skill_md, encoding="utf-8")

    if openai_yaml is not None:
        agents_dir = path / "agents"
        agents_dir.mkdir()
        (agents_dir / "openai.yaml").write_text(openai_yaml, encoding="utf-8")


def test_get_skill_list_prompt_describes_skills_not_experts(tmp_path: Path):
    _write_skill(
        tmp_path,
        "python-helper",
        """---
name: Python Helper
description: Helps with Python | scripting.
---

Use pytest for targeted verification.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    prompt = registry.get_skill_list_prompt()

    assert "Available skills:" in prompt
    assert "| Skill ID | Description |" in prompt
    assert "specialized experts" not in prompt
    assert "scripting." in prompt
    assert "\\|" in prompt


def test_get_skill_instructions_prompt_includes_full_skill_content(tmp_path: Path):
    _write_skill(
        tmp_path,
        "researcher",
        """---
name: Researcher
description: Search the web and summarize findings.
---

1. Gather current sources.
2. Cite the sources in the final answer.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    prompt = registry.get_skill_instructions_prompt()

    assert "Do not treat skills as separate agents" in prompt
    assert "## Skill: Researcher (`researcher`)" in prompt
    assert "Gather current sources." in prompt
    assert "Cite the sources in the final answer." in prompt
    assert "Allowed tools for this skill: all built-in tools" in prompt


def test_discover_reloads_skills_and_uses_openai_yaml_fallback(tmp_path: Path):
    _write_skill(
        tmp_path,
        "fallback-skill",
        """---
version: 1.0.0
---

Fallback instructions.
""",
        openai_yaml="""
name: Fallback Skill
description: Loaded from openai.yaml
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()
    assert registry.skills["fallback-skill"].metadata.name == "Fallback Skill"

    (tmp_path / "fallback-skill").rename(tmp_path / "renamed-skill")
    registry.discover()

    assert "fallback-skill" not in registry.skills
    assert "renamed-skill" in registry.skills


def test_discover_reads_tool_restrictions_from_frontmatter(tmp_path: Path):
    _write_skill(
        tmp_path,
        "reviewer",
        """---
name: reviewer
description: Reviews code changes
version: 1.2.3
tools:
  - read_file_content
  - list_files
---

# Reviewer
""",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    skill = registry.skills["reviewer"]
    assert skill.metadata.version == "1.2.3"
    assert skill.metadata.tools == ["read_file_content", "list_files"]


def test_get_skill_instructions_prompt_includes_tool_restrictions(tmp_path: Path):
    _write_skill(
        tmp_path,
        "readonly-reviewer",
        """---
name: readonly-reviewer
description: Reviews code without editing
tools:
  - read_file_content
  - list_files
---

# Readonly Reviewer
Only inspect repository state and summarize findings.
""",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    prompt = registry.get_skill_instructions_prompt()

    assert "If a skill declares a `tools` list, only use those built-in tools while applying that skill." in prompt
    assert "Allowed tools for this skill: read_file_content, list_files" in prompt
    assert "Only inspect repository state and summarize findings." in prompt

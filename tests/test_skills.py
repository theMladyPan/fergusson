from src.agent.skills import SkillRegistry


def test_discover_reads_tool_restrictions_from_frontmatter(tmp_path):
    skill_dir = tmp_path / "reviewer"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
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
        encoding="utf-8",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    skill = registry.skills["reviewer"]
    assert skill.metadata.version == "1.2.3"
    assert skill.metadata.tools == ["read_file_content", "list_files"]


def test_get_tools_for_skill_defaults_to_all_available_tools(tmp_path):
    skill_dir = tmp_path / "generalist"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: generalist
description: Handles general work
---

# Generalist
""",
        encoding="utf-8",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    def alpha():
        return "alpha"

    def beta():
        return "beta"

    assert registry.get_tools_for_skill("generalist", [alpha, beta]) == [alpha, beta]


def test_get_tools_for_skill_filters_to_requested_tool_names(tmp_path):
    skill_dir = tmp_path / "readonly-reviewer"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: readonly-reviewer
description: Reviews code without editing
tools:
  - beta
  - missing
  - alpha
  - beta
---

# Readonly Reviewer
""",
        encoding="utf-8",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    def alpha():
        return "alpha"

    def beta():
        return "beta"

    assert registry.get_tools_for_skill("readonly-reviewer", [alpha, beta]) == [beta, alpha]

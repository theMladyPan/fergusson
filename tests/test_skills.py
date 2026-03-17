import os
import sys
from pathlib import Path

import pytest

os.environ["DEBUG"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.core import AgentManager
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


def test_get_skill_catalog_prompt_includes_headers_but_not_full_content(tmp_path: Path):
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

    prompt = registry.get_skill_catalog_prompt()

    assert "Do not treat skills as separate agents" in prompt
    assert "Call `load_skill_details` before executing a non-trivial skill workflow." in prompt
    assert "## Skill: Researcher (`researcher`)" in prompt
    assert "Description: Search the web and summarize findings." in prompt
    assert "Allowed tools: all built-in tools" in prompt
    assert "Required skills: none" in prompt
    assert "Required binaries: none" in prompt
    assert "Gather current sources." not in prompt
    assert "Cite the sources in the final answer." not in prompt


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


def test_discover_reads_openclaw_requirements(tmp_path: Path):
    _write_skill(
        tmp_path,
        "persona-reviewer",
        """---
name: persona-reviewer
description: Reviews code without editing
metadata:
  openclaw:
    requires:
      bins:
        - gh
      skills:
        - git-helper
---

# Persona Reviewer
Only inspect repository state and summarize findings.
""",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    skill = registry.skills["persona-reviewer"]
    assert skill.metadata.required_bins == ["gh"]
    assert skill.metadata.required_skills == ["git-helper"]


def test_get_skill_catalog_prompt_includes_tool_restrictions_and_requirements(tmp_path: Path):
    _write_skill(
        tmp_path,
        "readonly-reviewer",
        """---
name: readonly-reviewer
description: Reviews code without editing
tools:
  - read_file_content
  - list_files
metadata:
  openclaw:
    requires:
      bins:
        - git
      skills:
        - repo-map
---

# Readonly Reviewer
Only inspect repository state and summarize findings.
""",
    )

    registry = SkillRegistry(skills_dir=tmp_path)
    registry.discover()

    prompt = registry.get_skill_catalog_prompt()

    assert "Allowed tools: read_file_content, list_files" in prompt
    assert "Required skills: repo-map" in prompt
    assert "Required binaries: git" in prompt
    assert "Only inspect repository state and summarize findings." not in prompt


def test_load_skill_bundle_returns_requested_skill_content(tmp_path: Path):
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

    bundle = registry.load_skill_bundle("researcher")

    assert "## Skill: Researcher (`researcher`)" in bundle
    assert "Gather current sources." in bundle
    assert "Cite the sources in the final answer." in bundle


def test_load_skill_bundle_includes_prerequisites_in_dependency_order(tmp_path: Path):
    _write_skill(
        tmp_path,
        "gws-shared",
        """---
name: gws-shared
description: Shared gws helpers.
---

Shared instructions.
""",
    )
    _write_skill(
        tmp_path,
        "gws-gmail",
        """---
name: gws-gmail
description: Gmail helpers.
metadata:
  openclaw:
    requires:
      skills:
        - gws-shared
---

Gmail instructions.
""",
    )
    _write_skill(
        tmp_path,
        "persona",
        """---
name: persona
description: Persona wrapper.
metadata:
  openclaw:
    requires:
      skills:
        - gws-gmail
        - gws-shared
---

Persona instructions.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    bundle = registry.load_skill_bundle("persona")

    assert bundle.index("## Skill: gws-shared (`gws-shared`)") < bundle.index("## Skill: gws-gmail (`gws-gmail`)")
    assert bundle.index("## Skill: gws-gmail (`gws-gmail`)") < bundle.index("## Skill: persona (`persona`)")
    assert bundle.count("## Skill: gws-shared (`gws-shared`)") == 1


def test_load_skill_bundle_detects_cycles(tmp_path: Path):
    _write_skill(
        tmp_path,
        "alpha",
        """---
name: alpha
description: Alpha skill.
metadata:
  openclaw:
    requires:
      skills:
        - beta
---

Alpha instructions.
""",
    )
    _write_skill(
        tmp_path,
        "beta",
        """---
name: beta
description: Beta skill.
metadata:
  openclaw:
    requires:
      skills:
        - alpha
---

Beta instructions.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    with pytest.raises(ValueError, match="Cycle detected in skill prerequisites: alpha -> beta -> alpha"):
        registry.load_skill_bundle("alpha")


def test_load_skill_bundle_reports_unknown_skill_with_suggestions(tmp_path: Path):
    _write_skill(
        tmp_path,
        "gws-gmail",
        """---
name: gws-gmail
description: Gmail helpers.
---

Gmail instructions.
""",
    )
    _write_skill(
        tmp_path,
        "gws-drive",
        """---
name: gws-drive
description: Drive helpers.
---

Drive instructions.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    with pytest.raises(KeyError, match="Unknown skill 'gws-gmai'.*Close matches: gws-gmail"):
        registry.load_skill_bundle("gws-gmai")


def test_agent_system_prompt_includes_skill_catalog_not_full_bodies(tmp_path: Path):
    _write_skill(
        tmp_path,
        "researcher",
        """---
name: Researcher
description: Search the web and summarize findings.
tools:
  - get_content_from_url
metadata:
  openclaw:
    requires:
      bins:
        - curl
      skills:
        - summarizer
---

Gather current sources.
""",
    )
    _write_skill(
        tmp_path,
        "summarizer",
        """---
name: Summarizer
description: Condense findings.
---

Condense findings carefully.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    manager = AgentManager.__new__(AgentManager)
    manager.registry = registry

    prompt = AgentManager._build_system_prompt(manager)

    assert "Treat those headers as discovery hints, not full instructions." in prompt
    assert "you MUST call `load_skill_details` before doing substantive work." in prompt
    assert "If the skill says another skill must be read or loaded first, you MUST load that prerequisite skill before continuing." in prompt
    assert "## Skill: Researcher (`researcher`)" in prompt
    assert "Description: Search the web and summarize findings." in prompt
    assert "Allowed tools: get_content_from_url" in prompt
    assert "Required skills: summarizer" in prompt
    assert "Required binaries: curl" in prompt
    assert "Gather current sources." not in prompt
    assert "Condense findings carefully." not in prompt

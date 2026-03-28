import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

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
    assert "Required skills are hints only. They are not loaded automatically" in prompt
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
    assert "Missing required skills: repo-map" in prompt
    assert "Only inspect repository state and summarize findings." not in prompt


def test_load_skill_details_returns_requested_skill_content_only(tmp_path: Path):
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

    details = registry.load_skill_details("researcher")

    assert "## Skill: Researcher (`researcher`)" in details
    assert "Gather current sources." in details
    assert "Cite the sources in the final answer." in details


def test_load_skill_details_does_not_inline_prerequisite_skills(tmp_path: Path):
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

    details = registry.load_skill_details("persona")

    assert "## Skill: persona (`persona`)" in details
    assert "Required skills: gws-gmail, gws-shared" in details
    assert "Loading behavior: Required skills listed here are not loaded automatically." in details
    assert "## Skill: gws-shared (`gws-shared`)" not in details
    assert "## Skill: gws-gmail (`gws-gmail`)" not in details


def test_discover_warns_about_missing_required_skills_and_surfaces_them(tmp_path: Path):
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
        - gamma
---

Alpha instructions.
""",
    )

    registry = SkillRegistry(tmp_path)
    registry.discover()

    prompt = registry.get_skill_catalog_prompt()
    details = registry.load_skill_details("alpha")

    assert "Missing required skills: gamma" in prompt
    assert "Missing required skills: gamma" in details


def test_load_skill_details_reports_unknown_skill_with_suggestions(tmp_path: Path):
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
        registry.load_skill_details("gws-gmai")


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
    assert "`load_skill_details` loads only the requested skill." in prompt
    assert "you MUST load that prerequisite skill explicitly before continuing." in prompt
    assert "## Skill: Researcher (`researcher`)" in prompt
    assert "Description: Search the web and summarize findings." in prompt
    assert "Allowed tools: get_content_from_url" in prompt
    assert "Required skills: summarizer" in prompt
    assert "Required binaries: curl" in prompt
    assert "Gather current sources." not in prompt
    assert "Condense findings carefully." not in prompt


@pytest.mark.asyncio
async def test_agent_manager_run_passes_usage_limits(monkeypatch):
    captured = {}

    async def fake_run(user_input, **kwargs):
        captured["user_input"] = user_input
        captured["kwargs"] = kwargs
        return SimpleNamespace(output="ok")

    manager = AgentManager.__new__(AgentManager)
    manager.core_agent = SimpleNamespace(run=fake_run)

    await AgentManager.run(manager, "hello", history=[], chat_id="cli_chat", channel="cli")

    usage_limits = captured["kwargs"]["usage_limits"]
    assert usage_limits.request_limit == 10
    assert usage_limits.tool_calls_limit is None
    assert captured["kwargs"]["message_history"] == []
    assert captured["kwargs"]["deps"].chat_id == "cli_chat"


@pytest.mark.asyncio
async def test_agent_manager_run_uses_recovery_agent_after_usage_limit():
    call_order = []
    captured = {}

    async def core_run(user_input, **kwargs):
        call_order.append("core")
        raise UsageLimitExceeded("tool limit reached")

    async def recovery_run(user_input, **kwargs):
        call_order.append("recovery")
        captured["user_input"] = user_input
        captured["kwargs"] = kwargs
        return SimpleNamespace(output="Recovered response")

    manager = AgentManager.__new__(AgentManager)
    manager.core_agent = SimpleNamespace(run=core_run)
    manager.request_limit_recovery_agent = SimpleNamespace(run=recovery_run)

    result = await AgentManager.run(manager, "hello", history=[], chat_id="cli_chat", channel="cli")

    assert result.output == "Recovered response"
    assert call_order == ["core", "recovery"]
    assert "runtime request limit" in captured["user_input"]
    assert "usage_limits" not in captured["kwargs"]


def test_common_gws_operations_skill_encodes_gws_cli_fallbacks():
    repo_root = Path(__file__).resolve().parents[1]
    skill_path = repo_root / "workspace" / "skills" / "common-gws-opeartions" / "SKILL.md"

    content = skill_path.read_text(encoding="utf-8")

    assert "gws gmail +triage --max 10 --format table" in content
    assert "gws gmail list" in content
    assert "do not guess alternate helper subcommands" in content
    assert "gws gmail --help" in content
    assert "gws schema <resource>.<method>" in content

    registry = SkillRegistry(repo_root / "workspace" / "skills")
    registry.discover()

    skill = registry.skills["common-gws-opeartions"]
    assert skill.metadata.required_bins == ["gws"]
    assert skill.metadata.missing_required_skills == []

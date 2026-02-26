import yaml
from pathlib import Path
from typing import Dict, Optional, List
from pydantic import BaseModel

class SkillMetadata(BaseModel):
    name: str
    description: str
    version: str = "0.1.0"

class Skill(BaseModel):
    id: str
    instructions: str
    metadata: SkillMetadata
    path: Path

class SkillRegistry:
    def __init__(self, skills_dir: str = "workspace/skills"):
        self.skills_dir = Path(skills_dir)
        self.skills: Dict[str, Skill] = {}

    def discover(self):
        """Scans the skills directory for valid skill packages."""
        if not self.skills_dir.exists():
            return

        for skill_path in self.skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            skill_md = skill_path / "SKILL.md"
            meta_yaml = skill_path / "agents" / "openai.yaml"

            if skill_md.exists():
                instructions = skill_md.read_text(encoding="utf-8")
                
                # Load metadata from openai.yaml if exists, else fallback to dir name
                metadata_dict = {"name": skill_path.name, "description": "No description provided."}
                if meta_yaml.exists():
                    try:
                        with open(meta_yaml, "r") as f:
                            raw_meta = yaml.safe_load(f)
                            # Handle different possible schema structures
                            metadata_dict["name"] = raw_meta.get("name", metadata_dict["name"])
                            metadata_dict["description"] = raw_meta.get("description", metadata_dict["description"])
                    except Exception:
                        pass
                
                skill_id = skill_path.name
                self.skills[skill_id] = Skill(
                    id=skill_id,
                    instructions=instructions,
                    metadata=SkillMetadata(**metadata_dict),
                    path=skill_path
                )

    def get_skill_list_prompt(self) -> str:
        """Returns a string describing available sub-agents for the core agent's context."""
        if not self.skills:
            return "No specialized sub-agents available."
        
        lines = ["Available specialized experts:"]
        for s in self.skills.values():
            lines.append(f"- {s.id}: {s.metadata.description}")
        return "
".join(lines)

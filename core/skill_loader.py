"""
Skill 发现与加载机制。

SKILL.md 格式：
    ---
    name: <skill_name>
    description: <when to use>
    triggers: [...]
    ---

    # <Skill 标题>
    ...正文（执行流程、脚本参数等）...
"""
from pathlib import Path
from dataclasses import dataclass, field
import re

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

_RESERVED_NAME_WORDS = {"anthropic", "claude"}
_VALID_NAME_RE = re.compile(r'^[\w][\w\-]*$', re.UNICODE)


@dataclass
class SkillMeta:
    name: str
    description: str
    triggers: list = field(default_factory=list)


class Skill:
    def __init__(self, skill_dir):
        self.dir = skill_dir
        self.name = skill_dir.name
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            raise ValueError(f"SKILL.md not found in {skill_dir}")
        self.meta, self.content = self._parse(skill_md.read_text(encoding="utf-8"))

    @staticmethod
    def _parse(text):
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
        if not match:
            raise ValueError("SKILL.md missing frontmatter")
        fm_text, content = match.groups()

        if not HAS_YAML:
            raise RuntimeError("pyyaml required: pip install pyyaml")
        fm = yaml.safe_load(fm_text) or {}

        name = fm.get("name", "").strip()
        description = fm.get("description", "").strip()

        if not name:
            print(f"[skill_loader] WARNING: SKILL.md missing 'name' in frontmatter")
        elif any(w in name.lower() for w in _RESERVED_NAME_WORDS):
            print(f"[skill_loader] WARNING: skill name '{name}' contains reserved word")
        elif not _VALID_NAME_RE.match(name):
            print(f"[skill_loader] WARNING: skill name '{name}' contains invalid characters (use letters, digits, hyphens)")
        if not description:
            print(f"[skill_loader] WARNING: skill '{name}' has no description — the agent cannot match it to user requests")

        meta = SkillMeta(
            name=name,
            description=description,
            triggers=fm.get("triggers", []) or [],
        )

        return meta, content.strip()

    def brief(self, index: int = 0) -> str:
        prefix = f"{index}. " if index > 0 else "- "
        return f"{prefix}**{self.name}**\n   {self.meta.description}"

    def full(self) -> str:
        return f"# Skill: {self.name}\n\n{self.content}"


class SkillRegistry:
    def __init__(self, skills_dir):
        self.skills_dir = skills_dir
        self.skills = {}
        self._discover()

    def _discover(self):
        for d in sorted(self.skills_dir.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                try:
                    skill = Skill(d)
                    self.skills[skill.name] = skill
                except Exception as e:
                    print(f"[skill_loader] failed to load {d.name}: {e}")

    def list_briefs(self) -> str:
        if not self.skills:
            return "（暂无可用 skill）"
        return "\n\n".join(
            s.brief(index=i + 1)
            for i, s in enumerate(self.skills.values())
        )

    def load(self, skill_name) -> str:
        if skill_name not in self.skills:
            available = ", ".join(self.skills.keys())
            return f"Skill '{skill_name}' 不存在。可用 skills: {available}"
        return self.skills[skill_name].full()

"""
Skills — reusable instructions the agent loads on demand.

`loader.py` has the format and the progressive-disclosure design; `tools/skill.py` is
how an agent pulls one in.
"""

from stcode.core.harness.skills.loader import (
    PROJECT_SKILLS_DIRNAME,
    SKILL_FILENAMES,
    SKILLS_PATH_ENV,
    USER_SKILLS_DIR,
    Skill,
    SkillRegistry,
    parse_frontmatter,
    parse_skill_file,
    skill_search_paths,
)

__all__ = [
    "PROJECT_SKILLS_DIRNAME",
    "SKILLS_PATH_ENV",
    "SKILL_FILENAMES",
    "USER_SKILLS_DIR",
    "Skill",
    "SkillRegistry",
    "parse_frontmatter",
    "parse_skill_file",
    "skill_search_paths",
]

"""
Skill loading — `SKILL.md` files as context the agent pulls in when it needs them.

A skill is a folder with a `SKILL.md` at its root: YAML frontmatter naming it and
saying when it applies, then markdown instructions, then whatever supporting files the
instructions reference.

    ~/.agents/skills/pdf/
      SKILL.md          <- frontmatter + instructions
      reference.md      <- read on demand, from inside the instructions
      scripts/…

**Progressive disclosure is the whole design.** Discovery reads only the frontmatter —
two lines per skill — and the system prompt lists those. The body is loaded only when
the agent calls `skill(name)`, and the body's own references (`reference.md`,
`scripts/`) are loaded only if it follows them with `read`. Nine skills therefore cost
roughly 400 tokens of catalogue rather than 90,000 tokens of instructions, and the
deep material stays free to be long.

The format is deliberately the same one Claude Code and other agents use, and the
default search path (`~/.agents/skills`) is the cross-agent convention — a skill written
once should work in whichever agent the user happens to be driving.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

SKILL_FILENAMES = ("SKILL.md", "skill.md")
USER_SKILLS_DIR = Path.home() / ".agents" / "skills"
PROJECT_SKILLS_DIRNAME = ".agents/skills"
SKILLS_PATH_ENV = "STCODE_SKILLS_PATH"
"""Colon-separated extra directories, searched before the conventional ones."""

MAX_SKILL_BYTES = 256 * 1024
"""A SKILL.md larger than this is a mistake — long material belongs in a file the
instructions point at, so it is read only when it is actually needed."""


@dataclass
class Skill:
    """One discovered skill. `body` is None until someone asks for it."""

    name: str
    description: str
    path: Path
    """The `SKILL.md` itself. `path.parent` is the skill's root, which is what relative
    references inside the body resolve against."""

    source: str = "user"
    metadata: dict[str, str | list[str]] = field(default_factory=dict)
    body: str | None = None

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def allowed_tools(self) -> list[str]:
        """Tools the skill declares it needs, if it declares any.

        Advisory, not a sandbox: it narrows what a sub-agent is *given* when a skill is
        the reason it was spawned. A skill cannot grant itself a tool the session does
        not already have.
        """
        raw = self.metadata.get("allowed-tools") or self.metadata.get("allowed_tools") or []
        return raw if isinstance(raw, list) else [item.strip() for item in raw.split(",") if item.strip()]

    def load(self) -> str:
        """Read and cache the instructions."""
        if self.body is None:
            _, self.body = parse_skill_file(self.path)
        return self.body

    def catalogue_line(self) -> str:
        return f"- **{self.name}** — {self.description}"


def parse_frontmatter(text: str) -> tuple[dict[str, str | list[str]], str]:
    """Split `---`-delimited frontmatter from the body.

    A deliberately small YAML subset — scalars, quoted scalars, block lists, inline
    lists, and indented continuations — rather than a YAML dependency. Skill
    frontmatter is two or three keys in practice, and the failure mode of a partial
    parser here is a skill with a missing field, which `discover` reports and skips.
    """
    metadata: dict[str, str | list[str]] = {}
    if not text.startswith("---"):
        return metadata, text

    lines = text.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return metadata, text  # unterminated block: it is body text, not frontmatter

    key: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        if line.lstrip().startswith("- ") and key:
            existing = metadata.get(key)
            item = _unquote(line.lstrip()[2:].strip())
            metadata[key] = [*existing, item] if isinstance(existing, list) else [item]
            continue

        if line[0].isspace() and key:
            # A wrapped scalar. Join with a space; the newline was formatting.
            previous = metadata.get(key)
            if isinstance(previous, str):
                metadata[key] = f"{previous} {line.strip()}"
            continue

        name, separator, value = line.partition(":")
        if not separator:
            continue
        key = name.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            metadata[key] = [_unquote(part.strip()) for part in value[1:-1].split(",") if part.strip()]
        else:
            metadata[key] = _unquote(value)

    return metadata, "\n".join(lines[end + 1 :]).lstrip("\n")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_skill_file(path: Path) -> tuple[dict[str, str | list[str]], str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
        text = text[:MAX_SKILL_BYTES]
    return parse_frontmatter(text)


def skill_search_paths(cwd: Path | None = None) -> list[Path]:
    """Where skills are looked for, most specific first.

    A project's own `.agents/skills` beats the user's, so a repository can ship a skill
    that overrides a personal one of the same name — the repository is the more
    specific statement about how work in it should be done.
    """
    paths: list[Path] = []
    for entry in os.environ.get(SKILLS_PATH_ENV, "").split(os.pathsep):
        if entry.strip():
            paths.append(Path(entry.strip()).expanduser())
    paths.append((cwd or Path.cwd()) / PROJECT_SKILLS_DIRNAME)
    paths.append(USER_SKILLS_DIR)
    return paths


def _skill_files(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    for entry in sorted(directory.iterdir()):
        if entry.is_dir():
            for filename in SKILL_FILENAMES:
                candidate = entry / filename
                if candidate.is_file():
                    yield candidate
                    break
        elif entry.suffix == ".md" and entry.name not in SKILL_FILENAMES:
            # A bare `<name>.md` is a skill with no bundled files. Common enough for
            # short, self-contained instructions that rejecting it would be pedantry.
            yield entry


class SkillRegistry:
    """The skills available this session, indexed by name.

    Discovery is frontmatter-only and happens once at startup: it stats a handful of
    directories and reads a few hundred bytes from each `SKILL.md`. Bodies are read
    lazily, so the cost of having fifty skills installed and using none is negligible.
    """

    def __init__(self, skills: dict[str, Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = skills or {}
        self.problems: list[str] = []
        """Skills that were found but could not be read. Surfaced rather than swallowed —
        a skill that silently fails to load looks identical to one that was never
        written, and the user has no way to tell which."""

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def add(self, skill: Skill) -> None:
        # First writer wins: `skill_search_paths` is ordered most-specific-first, so a
        # project skill must not be replaced by the user-global one behind it.
        self._skills.setdefault(skill.name, skill)

    def catalogue(self) -> str:
        """The listing that goes in the system prompt — names and descriptions only."""
        if not self._skills:
            return ""
        return "\n".join(skill.catalogue_line() for skill in sorted(self._skills.values(), key=lambda s: s.name))

    @classmethod
    def discover(cls, cwd: Path | None = None, paths: list[Path] | None = None) -> "SkillRegistry":
        registry = cls()
        project_dir = (cwd or Path.cwd()) / PROJECT_SKILLS_DIRNAME
        for directory in paths or skill_search_paths(cwd):
            # Compared as a path, not a substring: `~/.agents/skills` also *contains*
            # ".agents/skills", so a substring test labels every user skill "project".
            source = "project" if directory == project_dir else "user"
            for path in _skill_files(directory):
                try:
                    metadata, _ = parse_skill_file(path)
                except OSError as exc:
                    registry.problems.append(f"{path}: {exc.strerror}")
                    continue

                name = str(metadata.get("name") or path.parent.name if path.name in SKILL_FILENAMES else path.stem)
                description = str(metadata.get("description") or "")
                if not description:
                    registry.problems.append(f"{path}: no `description` in its frontmatter, skipped")
                    continue
                registry.add(
                    Skill(name=name, description=description, path=path, source=source, metadata=metadata)
                )
        return registry


__all__ = [
    "MAX_SKILL_BYTES",
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

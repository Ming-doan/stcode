"""File discovery preserves names users can select and open."""

import subprocess
from pathlib import Path

from stcode.core.daemon.workspace import list_files


def test_git_discovery_preserves_unicode_and_quoted_filenames(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "config", "core.quotePath", "true"], cwd=tmp_path, check=True
    )
    tracked = "tài-liệu/日本語 🐍.md"
    paths = [
        tracked, "café.py", 'a"b.txt', "tab\tname.txt", "line\nbreak.txt",
        "carriage\rreturn.txt", r"literal\u1234.txt", " spaced .txt",
    ]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    subprocess.run(["git", "add", "--", tracked], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "ẩn.txt").touch()

    assert list_files(tmp_path) == sorted([*paths, ".gitignore"])
    assert list_files(tmp_path, limit=2) == sorted([*paths, ".gitignore"])[:2]


def test_discovery_outside_git_preserves_unicode(tmp_path: Path) -> None:
    (tmp_path / "hướng-dẫn.md").touch()
    assert list_files(tmp_path) == ["hướng-dẫn.md"]

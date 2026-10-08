"""Protect the client boundaries without starting a terminal or daemon."""

import ast
from pathlib import Path
import subprocess
import sys

CLI = Path(__file__).resolve().parents[2] / "stcode" / "cli"


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                package = ["stcode", *path.relative_to(CLI.parent).parts[:-1]]
                module = ".".join(
                    package[: len(package) - node.level + 1]
                    + ([module] if module else [])
                )
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def test_logic_and_services_do_not_depend_on_the_terminal() -> None:
    for layer in ("logic", "services"):
        paths = list((CLI / layer).rglob("*.py"))
        assert paths, f"missing {layer} package"
        forbidden = ("textual", "rich", "stcode.cli.ui", "stcode.cli.bootstrap")
        if layer == "services":
            forbidden += ("stcode.cli.logic",)
        for path in paths:
            assert not any(
                name == prefix or name.startswith(prefix + ".")
                for name in imports(path)
                for prefix in forbidden
            ), path


def test_cli_and_core_cannot_import_each_other() -> None:
    for root, forbidden in ((CLI, "stcode.core"), (CLI.parent / "core", "stcode.cli")):
        for path in root.rglob("*.py"):
            assert not any(
                name == forbidden or name.startswith(forbidden + ".")
                for name in imports(path)
            ), path


def test_noninteractive_commands_do_not_load_textual() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import stcode.cli.main; assert 'textual' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

"""Human output in a pipe: content-sized, no trailing whitespace, nothing cut.

A console that is not a terminal has no width (``output._UNBOUNDED_WIDTH``),
so no line is wrapped or cut there. That must never turn into a width that a
renderer pads or stretches to: ``template list`` once padded every row to
100000 columns in a pipe. These tests run every human-output command with
stdout and stderr as pipes, through the installed console script (what a
user's pipe sees) and in-process (the renderers alone, without the console
script's line-end trim), and check every line.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from treebox import output
from treebox.cli import app

# Far above any real line (a --print command with a long path is a few
# hundred columns), far below a line padded or stretched to the unbounded width.
MAX_LINE = 1000
TREEBOX = Path(sys.executable).with_name("treebox")
# A template whose name and status detail are long: its STATUS cell is the
# widest cell of the table, and off a terminal it is never cut.
LONG_TEMPLATE = "a-template-name-that-is-long-enough-to-matter-in-a-table"
LONG_STATUS = "○ missing: Dockerfile, post-create.sh; invalid: container.json"
# Environment that makes Rich or Typer treat a pipe as a terminal.
FORCED_TERMINAL = (
    "FORCE_COLOR",
    "PY_COLORS",
    "CLICOLOR_FORCE",
    "GITHUB_ACTIONS",
    "TTY_COMPATIBLE",
    "TTY_INTERACTIVE",
    "TERMINAL_WIDTH",
)


def bad_lines(text: str) -> list[str]:
    """The lines that are too long or end in whitespace (shortened for the report)."""
    return [
        f"line {n} ({len(line)} columns): {line[:120]!r}"
        for n, line in enumerate(text.split("\n"), 1)
        if len(line) > MAX_LINE or line != line.rstrip()
    ]


@pytest.fixture
def long_root(tmp_path: Path) -> Path:
    """A worktree root whose path is longer than any terminal is wide."""
    return tmp_path / ("a-long-directory-name-for-the-worktree-root-" * 3) / "wts"


@pytest.fixture
def piped_env(monkeypatch: pytest.MonkeyPatch, isolated_treebox_home: Path) -> None:
    """A narrow COLUMNS and NO_COLOR, as in the reported reproduction, and a
    broken template with a long name and a long status."""
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setenv("NO_COLOR", "1")
    for name in FORCED_TERMINAL:
        monkeypatch.delenv(name, raising=False)
    template = isolated_treebox_home / "templates" / LONG_TEMPLATE
    assert CliRunner().invoke(app, ["template", "init", LONG_TEMPLATE]).exit_code == 0
    (template / "Dockerfile").unlink()
    (template / "post-create.sh").unlink()
    (template / "container.json").write_text("{bad")


def commands(repo: Path, root: Path) -> list[list[str]]:
    """Every human-output command, in an order that leaves each one work to do."""
    base = ["--repo", str(repo), "--root", str(root)]
    return [
        ["create", "piped", *base, "--print"],
        ["create", "planned", *base, "--dry-run"],
        ["enter", "piped", *base, "--print"],
        ["list", *base],
        ["template", "list"],
        ["doctor", "--repo", str(repo)],
        ["teardown", "piped", *base, "--force"],
        ["version"],
        ["--help"],
        ["create", "--help"],
        ["template", "--help"],
    ]


def check_known_long_values(runs: dict[str, tuple[str, str]], root: Path) -> list[str]:
    """Each known long value is whole, on one line, where its command shows it."""
    problems = []

    def lines(key: str) -> list[str]:
        out, err = runs[key]
        return (out + err).splitlines()

    if f"  Root: {root}" not in runs["list"][0].splitlines():
        problems.append("list: the root footer is not one line with the whole root")
    for key, path in (
        ("create planned", root / "planned"),
        ("create piped", root / "piped"),
        ("enter piped", root / "piped"),
    ):
        if not any(str(path) in line for line in lines(key)):
            problems.append(f"{key}: no line holds the whole worktree path")
    for key in ("create piped", "enter piped"):
        if len(runs[key][0].splitlines()) != 1:
            problems.append(f"{key}: the entry command is not one line")
    if not any(LONG_TEMPLATE in line and LONG_STATUS in line for line in lines("template list")):
        problems.append("template list: the long template row is wrapped or cut")
    return problems


def report(runs: dict[str, tuple[str, str]], root: Path) -> list[str]:
    problems = [
        f"{key} {stream}: {bad}"
        for key, (out, err) in runs.items()
        for stream, text in (("stdout", out), ("stderr", err))
        for bad in bad_lines(text)
    ]
    return problems + check_known_long_values(runs, root)


def key_of(argv: list[str]) -> str:
    return " ".join(a for a in argv[:2] if not a.startswith("/") and a != "--repo")


@pytest.mark.usefixtures("piped_env", "agent_ready", "hermetic_config")
def test_every_human_command_in_a_pipe(repo: Path, long_root: Path) -> None:
    """The console script, stdout and stderr both pipes: what a user's
    ``treebox … | less`` or ``2>log`` gets."""
    assert TREEBOX.is_file(), f"no console script at {TREEBOX}"
    runs: dict[str, tuple[str, str]] = {}
    for argv in commands(repo, long_root):
        proc = subprocess.run(
            [str(TREEBOX), *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,  # stdout and stderr are both pipes
            text=True,
            env=dict(os.environ),
        )
        assert proc.returncode == 0, f"{argv}: exit {proc.returncode}\n{proc.stderr}"
        runs[key_of(argv)] = (proc.stdout, proc.stderr)
    assert report(runs, long_root) == []


@pytest.mark.usefixtures("piped_env", "agent_ready", "hermetic_config")
def test_renderers_pad_nothing_off_a_terminal(repo: Path, long_root: Path) -> None:
    """treebox's own renderers, without the console script's line-end trim
    (``output.trim_line_ends``): a renderer must never pad or stretch to the
    unbounded width. Typer's help is not treebox's renderer, so it is left out."""
    runner = CliRunner()
    runs: dict[str, tuple[str, str]] = {}
    for argv in commands(repo, long_root):
        if "--help" in argv:
            continue
        res = runner.invoke(app, argv, catch_exceptions=False)
        assert res.exit_code == 0, f"{argv}: exit {res.exit_code}\n{res.output}"
        runs[key_of(argv)] = (res.stdout, res.stderr)
    assert report(runs, long_root) == []


def test_fit_width_is_none_only_for_an_unbounded_console() -> None:
    piped = output.new_console(file=io.StringIO())
    assert output.fit_width(piped) is None
    terminal = Console(file=io.StringIO(), force_terminal=True, width=72)
    assert output.fit_width(terminal) == 72
    # A console given a width (a test's stand-in for a terminal) is fitted.
    assert output.fit_width(Console(file=io.StringIO(), width=60)) == 60


def test_line_end_trimmer_trims_whole_lines_and_keeps_a_prompt() -> None:
    sink = io.StringIO()
    trimmer = output._LineEndTrimmer(sink)
    trimmer.write("a row   \nanother")
    trimmer.write(" row \t\n\n")
    assert sink.getvalue() == "a row\nanother row\n\n"
    trimmer.write("Proceed? [y/N] ")
    trimmer.flush()
    assert sink.getvalue().endswith("\nProceed? [y/N] ")

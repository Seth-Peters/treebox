"""The verify-treebox fixture cannot be lost silently.

A fixture sourced where its environment dies (a pipeline, a subshell, an
executed script, a shell that is not bash), or a new shell that did not
attach, leaves ``treebox`` resolving to the release on PATH with the real
HOME. ``fixture.sh`` refuses the wrong invocations loudly and builds
nothing; ``e2e_guard`` (``guard.sh``) fails outside a fixture and passes
inside one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

CHECKOUT = Path(__file__).resolve().parents[1]
SKILL = CHECKOUT / ".claude" / "skills" / "verify-treebox"
FIXTURE = SKILL / "fixture.sh"
GUARD = SKILL / "guard.sh"
USE = "source .claude/skills/verify-treebox/fixture.sh"


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    """No fixture variables from the caller; a scratch TMPDIR, so a built
    fixture would show (and be removed with the test's tmp_path)."""
    env = {k: v for k, v in os.environ.items() if k not in ("SBX", "TREEBOX_CONFIG", "E2E_GUARD")}
    env["TMPDIR"] = str(tmp)
    env.update(extra)
    return env


def _sh(shell: str, script: str, tmp: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shell, "-c", script],
        cwd=CHECKOUT,
        env=_env(tmp),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _built(tmp: Path) -> list[str]:
    return sorted(p.name for p in tmp.glob("treebox-e2e.*"))


@pytest.mark.parametrize(
    ("script", "where"),
    [
        (f"bash {FIXTURE}", "executed, not sourced"),
        (f"source {FIXTURE} | cat; exit ${{PIPESTATUS[0]}}", "sourced in a subshell"),
        (f"source {FIXTURE} 2>&1 | tail -1 >&2; exit ${{PIPESTATUS[0]}}", "sourced in a subshell"),
        (f"( source {FIXTURE} )", "sourced in a subshell"),
        (f'out="$(source {FIXTURE})"', "sourced in a subshell"),
        (f"source {FIXTURE} --attach /nonexistent | cat; exit ${{PIPESTATUS[0]}}", "subshell"),
    ],
    ids=["executed", "pipeline", "pipeline-stderr", "subshell", "command-substitution", "attach"],
)
def test_fixture_refuses_where_its_environment_dies(script: str, where: str, tmp_path: Path):
    res = _sh("bash", script, tmp_path)
    assert res.returncode == 2, res.stderr
    assert "FIXTURE NOT BUILT" in res.stderr
    assert where in res.stderr
    assert USE in res.stderr  # the correct invocation
    assert _built(tmp_path) == []


@pytest.mark.parametrize("shell", ["zsh", "dash"])
def test_fixture_refuses_a_shell_that_is_not_bash(shell: str, tmp_path: Path):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} is not installed")
    res = _sh(shell, f". {FIXTURE}", tmp_path)
    assert res.returncode == 2, res.stderr
    assert "FIXTURE NOT BUILT: this shell is not bash" in res.stderr
    assert USE in res.stderr
    assert _built(tmp_path) == []


@pytest.mark.parametrize(
    "script",
    [
        "e2e_guard",  # a new shell: the function does not exist
        str(GUARD),  # no SBX
        f"SBX=/nonexistent {GUARD}",  # no fixture there
    ],
    ids=["new-shell", "no-sbx", "no-fixture"],
)
def test_guard_fails_outside_a_fixture(script: str, tmp_path: Path):
    res = _sh("bash", script, tmp_path)
    assert res.returncode != 0
    if script != "e2e_guard":
        assert "FIXTURE LOST" in res.stderr
        assert "--attach" in res.stderr


def test_guard_passes_inside_a_fixture_and_fails_when_it_is_lost(tmp_path: Path):
    """One real fixture: the guard passes in its shell and in a script
    started from it, and fails for a shadowed ``treebox``, a config outside
    the fixture, a new shell that did not attach, and after cleanup."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "treebox").write_text("#!/bin/sh\n")
    (shadow / "treebox").chmod(0o755)
    script = f"""
source {FIXTURE} 2>/dev/null || exit 90
trap 'e2e_cleanup >/dev/null 2>&1' EXIT
e2e_guard 2>/dev/null || exit 91
bash -c 'e2e_guard' 2>/dev/null || exit 92
PATH="{shadow}:$PATH" e2e_guard 2>/dev/null && exit 93
TREEBOX_CONFIG=/elsewhere.toml e2e_guard 2>/dev/null && exit 94
env -i HOME="$HOME" PATH=/usr/bin:/bin bash -c '{GUARD}' 2>/dev/null && exit 95
env -i HOME="$HOME" PATH=/usr/bin:/bin bash -c \
  "cd {CHECKOUT} && source {FIXTURE} --attach $SBX 2>/dev/null && e2e_guard 2>/dev/null" || exit 96
e2e_doctor > "$SBX/../doctor.out"
grep -q '^ok    e2e_guard' "$SBX/../doctor.out" || exit 97
e2e_cleanup >/dev/null 2>&1
e2e_guard 2>/dev/null && exit 98
exit 0
"""
    res = _sh("bash", script, tmp_path)
    assert res.returncode == 0, f"step {res.returncode - 90} failed\n{res.stdout}\n{res.stderr}"
    assert _built(tmp_path) == []  # e2e_cleanup removed it

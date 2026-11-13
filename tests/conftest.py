"""Shared fixtures: a real git repo wired to a local 'origin' with a submodule.

Everything is local (no network): a bare origin, a working clone, and a bare
submodule. Setup is made hermetic with a marker-writing setup hook via config,
so tests never need uv/npm to exercise the create/enter/lockfile-hash flow.
The developer's global/system gitconfig is also hidden from every git
subprocess (see ``isolated_git_config`` below), so host settings can't
change test behavior.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's global/system gitconfig from every git subprocess.

    The suite runs the real git binary, and git reads ``~/.gitconfig`` and
    ``/etc/gitconfig`` by default — so a developer's ``init.templateDir``,
    ``core.hooksPath``, ``fsmonitor``, etc. could silently change test
    behavior. ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` (honored by
    git >= 2.32; ignored, i.e. today's behavior, on older git) point both
    lookups at /dev/null for the duration of each test. Test-process only —
    never affects treebox itself or anyone's normal git usage.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture(autouse=True)
def scratch_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at an empty scratch dir for every test.

    treebox reads the agent logins from ``~/.claude`` / ``~/.codex`` (and, on
    macOS, probes the Keychain, which follows HOME). No test may read or
    write the developer's real logins, and no test may pass or fail because
    of them. Tests that need a logged-in agent use ``agent_ready``.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def agent_ready(scratch_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A logged-in agent for launch and doctor tests: synthetic login files in
    the scratch HOME and a stub ``claude`` / ``codex`` first on PATH (it
    prints its args and exits 0; never a real agent). Returns the stub dir,
    so a test can replace a stub with its own script."""
    (scratch_home / ".claude").mkdir()
    (scratch_home / ".claude" / ".credentials.json").write_text('{"token": "test"}\n')
    (scratch_home / ".codex").mkdir()
    (scratch_home / ".codex" / "auth.json").write_text('{"token": "test"}\n')
    stubs = tmp_path / "agent-stubs"
    stubs.mkdir()
    for name in ("claude", "codex"):
        stub = stubs / name
        stub.write_text('#!/bin/sh\necho "stub agent: $*"\n')
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    return stubs


def _git(*args: str, cwd: Path) -> str:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@e",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@e",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A working clone of a local origin, with a uv manifest, .env, and submodule."""
    origin = tmp_path / "origin.git"
    sub_origin = tmp_path / "sub.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(sub_origin)], check=True)

    # Build the submodule's content first.
    subwork = tmp_path / "subwork"
    subwork.mkdir()
    _git("init", "-b", "main", cwd=subwork)
    (subwork / "lib.txt").write_text("hello from submodule\n")
    _git("add", "-A", cwd=subwork)
    _git("commit", "-m", "sub", cwd=subwork)
    _git("remote", "add", "origin", str(sub_origin), cwd=subwork)
    _git("push", "origin", "main", cwd=subwork)

    work = tmp_path / "repo"
    _git("clone", str(origin), str(work), cwd=tmp_path)
    (work / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    (work / "uv.lock").write_text("version = 1\n")
    (work / ".env").write_text("SECRET=canonical\n")
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(sub_origin),
        "sub",
        cwd=work,
    )
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "init", cwd=work)
    _git("push", "origin", "main", cwd=work)
    # A second base branch for --base resolution tests.
    _git("branch", "dev", cwd=work)
    _git("push", "origin", "dev", cwd=work)
    return work


@pytest.fixture
def hermetic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config at a TOML whose setup hook just appends a marker line."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('setup_hook = ["echo ran >> setup.log"]\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    return cfg

"""The TREEBOX_* variables that setup and the agent entry get (issue #66).

Driven through the CLI against a real local repo. The replay tests run the
printed / --json entry command for real, with a stub ``claude`` first on PATH
that prints the TREEBOX_* variables it sees - never the real agent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox.cli import app
from treebox.config import Config
from treebox.harnesses import get_harness
from treebox.models import WORKTREE_ID_LENGTH, Worktree, worktree_id
from treebox.runners.docker import DockerRunner

runner = CliRunner()

ID_RE = re.compile(rf"^[0-9a-f]{{{WORKTREE_ID_LENGTH}}}$")


def _run(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


@pytest.fixture
def stub_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A harmless ``claude`` first on PATH: it prints its TREEBOX_* variables."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text('#!/bin/sh\necho "pwd=$(pwd)"\nenv | grep "^TREEBOX_" | sort\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return stub


@pytest.fixture
def env_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config whose setup hook records the TREEBOX_* variables setup sees."""
    out = tmp_path / "setup-env.txt"
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"setup_hook = [\"env | grep '^TREEBOX_' | sort > {out}\"]\n")
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    return out


def _treebox_vars(text: str) -> dict[str, str]:
    """The TREEBOX_* lines of ``env`` output, minus the operator's own config
    variables (TREEBOX_HOME, TREEBOX_CONFIG), which are inherited, not exported."""
    found = dict(line.split("=", 1) for line in text.splitlines() if line.startswith("TREEBOX_"))
    return {k: v for k, v in found.items() if k not in {"TREEBOX_HOME", "TREEBOX_CONFIG"}}


def _replay(entry_command: list[str]) -> str:
    """Run an emitted entry command for real, from an unrelated directory."""
    proc = subprocess.run(entry_command, cwd="/", capture_output=True, text=True, check=True)
    return proc.stdout


def test_setup_print_json_enter_and_list_expose_the_same_identity(
    repo: Path, tmp_path: Path, env_hook: Path, stub_agent: Path
) -> None:
    root = str(tmp_path / "wts")
    printed = _run(["create", "svc", "--repo", str(repo), "--root", root, "--print"])
    assert printed.exit_code == 0, printed.output
    wt_path = Path(root) / "svc"
    expected = {
        "TREEBOX_NAME": "svc",
        "TREEBOX_ID": worktree_id(wt_path),
        # The first block of the default range (an empty ledger).
        "TREEBOX_PORT_BASE": "20000",
        "TREEBOX_PORT_COUNT": "10",
    }
    assert ID_RE.match(expected["TREEBOX_ID"])

    # Setup saw them.
    assert _treebox_vars(env_hook.read_text()) == expected

    # The printed command, replayed through a shell, gives them to the agent.
    out = subprocess.run(
        printed.stdout, shell=True, cwd="/", capture_output=True, text=True, check=True
    ).stdout
    assert f"pwd={wt_path}" in out
    assert _treebox_vars(out) == expected

    # enter --json: the payload states them and its entry command carries them.
    entered = json.loads(
        _run(["enter", "svc", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    assert entered["id"] == expected["TREEBOX_ID"]
    assert (entered["port_base"], entered["port_count"]) == (20000, 10)
    assert _treebox_vars(_replay(entered["entry_command"])) == expected

    # list --json reports the same values.
    listed = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    (row,) = listed["worktrees"]
    assert row["id"] == expected["TREEBOX_ID"]
    assert (row["port_base"], row["port_count"]) == (20000, 10)


def test_setup_rerun_by_enter_sees_the_same_identity(
    repo: Path, tmp_path: Path, env_hook: Path
) -> None:
    root = str(tmp_path / "wts")
    created = json.loads(
        _run(["create", "svc", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    first = env_hook.read_text()
    env_hook.unlink()
    # A changed lockfile makes enter run setup again.
    (Path(root) / "svc" / "uv.lock").write_text("version = 2\n")
    assert _run(["enter", "svc", "--repo", str(repo), "--root", root, "--json"]).exit_code == 0
    assert env_hook.read_text() == first
    assert _treebox_vars(first)["TREEBOX_ID"] == created["id"]


def test_same_name_in_two_repos_and_nested_roots_gets_distinct_ids(
    repo: Path, tmp_path: Path, hermetic_config: Path
) -> None:
    """A name repeats across repos and roots; the id comes from the path."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    ids = set()
    # The nested root sits in the same repo, where branch "same" now exists:
    # it checks out another branch under the same worktree name.
    for repo_dir, root, extra in (
        (repo, tmp_path / "wts", []),
        (other, tmp_path / "wts-other", []),
        (repo, tmp_path / "wts" / "nested", ["--checkout", "dev"]),
    ):
        args = ["create", "same", *extra, "--repo", str(repo_dir), "--root", str(root), "--json"]
        res = _run(args)
        assert res.exit_code == 0, res.output
        payload = json.loads(res.stdout)
        assert ID_RE.match(payload["id"])
        ids.add(payload["id"])
    assert len(ids) == 3


def test_docker_exec_commands_carry_the_identity(tmp_path: Path) -> None:
    """The docker entry command and the post-create exec pass the variables
    with ``-e`` on every exec (so a restarted container gets them too); the
    root-run firewall exec does not need them."""
    wt = Worktree("/repo", "svc", "svc", "main", tmp_path / "svc")
    docker = DockerRunner(Config(isolation="docker"))
    flags = ["-e", "TREEBOX_NAME=svc", "-e", f"TREEBOX_ID={wt.id}"]

    entry = docker.entry_command(wt, harness=get_harness("claude"), args=[])
    assert entry[2:6] == flags or entry[3:7] == flags  # after -i (and -t on a TTY)
    post_create = docker._exec_command(wt, ["true"], treebox_env=True)
    assert post_create[2:6] == flags
    assert "TREEBOX_NAME=svc" not in docker._firewall_command(wt)


def test_docker_sandbox_name_embeds_the_worktree_id(tmp_path: Path) -> None:
    """One derivation: the container/image name ends in TREEBOX_ID."""
    wt = Worktree("/repo", "svc", "svc", "main", tmp_path / "svc")
    assert DockerRunner(Config(isolation="docker"))._slug(wt) == f"treebox-svc-{wt.id}"


def test_list_reports_the_id_setup_and_agent_get_under_a_root_alias(
    repo: Path, tmp_path: Path, hermetic_config: Path
) -> None:
    """git canonicalizes a root reached through a symlink (like macOS /var ->
    /private/var); list must still show the id create and enter export."""
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    root = str(tmp_path / "alias" / "wts")
    created = json.loads(
        _run(["create", "svc", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    entered = json.loads(
        _run(["enter", "svc", "--repo", str(repo), "--root", root, "--json"]).stdout
    )
    listed = json.loads(_run(["list", "--repo", str(repo), "--root", root, "--json"]).stdout)
    (row,) = listed["worktrees"]
    assert row["path"] != created["worktree_path"]  # git stored the resolved spelling
    assert created["id"] == entered["id"] == row["id"]

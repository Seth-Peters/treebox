"""Run firewall recovery commands through the CLI against disposable Git repositories."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from treebox import assets, git, record
from treebox.config import Config
from treebox.models import Worktree
from treebox.runners.docker import DockerRunner


@pytest.fixture
def docker_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["TEST_DOCKER_STATE"])
state = json.loads(path.read_text()) if path.exists() else {}
args = sys.argv[1:]
command = args[0]
if command == "ps" and state.get("name"):
    print("fixture-container")
elif command == "inspect":
    if args[-1] == "{{.Config.Image}}":
        print(state["name"])
    elif ".State.Running" in args[-1]:
        print("/" + state["name"])
        print("true")
        if state["firewall"]:
            print("TREEBOX_FIREWALL=1")
elif command == "run":
    state["name"] = args[args.index("--name") + 1]
    state["firewall"] = "TREEBOX_FIREWALL=1" in args
    path.write_text(json.dumps(state))
    print("fixture-container")
elif command == "exec" and "init-firewall.sh" in " ".join(args):
    if path.with_suffix(".fail").exists():
        print("ERROR: fixture firewall failure", file=sys.stderr)
        sys.exit(1)
elif command == "rm":
    state.pop("name", None)
    path.write_text(json.dumps(state))
elif command not in {"info", "build", "exec", "rmi", "ps", "image", "volume"}:
    sys.exit("Unexpected Docker command: " + repr(args))
"""
    )
    docker.chmod(0o755)
    docker_state = tmp_path / "docker.json"
    monkeypatch.setenv("TEST_DOCKER_STATE", str(docker_state))
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    return docker_state


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from treebox.cli import app; app()", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize("firewall", [False, True])
@pytest.mark.parametrize(
    ("create_args", "branch", "renamed", "recoverable"),
    [
        (["fix-auth"], "fix-auth", None, True),
        (["feature/auth"], "feature/auth", None, True),
        (["feature/auth"], "feature/auth", "fix/renamed'auth", True),
        (["--checkout", "Feature/Auth"], "Feature/Auth", None, True),
        (["custom-name", "--checkout", "Feature/Auth"], "Feature/Auth", None, True),
        (["--checkout", "Feature/Auth"], "Feature/Auth", "feature/auth", False),
        (["--checkout", "treebox/-auth"], "treebox/-auth", "feature/auth", True),
    ],
)
def test_firewall_recovery_restores_existing_work_and_choices(
    repo: Path,
    tmp_path: Path,
    hermetic_config: Path,
    docker_cli: Path,
    monkeypatch: pytest.MonkeyPatch,
    firewall: bool,
    create_args: list[str],
    branch: str,
    renamed: str | None,
    recoverable: bool,
):
    home = tmp_path / "home"
    template = home / "templates" / "locked"
    shutil.copytree(assets.template_dir("default"), template)
    container_config = json.loads((template / "container.json").read_text())
    container_config["env"]["RECOVERY_TEMPLATE"] = "locked"
    (template / "container.json").write_text(json.dumps(container_config))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TREEBOX_HOME", str(home))
    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    root = tmp_path / "worktrees with spaces"
    location = ["--repo", str(repo), "--root", str(root)]
    if "--checkout" in create_args:
        _git(repo, "branch", branch)
    created = _run(
        [
            "create",
            *create_args,
            *location,
            "--isolation",
            "docker",
            "--template",
            "locked",
            "--harness",
            "codex",
            "--firewall" if firewall else "--no-firewall",
            "--json",
        ],
        tmp_path,
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    wt = Path(payload["worktree_path"])
    (wt / "work.txt").write_text("existing work must survive\n")
    _git(wt, "add", "work.txt")
    _git(wt, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-m", "existing work")
    commit = _git(wt, "rev-parse", "HEAD")
    if renamed:
        _git(wt, "branch", "-m", "temporary-recovery")
        _git(wt, "branch", "-m", renamed)
    expected_branch = renamed or branch
    if not recoverable:
        (wt / "uncommitted.txt").write_text("uncommitted work must survive\n")
    hermetic_config.write_text(
        f'firewall = {"false" if firewall else "true"}\ntemplate = "default"\nharness = "claude"\n'
    )
    failure = docker_cli.with_suffix(".fail")
    failure.touch()
    recorded_before = record.load(wt)
    container_before = docker_cli.read_text()

    entered = _run(["enter", *location, "--json", "--", wt.name], tmp_path)

    assert entered.returncode == 1, entered.stderr
    assert entered.stdout == ""
    error = json.loads(entered.stderr)["error"]
    assert error["code"] == "FIREWALL_FAILED"
    if not recoverable:
        assert "Keep the worktree and its data intact" in error["hint"]
        assert str(wt) in error["hint"]
        assert expected_branch in error["hint"]
        assert "treebox teardown" not in error["hint"]
        assert "Then re-create:" not in error["hint"]
        template_command = error["hint"].split("(", 1)[1].split(")", 1)[0]
        inspected = _run(shlex.split(template_command)[1:], tmp_path)
        assert inspected.returncode == 0, inspected.stderr
        assert inspected.stdout.strip() == str(template)
        assert _git(wt, "branch", "--show-current") == expected_branch
        assert _git(wt, "rev-parse", "HEAD") == commit
        assert (wt / "work.txt").read_text() == "existing work must survive\n"
        assert (wt / "uncommitted.txt").read_text() == "uncommitted work must survive\n"
        assert record.load(wt) == recorded_before
        assert docker_cli.read_text() == container_before
        return
    recovery = error["hint"].split("re-create: ", 1)[1]
    teardown, create = [shlex.split(command) for command in recovery.split(" && ")]
    assert teardown[0] == create[0] == "treebox"
    failure.unlink()

    removed = _run([teardown[1], "--force", "--json", *teardown[2:]], tmp_path)
    assert removed.returncode == 0, removed.stderr
    assert not wt.exists()
    restored = _run([create[1], "--json", *create[2:]], tmp_path)

    assert restored.returncode == 0, restored.stderr
    recovered = json.loads(restored.stdout)
    assert recovered["branch"] == expected_branch
    assert Path(recovered["worktree_path"]) == wt
    assert _git(wt, "branch", "--show-current") == expected_branch
    assert _git(wt, "rev-parse", "HEAD") == commit
    assert (wt / "work.txt").read_text() == "existing work must survive\n"
    recorded = record.load(wt)
    assert recorded.template == "locked"
    assert recorded.firewall is firewall
    assert recorded.harness == "codex"
    rendered = json.loads((wt.parent / ".containers" / wt.name / "container.json").read_text())
    assert rendered["env"]["RECOVERY_TEMPLATE"] == "locked"
    assert rendered["env"].get("TREEBOX_FIREWALL") == ("1" if firewall else None)


def test_firewall_recovery_does_not_teardown_detached_work(repo: Path, tmp_path: Path):
    wt = tmp_path / "detached"
    _git(repo, "worktree", "add", "--detach", str(wt), "HEAD")
    error = DockerRunner(Config(isolation="docker", firewall=True))._firewall_error(
        Worktree.at(str(repo), wt, branch="old-branch"), "ERROR: fixture firewall failure"
    )

    assert str(error) == (
        "Firewall initialization failed for worktree 'detached': ERROR: fixture firewall failure"
    )
    assert error.hint is not None
    assert "Save current work on a branch" in error.hint
    assert "treebox teardown" not in error.hint


def test_firewall_failure_survives_unavailable_recovery_branch(repo: Path, monkeypatch):
    def unavailable(*args):
        raise git.GitError("fixture Git failure")

    monkeypatch.setattr(git, "branch_for_path", unavailable)
    error = DockerRunner(Config(isolation="docker", firewall=True))._firewall_error(
        Worktree.at(str(repo), repo), "ERROR: fixture firewall failure"
    )

    assert str(error) == (
        "Firewall initialization failed for worktree 'repo': ERROR: fixture firewall failure"
    )
    assert error.hint is not None
    assert "Save current work on a branch" in error.hint
    assert "treebox teardown" not in error.hint

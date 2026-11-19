"""The docker runner checks that the engine enforces the read-only shared
``hooks/`` mount, and fails closed before any setup runs when it does not."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from treebox import git

_DOCKER = """
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["TEST_DOCKER_STATE"])
args = sys.argv[1:]
with (state / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps(args) + "\\n")
name = state / "name"
if args[0] == "ps" and name.exists():
    print("fixture-container")
elif args[0] == "inspect" and ".State.Running" in args[-1]:
    print("/" + name.read_text())
    print("true")
elif args[0] == "run":
    name.write_text(args[args.index("--name") + 1])
elif args[0] == "exec" and "treebox-readonly-probe" in " ".join(args):
    probe = state / "probe-exit"
    if probe.exists():
        print("fixture engine message", file=sys.stderr)
        sys.exit(int(probe.read_text()))
"""


@pytest.fixture
def docker_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(f"#!{sys.executable}\n{_DOCKER}")
    docker.chmod(0o755)
    state = tmp_path / "docker-state"
    state.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TEST_DOCKER_STATE", str(state))
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TREEBOX_HOME", str(home))
    monkeypatch.delenv("TREEBOX_TEMPLATE_DIR", raising=False)
    return state


def _treebox(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from treebox.cli import app; app()", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _calls(state: Path) -> list[list[str]]:
    return [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines()]


def _create(
    repo: Path, tmp_path: Path, command: str = "create"
) -> subprocess.CompletedProcess[str]:
    location = ["--repo", str(repo), "--root", str(tmp_path / "wts")]
    extra = ["--isolation", "docker", "--no-firewall"] if command == "create" else []
    return _treebox([command, "boxed", *location, *extra, "--json"], tmp_path)


def test_hooks_are_probed_as_root_before_post_create(
    repo: Path, tmp_path: Path, hermetic_config: Path, docker_state: Path
):
    created = _create(repo, tmp_path)

    assert created.returncode == 0, created.stderr
    execs = [call for call in _calls(docker_state) if call[0] == "exec"]
    probe = execs[0]
    assert "treebox-readonly-probe" in " ".join(probe)
    assert probe[probe.index("-u") + 1] == "root"
    assert probe[-1] == str(git.common_dir(str(repo)) / "hooks")
    assert len(execs) > 1, "post-create runs after the probe"


def test_an_engine_that_ignores_readonly_fails_closed(
    repo: Path, tmp_path: Path, hermetic_config: Path, docker_state: Path
):
    (docker_state / "probe-exit").write_text("73")

    created = _create(repo, tmp_path)

    assert created.returncode == 1
    assert created.stdout == ""
    error = json.loads(created.stderr)["error"]
    assert error["code"] == "SANDBOX_FAILED"
    assert "did not enforce the read-only mount" in error["message"]
    assert "treebox teardown" in error["hint"]
    # Nothing ran in the sandbox after the probe: no post-create.
    execs = [call for call in _calls(docker_state) if call[0] == "exec"]
    assert len(execs) == 1

    # Setup did not finish, so entering runs it (and the probe) again.
    entered = _create(repo, tmp_path, "enter")
    assert entered.returncode == 1
    assert json.loads(entered.stderr)["error"]["code"] == "SANDBOX_FAILED"
    assert len([call for call in _calls(docker_state) if call[0] == "exec"]) == 2


def test_a_probe_that_cannot_run_is_not_a_pass(
    repo: Path, tmp_path: Path, hermetic_config: Path, docker_state: Path
):
    (docker_state / "probe-exit").write_text("126")

    created = _create(repo, tmp_path)

    assert created.returncode == 1
    error = json.loads(created.stderr)["error"]
    assert error["code"] == "SANDBOX_FAILED"
    assert "Could not confirm" in error["message"]
    assert "fixture engine message" in error["message"]

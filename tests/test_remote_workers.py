"""The remote-worker contract (issue #68): what a coordinator relies on.

A coordinator drives treebox over SSH and must be able to retry any request
after a lost reply. These tests pin the two pieces of that contract treebox
itself owns:

- an absolute worktree path as the ref of ``enter`` / ``teardown`` matches
  that one worktree exactly, never another one by name, branch or substring,
  so a retried request can never act on another task;
- a docker sandbox that stops or restarts while the agent runs is a treebox
  failure, never reported as the agent's exit status.

The repo-scoped git token needs no treebox code: it is operator template
configuration (docs/remote-workers.md), proven on a real worker in the PR.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox.cli import app
from treebox.config import Config
from treebox.models import Worktree
from treebox.runners.docker import DockerRunner

runner = CliRunner()

_TOKEN = "fake-token-6f1d2c"  # a fake: tests never hold a real credential


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "wts")


@pytest.fixture
def clean_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A setup that leaves the tree clean, so `teardown --yes` may remove it
    (a task that committed and pushed its work leaves no changes)."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('setup_hook = ["true"]\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))


def _run(args: list[str]):
    return runner.invoke(app, ["--non-interactive", *args], catch_exceptions=False)


def _create(repo: Path, root: str, name: str) -> str:
    res = _run(["create", name, "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.stderr
    path: str = json.loads(res.stdout)["worktree_path"]
    return path


def _names(repo: Path, root: str) -> list[str]:
    res = _run(["list", "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.stderr
    return sorted(w["name"] for w in json.loads(res.stdout)["worktrees"])


# --- retries address one task exactly ------------------------------------------


def test_retried_teardown_by_name_hits_another_task(repo: Path, root: str, clean_setup):
    """Why the coordinator must not address a task by name: a name ref falls
    back to a unique substring, so a retried teardown of a removed task
    removes the one other task whose name contains it."""
    _create(repo, root, "task-1")
    _create(repo, root, "task-12")
    args = ["--repo", str(repo), "--root", root, "--yes", "--json"]
    assert _run(["teardown", "task-1", *args]).exit_code == 0
    retry = _run(["teardown", "task-1", *args])
    assert retry.exit_code == 0
    assert [w["name"] for w in json.loads(retry.stdout)["worktrees"]] == ["task-12"]


def test_retried_teardown_by_path_never_removes_another_task(repo: Path, root: str, clean_setup):
    first = _create(repo, root, "task-1")
    _create(repo, root, "task-12")
    args = ["--repo", str(repo), "--root", root, "--yes", "--json"]

    done = _run(["teardown", first, *args])
    assert done.exit_code == 0, done.stderr
    assert [w["name"] for w in json.loads(done.stdout)["worktrees"]] == ["task-1"]

    retry = _run(["teardown", first, *args])
    assert retry.exit_code == 3  # NOT_FOUND: already gone, nothing else touched
    assert json.loads(retry.stderr)["error"]["code"] == "NOT_FOUND"
    assert _names(repo, root) == ["task-12"]


def test_path_ref_matches_the_registered_path_exactly(repo: Path, root: str, clean_setup):
    path = _create(repo, root, "task-7")
    _create(repo, root, "task-70")

    res = _run(["enter", path, "--repo", str(repo), "--root", root, "--json"])
    assert res.exit_code == 0, res.stderr
    assert json.loads(res.stdout)["name"] == "task-7"

    # A path that is no registered worktree is NOT_FOUND - never the
    # substring match a name ref would make.
    for ref in (path.rstrip("7"), f"{path}0-missing", str(Path(path).parent)):
        miss = _run(["enter", ref, "--repo", str(repo), "--root", root, "--json"])
        assert miss.exit_code == 3, (ref, miss.stdout)
        assert json.loads(miss.stderr)["error"]["code"] == "NOT_FOUND"


def test_path_ref_finds_a_worktree_whose_directory_is_gone(repo: Path, root: str, clean_setup):
    """A partial cleanup can leave a registration without its directory: the
    same path ref still finds it, so the retried teardown finishes it."""
    path = _create(repo, root, "task-3")
    _create(repo, root, "task-33")
    subprocess.run(["rm", "-rf", path], check=True)

    res = _run(["teardown", path, "--repo", str(repo), "--root", root, "--yes", "--json"])
    assert res.exit_code == 0, res.stderr
    assert [w["name"] for w in json.loads(res.stdout)["worktrees"]] == ["task-3"]
    assert _names(repo, root) == ["task-33"]


# --- a lost sandbox is not an agent result --------------------------------------


@pytest.mark.parametrize(
    ("after", "code", "expected"),
    [
        ("2026-11-13T10:00:00Z", 3, 3),  # same run of the container: the agent's exit
        ("2026-11-13T10:00:00Z", 0, 0),
        ("", 137, None),  # stopped (an engine restart, a worker shutdown)
        ("2026-11-13T10:05:00Z", 137, None),  # restarted (`docker restart`)
    ],
)
def test_docker_launch_reports_a_lost_sandbox_apart_from_the_agent_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: str, code: int, expected: int | None
):
    """`docker exec` ends with a status the agent never returned when its
    container stops or restarts. The agent's status is passed on only when
    the container still runs the same run it ran at launch; otherwise the
    launch is a treebox runtime failure (exit 1, no agent exit line)."""
    from treebox import git, system
    from treebox.harnesses import get_harness
    from treebox.runners import docker as docker_mod

    common = tmp_path / "gitcommon" / ".git"
    common.mkdir(parents=True)
    monkeypatch.setattr(git, "common_dir", lambda repo: common)
    wt = Worktree.at(tmp_path / "repo", tmp_path / "root" / "task-1", branch="task-1", base="main")
    cfg = Config(isolation="docker")
    slug = DockerRunner(cfg)._slug(wt)
    state = {"started": "2026-11-13T10:00:00Z"}

    def engine(args: list[str]) -> subprocess.CompletedProcess[str]:
        out = ""
        if args[0] == "ps":
            out = "abc123\n"
        elif args[0] == "inspect" and "{{.State.StartedAt}}" in args[-1]:
            out = state["started"] + "\n"
        elif args[0] == "inspect":
            out = f"/{slug}\ntrue\n"
        return subprocess.CompletedProcess(["docker", *args], 0, out, "")

    def fake_run_agent(argv, *, cwd=None, stop=None, grace=system.STOP_GRACE_SECONDS):
        state["started"] = after
        return code, None

    monkeypatch.setattr(docker_mod, "run_agent", fake_run_agent)
    run = DockerRunner(cfg, docker=engine)
    if expected is not None:
        assert run.launch(wt, harness=get_harness("claude"), args=[]) == expected
    else:
        with pytest.raises(RuntimeError, match="stopped or restarted while the agent ran"):
            run.launch(wt, harness=get_harness("claude"), args=[])

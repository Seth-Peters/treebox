"""A resolved worktree path must remain the identity through every operation."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import locking, state
from treebox.cli import app


def _run(args: list[str]):
    return CliRunner().invoke(app, args, catch_exceptions=False)


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "wts")


@pytest.fixture
def nested_worktrees(repo: Path, root: str, hermetic_config) -> tuple[Path, Path]:
    """Two Treebox roots can contain the same stable leaf name."""
    subprocess.run(["git", "-C", str(repo), "branch", "nested-branch"], check=True)
    for target_root, checkout in (
        (Path(root), []),
        (Path(root) / "nested", ["--checkout", "nested-branch"]),
    ):
        result = _run(
            ["create", "same", *checkout, "--repo", str(repo), "--root", str(target_root), "--json"]
        )
        assert result.exit_code == 0, result.output
    return Path(root) / "same", Path(root) / "nested" / "same"


def test_enter_nested_root_keeps_resolved_path(repo: Path, root: str, nested_worktrees):
    direct, nested = nested_worktrees
    (repo / ".env").write_text("SECRET=rotated\n")
    before = state.load(direct)
    result = _run(["enter", "nested-branch", "--repo", str(repo), "--root", root, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert Path(payload["worktree_path"]) == nested
    assert payload["branch"] == "nested-branch"
    assert str(nested) in payload["entry_command"][-1]
    assert (nested / ".env").read_text() == "SECRET=rotated\n"
    assert (direct / ".env").read_text() == "SECRET=canonical\n"
    assert state.load(direct) == before


def test_teardown_nested_root_keeps_resolved_path(repo: Path, root: str, nested_worktrees):
    direct, nested = nested_worktrees
    (direct / "keep.txt").write_text("uncommitted user work\n")
    result = _run(
        ["teardown", "nested-branch", "--repo", str(repo), "--root", root, "--force", "--json"]
    )
    assert result.exit_code == 0, result.output
    (record,) = json.loads(result.stdout)["worktrees"]
    assert Path(record["worktree_path"]) == nested
    assert record["branch"] == "nested-branch"
    assert not nested.exists()
    assert (direct / "keep.txt").read_text() == "uncommitted user work\n"


@pytest.mark.parametrize("command", ["enter", "teardown"])
def test_nested_root_uses_creation_lock(repo: Path, root: str, nested_worktrees, command: str):
    direct, nested = nested_worktrees
    extra = ["--force"] if command == "teardown" else []
    with locking.worktree_lock(str(repo), str(nested.parent), nested.name):
        result = _run(
            [command, "nested-branch", "--repo", str(repo), "--root", root, *extra, "--json"]
        )
    assert result.exit_code == 5, result.output
    assert json.loads(result.stderr)["error"]["code"] == "LOCK_HELD"
    assert direct.exists() and nested.exists()


def test_nested_root_lock_does_not_block_other_root(repo: Path, root: str, nested_worktrees):
    direct, nested = nested_worktrees
    with locking.worktree_lock(str(repo), str(direct.parent), direct.name):
        result = _run(["enter", "nested-branch", "--repo", str(repo), "--root", root, "--json"])
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["worktree_path"]) == nested


def test_root_alias_preserves_path_spelling(
    repo: Path, root: str, nested_worktrees, tmp_path: Path
):
    direct, nested = nested_worktrees
    alias = tmp_path / "alias"
    alias.symlink_to(Path(root), target_is_directory=True)
    result = _run(["enter", "nested-branch", "--repo", str(repo), "--root", str(alias), "--json"])
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["worktree_path"]) == alias / "nested" / nested.name
    assert direct.exists() and nested.exists()


def test_nested_batch_preserves_distinct_paths(repo: Path, root: str, nested_worktrees):
    direct, nested = nested_worktrees
    subprocess.run(["git", "-C", str(direct), "branch", "-m", "direct-branch"], check=True)
    listed = _run(["list", "--repo", str(repo), "--root", root, "--json"])
    assert {Path(row["path"]) for row in json.loads(listed.stdout)["worktrees"]} == {direct, nested}
    result = _run(
        [
            "teardown",
            "direct-branch",
            "nested-branch",
            "--repo",
            str(repo),
            "--root",
            root,
            "--force",
            "--json",
        ]
    )
    assert result.exit_code == 0, result.output
    assert {Path(row["worktree_path"]) for row in json.loads(result.stdout)["worktrees"]} == {
        direct,
        nested,
    }
    assert not direct.exists() and not nested.exists()


def test_nested_batch_lock_conflict_removes_nothing(repo: Path, root: str, nested_worktrees):
    direct, nested = nested_worktrees
    subprocess.run(["git", "-C", str(direct), "branch", "-m", "direct-branch"], check=True)
    with locking.worktree_lock(str(repo), str(nested.parent), nested.name):
        result = _run(
            [
                "teardown",
                "direct-branch",
                "nested-branch",
                "--repo",
                str(repo),
                "--root",
                root,
                "--force",
                "--json",
            ]
        )
    assert result.exit_code == 5, result.output
    assert json.loads(result.stderr)["error"]["code"] == "LOCK_HELD"
    assert direct.exists() and nested.exists()


def test_nested_dirty_gate_and_corrupt_recovery(repo: Path, root: str, nested_worktrees):
    direct, nested = nested_worktrees
    (nested / "user-work.txt").write_text("keep\n")
    result = _run(["teardown", "nested-branch", "--repo", str(repo), "--root", root, "--json"])
    assert result.exit_code == 5
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "DIRTY_WORKTREE"
    assert Path(error["path"]) == nested
    assert nested.exists() and direct.exists()
    (nested / ".git").unlink()
    result = _run(
        ["teardown", "nested-branch", "--repo", str(repo), "--root", root, "--force", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert not nested.exists() and direct.exists()


def test_nested_runner_and_state_use_selected_path(
    repo: Path, root: str, nested_worktrees, monkeypatch
):
    from treebox.runners.host import HostRunner

    direct, nested = nested_worktrees
    selected = state.load(nested)
    assert selected is not None
    selected.harness = "codex"
    selected.lockfile_hash = "stale"
    state.save(nested, selected)
    before = state.load(direct)
    result = _run(["enter", "nested-branch", "--repo", str(repo), "--root", root, "--json"])
    assert result.exit_code == 0, result.output
    assert "codex" in json.loads(result.stdout)["entry_command"][-1]
    after = state.load(nested)
    assert after is not None and after.harness == "codex" and after.lockfile_hash != "stale"
    assert state.load(direct) == before
    actual_teardown = HostRunner.teardown
    observed = []

    def record_teardown(self, wt, *, reporter):
        observed.append(wt.path)
        return actual_teardown(self, wt, reporter=reporter)

    monkeypatch.setattr(HostRunner, "teardown", record_teardown)
    result = _run(
        ["teardown", "nested-branch", "--repo", str(repo), "--root", root, "--force", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert observed == [nested]
    assert direct.exists()

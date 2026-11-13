"""Cleanup through its interface, with real git registrations and records."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from treebox import cleanup, git, locking, record
from treebox.config import Config
from treebox.models import Worktree
from treebox.output import Reporter
from treebox.runners.base import RunnerTeardownResult
from treebox.runners.host import HostRunner


@pytest.fixture
def worktree(repo: Path, tmp_path: Path) -> Worktree:
    path = tmp_path / "worktrees" / "cleanup-task"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "cleanup-task", str(path)],
        check=True,
        capture_output=True,
    )
    record.save(record.WorktreeRecord(path, "main", "host", "claude", False, "default"))
    return Worktree.at(str(repo), path, branch="cleanup-task")


class ControlledRunner(HostRunner):
    """A runner whose external resource can fail once, then be removed."""

    def __init__(self, resource: Path, *, raises: bool = False) -> None:
        super().__init__(Config())
        self.resource = resource
        self.raises = raises

    def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
        # Resource cleanup must happen while the worktree and record remain.
        self.resource.write_text(f"worktree={wt.path.is_dir()},record={record.exists(wt.path)}\n")
        if self.raises:
            raise RuntimeError("resource unavailable")
        return RunnerTeardownResult("failed", remaining=("test resource (unavailable)",))


@pytest.mark.parametrize("raises", [False, True])
def test_failed_runner_keeps_worktree_record_and_branch_until_retry(
    worktree: Worktree, tmp_path: Path, raises: bool
) -> None:
    resource = tmp_path / "runner-resource"
    reporter = Reporter(silent=True)
    with locking.worktree_lock(worktree.path):
        failed = cleanup.cleanup_one(
            worktree,
            ControlledRunner(resource, raises=raises),
            force=True,
            delete_branch=True,
            reporter=reporter,
        )
    assert (failed.complete, failed.worktree, failed.branch_deleted) == (
        False,
        "cleanup_incomplete",
        False,
    )
    assert failed.remaining == (
        ("host resources (resource unavailable)",) if raises else ("test resource (unavailable)",)
    )
    assert resource.read_text() == "worktree=True,record=True\n"
    assert worktree.path.is_dir() and record.load(worktree.path).isolation == "host"
    assert git.branch_for_path(worktree.repo, str(worktree.path)) == "cleanup-task"

    class RecoveredRunner(HostRunner):
        def teardown(self, wt: Worktree, *, reporter: Reporter) -> RunnerTeardownResult:
            assert wt.path.is_dir() and record.exists(wt.path)
            resource.unlink()
            return RunnerTeardownResult.cleaned()

    with locking.worktree_lock(worktree.path):
        done = cleanup.cleanup_one(
            worktree, RecoveredRunner(Config()), force=True, delete_branch=True, reporter=reporter
        )
    assert (done.complete, done.worktree, done.branch_deleted, done.remaining) == (
        True,
        "removed",
        True,
        (),
    )
    assert not resource.exists() and not record.exists(worktree.path)
    assert not worktree.path.exists()
    assert not git.worktree_registered(worktree.repo, str(worktree.path))
    assert not git.local_branch_exists(worktree.repo, "cleanup-task")


def test_git_refusal_keeps_files_and_record_without_deletion_fallback(worktree: Worktree) -> None:
    subprocess.run(["git", "-C", worktree.repo, "worktree", "lock", str(worktree.path)], check=True)
    sentinel = worktree.path / "uncommitted.txt"
    sentinel.write_text("keep this work\n")
    with locking.worktree_lock(worktree.path):
        result = cleanup.cleanup_one(
            worktree,
            HostRunner(Config()),
            force=True,
            delete_branch=True,
            reporter=Reporter(silent=True),
        )
    assert (result.complete, result.worktree, result.branch_deleted) == (False, "kept", False)
    assert "git refused to remove it" in result.remaining[0]
    assert sentinel.read_text() == "keep this work\n"
    assert record.load(worktree.path).isolation == "host"
    assert git.branch_for_path(worktree.repo, str(worktree.path)) == "cleanup-task"


@pytest.mark.parametrize("missing_directory", [False, True])
def test_broken_or_missing_worktree_can_complete_cleanup(
    worktree: Worktree, missing_directory: bool
) -> None:
    import shutil

    if missing_directory:
        shutil.rmtree(worktree.path)
    else:
        (worktree.path / ".git").unlink()
    with locking.worktree_lock(worktree.path):
        result = cleanup.cleanup_one(
            worktree, None, force=True, delete_branch=False, reporter=Reporter(silent=True)
        )
    assert (result.complete, result.worktree, result.branch) == (
        True,
        "already_gone" if missing_directory else "removed",
        "cleanup-task",
    )
    assert not worktree.path.exists() and not record.exists(worktree.path)
    assert not git.worktree_registered(worktree.repo, str(worktree.path))
    assert git.local_branch_exists(worktree.repo, "cleanup-task")


def test_main_worktree_is_rejected_before_runner_cleanup(repo: Path, tmp_path: Path) -> None:
    resource = tmp_path / "runner-resource"
    resource.write_text("untouched\n")
    with pytest.raises(cleanup.MainWorktreeError, match="main working tree"):
        cleanup.cleanup_one(
            Worktree.at(str(repo), repo),
            ControlledRunner(resource),
            force=True,
            delete_branch=True,
            reporter=Reporter(silent=True),
        )
    assert resource.read_text() == "untouched\n"
    assert (repo / "uv.lock").read_text() == "version = 1\n"


def test_cleanup_releases_ports_after_worktree_and_record_are_gone(
    worktree: Worktree, monkeypatch: pytest.MonkeyPatch
) -> None:
    from treebox import ports
    from treebox.config import PortRange
    from treebox.models import PortBlock

    with locking.worktree_lock(worktree.path):
        block, _ = ports.reserve(worktree.path, worktree.repo, PortRange())
        release = ports.release
        resources_present: list[tuple[bool, bool, bool]] = []

        def observed_release(path: Path) -> PortBlock | None:
            resources_present.append(
                (
                    path.exists(),
                    git.worktree_registered(worktree.repo, str(path)),
                    record.exists(path),
                )
            )
            return release(path)

        monkeypatch.setattr(ports, "release", observed_release)
        result = cleanup.cleanup_one(
            worktree, None, force=False, delete_branch=False, reporter=Reporter(silent=True)
        )
    assert result.complete is True
    assert result.released_ports == block
    assert result.port_error is None
    assert resources_present == [(False, False, False)]
    assert release(worktree.path) is None

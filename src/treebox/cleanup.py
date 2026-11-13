"""Remove one worktree in order and retain the records needed for a retry.

Callers resolve the runner, validate the batch and hold the worktree lock.
This module owns runner resources, worktree removal and ownership record
removal. Runner adapters retain ownership of their resource manifests.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Literal

from . import git, locking, provision, record
from .models import Worktree, same_path
from .output import Reporter
from .runners import Runner
from .runners.base import RunnerTeardownResult

WorktreeRemoval = Literal["removed", "already_gone", "kept", "cleanup_incomplete"]


class MainWorktreeError(RuntimeError):
    """The repository's main working tree is never a cleanup target."""


@dataclass(frozen=True)
class CleanupResult:
    """Completed effects and remaining resources, independent of CLI output."""

    runner: RunnerTeardownResult
    worktree: WorktreeRemoval
    branch: str | None
    remaining: tuple[str, ...] = ()
    branch_deleted: bool = False
    runner_error: str | None = None
    branch_error: str | None = None

    @property
    def complete(self) -> bool:
        return not self.remaining


def cleanup_one(
    wt: Worktree,
    runner: Runner | None,
    *,
    force: bool,
    delete_branch: bool,
    reporter: Reporter,
) -> CleanupResult:
    """Remove resources, the worktree, then its record. Return what remains.

    A missing runner means its cleanup was explicitly skipped or no recorded
    isolation mode survives. A runner failure keeps the worktree and record.
    Git refusal never falls back to filesystem deletion. Branch deletion is
    best effort after cleanup, as it is not part of resource ownership.
    """
    if same_path(wt.path, wt.repo):
        raise MainWorktreeError(f"Refusing to remove the main working tree '{wt.name}'.")
    branch = (
        wt.branch
        if wt.branch and (wt.path.is_dir() or git.local_branch_exists(wt.repo, wt.branch))
        else None
    )
    runner_error = None
    if runner is None:
        resources = RunnerTeardownResult.skipped()
    else:
        try:
            resources = runner.teardown(wt, reporter=reporter)
        except Exception as exc:
            runner_error = str(exc)
            resources = RunnerTeardownResult(
                "failed", remaining=(f"{runner.name} resources ({exc})",)
            )
    if resources.remaining:
        return CleanupResult(
            resources, "cleanup_incomplete", branch, resources.remaining, runner_error=runner_error
        )

    # Runner cleanup can be slow. Only shared git mutations queue behind
    # other creates and teardowns of this repository.
    with locking.repo_lock(wt.repo):
        removal, remaining = _remove_worktree(wt, force=force)
        if remaining:
            return CleanupResult(
                resources,
                removal,
                branch,
                remaining,
                runner_error=runner_error,
            )

        record.remove(wt.path)
        branch_deleted = False
        branch_error = None
        if delete_branch and branch:
            try:
                git.delete_branch(wt.repo, branch)
                branch_deleted = True
            except git.GitError as exc:
                branch_error = str(exc)
        return CleanupResult(
            resources,
            removal,
            branch,
            branch_deleted=branch_deleted,
            runner_error=runner_error,
            branch_error=branch_error,
        )


def _remove_worktree(wt: Worktree, *, force: bool) -> tuple[WorktreeRemoval, tuple[str, ...]]:
    health = provision.worktree_health(wt.repo, wt.path)
    remaining: list[str] = []
    removed = False
    if health in ("healthy", "unreadable_state"):
        try:
            git.worktree_remove(wt.repo, wt.path, force=force)
            removed = True
        except git.GitError as exc:
            remaining.append(f"worktree (git refused to remove it: {exc})")
    elif health == "invalid_linkage":
        try:
            shutil.rmtree(wt.path)
            removed = True
        except OSError as exc:
            remaining.append(f"worktree (cannot remove the directory: {exc})")
    elif health == "unregistered":
        remaining.append("worktree (git has no registration for this directory; remove it by hand)")
    if not removed or health == "invalid_linkage":
        git.worktree_prune(wt.repo)
    if git.worktree_registered(wt.repo, str(wt.path)):
        remaining.append("git registration (git still lists the worktree)")
    outcome: WorktreeRemoval = (
        "removed"
        if removed
        else "already_gone"
        if health == "directory_missing" and not remaining
        else "kept"
    )
    return outcome, tuple(remaining)

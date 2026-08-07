"""Resolve a user-supplied ``<ref>`` to a worktree.

``enter`` and ``teardown`` accept a worktree name, a branch, or a unique
substring of either — resolved live from ``git worktree list --porcelain``
(the branch is a mutable attribute; only git knows the current one).
Ambiguity is a loud usage error, never a guess.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from . import git
from .models import is_slug, path_is_under, same_path, worktree_path, worktree_root
from .provision import NotFoundError, ProvisionError


class AmbiguousRefError(ProvisionError):
    """The ref matches more than one worktree (exit 2, AMBIGUOUS_REF)."""

    def __init__(self, ref: str, matches: list[Candidate]) -> None:
        shown = ", ".join(f"{c.name} ({c.branch})" if c.branch else c.name for c in matches)
        super().__init__(f"'{ref}' matches more than one worktree: {shown}.")
        self.hint = "Use the full name or branch (treebox list shows both)."


@dataclass(frozen=True)
class StrayDirectory:
    root: str
    root_identity: tuple[int, int]
    target_identity: tuple[int, int]


@dataclass(frozen=True)
class Candidate:
    """One live worktree under the treebox root: its permanent name (the
    directory leaf) and its current branch, straight from git. ``stray`` marks
    the narrow teardown recovery case: an exact, safe directory with no Git
    registration and no matching branch."""

    name: str
    branch: str | None
    path: str
    stray: StrayDirectory | None = None


def candidates(repo: str, root: str) -> list[Candidate]:
    base = worktree_root(repo, root)
    found = []
    for rec in git.worktree_list(repo):
        path = Path(rec.path)
        if not path_is_under(path, base):
            continue
        # A root containing the repo (e.g. root = "..") makes the main checkout
        # pass the filter above; it is never a treebox worktree, so resolving it
        # here would let enter/teardown act on the repo itself.
        if same_path(path, repo):
            continue
        found.append(Candidate(name=path.name, branch=rec.branch, path=rec.path))
    return found


def resolve_ref(repo: str, root: str, ref: str) -> Candidate:
    """Name first (exact), then branch (exact), then a unique substring of
    either. Raises NotFoundError / AmbiguousRefError otherwise."""
    if not ref.strip():
        exc = NotFoundError("No worktree ref given (empty).")
        exc.hint = "Pass a worktree name or branch (treebox list shows them)."
        raise exc
    cands = candidates(repo, root)
    for exact in ([c for c in cands if c.name == ref], [c for c in cands if c.branch == ref]):
        if len(exact) == 1:
            return exact[0]
        if exact:  # two worktrees can't share a branch, but never guess
            raise AmbiguousRefError(ref, exact)
    partial = [c for c in cands if ref in c.name or (c.branch and ref in c.branch)]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise AmbiguousRefError(ref, partial)
    exc = NotFoundError(f"No worktree matches '{ref}'.")
    exc.hint = "treebox list shows what exists; treebox create starts new work."
    raise exc


def exact_stray(repo: str, root: str, ref: str) -> Candidate | None:
    """Return an exact unregistered-directory teardown target, if it is safe.

    This is not general discovery. The ref must be one directory-leaf slug,
    and only ``<root>/<ref>`` is checked. Symlinks are not targets because they
    can point outside the configured root. Call this only after registered
    worktree and branch resolution fails, so those trusted cases keep their
    normal teardown behavior.
    """
    if not is_slug(ref):
        return None
    path = worktree_path(repo, root, ref)
    base = worktree_root(repo, root)
    stray = _stray_directory(base, ref)
    if stray is None or same_path(path, repo) or _stray_has_git_state(repo, stray, ref):
        return None
    return Candidate(name=ref, branch=None, path=str(path), stray=stray)


def remove_exact_stray(repo: str, cand: Candidate) -> bool:
    stray = cand.stray
    if stray is None:
        return False
    try:
        root_fd = _open_directory(Path(stray.root))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return False
        raise
    try:
        if _identity(os.fstat(root_fd)) != stray.root_identity:
            return False
        try:
            current = os.stat(cand.name, dir_fd=root_fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != stray.target_identity:
            return False
        if _stray_has_git_state(repo, stray, cand.name):
            return False
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError("safe directory-relative removal is unavailable")
        try:
            shutil.rmtree(cand.name, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        except OSError:
            try:
                changed = os.stat(cand.name, dir_fd=root_fd, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                return False
            if not stat.S_ISDIR(changed.st_mode) or _identity(changed) != stray.target_identity:
                return False
            raise
    finally:
        os.close(root_fd)
    return True


def _stray_directory(root: Path, name: str) -> StrayDirectory | None:
    try:
        physical_root = root.resolve(strict=True)
        root_fd = _open_directory(physical_root)
    except OSError:
        return None
    try:
        root_stat = os.fstat(root_fd)
        target_stat = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(target_stat.st_mode):
        return None
    return StrayDirectory(
        root=str(physical_root),
        root_identity=_identity(root_stat),
        target_identity=_identity(target_stat),
    )


def _stray_has_git_state(repo: str, stray: StrayDirectory, name: str) -> bool:
    target = Path(stray.root) / name
    return git.local_branch_exists(repo, name) or git.worktree_registered(repo, str(target))


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags)


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino

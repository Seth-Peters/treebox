"""Resolve worktree refs and exact unregistered teardown targets.

For registered worktrees, ``enter`` and ``teardown`` accept a name, a branch,
or a unique substring of either. Resolution uses live
``git worktree list --porcelain`` data because a branch is mutable. Teardown
also has a guarded exact-name recovery for an unregistered directory.
Ambiguity is a usage error, never a guess.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from . import git
from .models import derive_name, is_slug, path_is_under, same_path, worktree_path, worktree_root
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
    """One registered worktree or exact unregistered teardown target.

    Registered candidates use the permanent directory-leaf name and the live
    Git branch. ``stray`` marks the narrow recovery case: a safe directory with
    no Git registration and no matching branch.
    """

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
    exact = _exact_ref(ref, cands)
    if exact is not None:
        return exact
    partial = [c for c in cands if ref in c.name or (c.branch and ref in c.branch)]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise AmbiguousRefError(ref, partial)
    exc = NotFoundError(f"No worktree matches '{ref}'.")
    exc.hint = "treebox list shows what exists; treebox create starts new work."
    raise exc


def resolve_exact_ref(repo: str, root: str, ref: str) -> Candidate | None:
    """Resolve only an exact registered name or branch.

    Teardown uses this before its exact stray-directory recovery. General
    substring matching stays in ``resolve_ref`` for all other cases.
    """
    if not ref.strip():
        return None
    return _exact_ref(ref, candidates(repo, root))


def _exact_ref(ref: str, cands: list[Candidate]) -> Candidate | None:
    for exact in ([c for c in cands if c.name == ref], [c for c in cands if c.branch == ref]):
        if len(exact) == 1:
            return exact[0]
        if exact:  # two worktrees can't share a branch, but never guess
            raise AmbiguousRefError(ref, exact)
    return None


def exact_stray(repo: str, root: str, ref: str) -> Candidate | None:
    """Return an exact unregistered-directory teardown target, if it is safe.

    This is not general discovery. The ref must be one directory-leaf slug,
    and only ``<root>/<ref>`` is checked. Symlinks are not targets because they
    can point outside the configured root. Call this only after exact
    registered-name and branch resolution fails, so those trusted cases keep
    their normal teardown behavior.
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
        current = _exact_entry_stat(root_fd, cand.name)
        if current is None:
            return False
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != stray.target_identity:
            return False
        if _stray_has_git_state(repo, stray, cand.name):
            return False
        if not shutil.rmtree.avoids_symlink_attacks:
            raise OSError("safe directory-relative removal is unavailable")
        current = _exact_entry_stat(root_fd, cand.name)
        if current is None or _identity(current) != stray.target_identity:
            return False
        try:
            shutil.rmtree(cand.name, dir_fd=root_fd)
        except FileNotFoundError:
            return False
        except OSError:
            changed = _exact_entry_stat(root_fd, cand.name)
            if changed is None:
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
        target_stat = _exact_entry_stat(root_fd, name)
    except OSError:
        return None
    finally:
        os.close(root_fd)
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or target_stat is None
        or not stat.S_ISDIR(target_stat.st_mode)
    ):
        return None
    return StrayDirectory(
        root=str(physical_root),
        root_identity=_identity(root_stat),
        target_identity=_identity(target_stat),
    )


def _stray_has_git_state(repo: str, stray: StrayDirectory, name: str) -> bool:
    target = Path(stray.root) / name
    return git.worktree_registered(repo, str(target)) or any(
        derive_name(branch) == name for branch in git.branch_names(repo)
    )


def _exact_entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if entry.name == name:
                try:
                    return entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    return None
    return None


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags)


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino

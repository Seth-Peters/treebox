"""Resolve a user-supplied ``<ref>`` to a worktree.

``enter`` and ``teardown`` accept a worktree name, a branch, or a unique
substring of either — resolved live from ``git worktree list --porcelain``
(the branch is a mutable attribute; only git knows the current one).
Ambiguity is a loud usage error, never a guess.
"""

from __future__ import annotations

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
class Candidate:
    """One live worktree under the treebox root: its permanent name (the
    directory leaf) and its current branch, straight from git. ``stray`` marks
    the narrow teardown recovery case: an exact, safe directory with no Git
    registration and no matching branch."""

    name: str
    branch: str | None
    path: str
    stray: bool = False


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
    if (
        path.is_symlink()
        or not path.is_dir()
        or not path_is_under(path, base)
        or same_path(path, repo)
    ):
        return None
    return Candidate(name=ref, branch=None, path=str(path), stray=True)

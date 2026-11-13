"""Resolve a user-supplied ``<ref>`` to a worktree.

``enter``, ``adopt`` and ``teardown`` accept a worktree name, a branch, a
unique substring of either, or an absolute worktree path - resolved live
from ``git worktree list --porcelain`` (the branch is a mutable attribute;
only git knows the current one).
Ambiguity is a loud usage error, never a guess.

An absolute path is the exact ref: it matches only the worktree registered at
that path (the worktree's identity, see ``models.py``) and never falls back to
a name, branch or substring match. A caller that retries a request - a
coordinator whose reply was lost - addresses the task by the
``worktree_path`` that ``create --json`` returned, so a retry of a request
that already succeeded finds nothing (``NOT_FOUND``) instead of another
worktree whose name contains the first one's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import git
from .models import path_is_under, same_path, worktree_root
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
    directory leaf) and its current branch, straight from git."""

    name: str
    branch: str | None
    path: str

    @classmethod
    def registered(cls, repo: str, root: str, path: str, branch: str | None) -> Candidate:
        """The candidate for one git-registered worktree path under ``root``.

        The one place a registration becomes a candidate, so every consumer
        (ref resolution, the teardown chooser) agrees on two things: the name
        is the directory leaf, and the path keeps its *full* registered suffix
        below the root - a nested worktree is never reduced to ``root/leaf``,
        which would name a different worktree.

        The prefix is re-spelled the way the caller's root spells it: git may
        canonicalize an alias (``/var`` -> ``/private/var``), while runner
        resource names were derived from the spelling used at create.
        """
        base = worktree_root(repo, root)
        registered = Path(path)
        for prefix in (base, base.resolve()):
            try:
                relative = registered.relative_to(prefix)
            except ValueError:
                continue
            return cls(name=registered.name, branch=branch, path=str(base / relative))
        # The registration uses another alias for the root. Keep it verbatim
        # rather than resolving a possibly replaced target into another tree.
        return cls(name=registered.name, branch=branch, path=path)


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
        found.append(Candidate.registered(repo, root, rec.path, rec.branch))
    return found


def resolve_ref(repo: str, root: str, ref: str) -> Candidate:
    """An absolute path matches that registered worktree only. Otherwise:
    name first (exact), then branch (exact), then a unique substring of
    either. Raises NotFoundError / AmbiguousRefError otherwise."""
    if not ref.strip():
        exc = NotFoundError("No worktree ref given (empty).")
        exc.hint = "Pass a worktree name or branch (treebox list shows them)."
        raise exc
    cands = candidates(repo, root)
    if Path(ref).is_absolute():
        at_path = [c for c in cands if same_path(c.path, ref)]
        if at_path:
            return at_path[0]
        exc = NotFoundError(f"No worktree is registered at '{ref}'.")
        exc.hint = (
            "A path ref matches only that exact worktree; treebox list --json shows each path."
        )
        raise exc
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

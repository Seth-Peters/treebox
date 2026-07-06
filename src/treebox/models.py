"""Core value objects shared across modules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

# Branches treebox generates carry this prefix. It marks the name as
# machine-made: the pre-push guard refuses to push them, so a placeholder can
# never become a PR title — the agent must `git branch -m <real-name>` first.
PLACEHOLDER_PREFIX = "treebox/"

# One clean lowercase token — the grammar of a generated name and of each
# slash-separated segment of an explicit `create NAME`. Anything fancier
# (spaces, unicode, dots) is a usage error, not something we slugify.
SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def is_slug(name: str) -> bool:
    return bool(SLUG_RE.match(name))


def is_valid_name(name: str) -> bool:
    """What an explicit ``create NAME`` accepts: slug tokens joined by slashes
    (``fix-login``, ``feature/user-auth``). The name doubles as the branch
    name — slashes stay in the branch and flatten to ``--`` in the directory.
    Empty segments (``fix//x``, a leading/trailing ``/``) fail via ``is_slug``."""
    return all(is_slug(part) for part in name.split("/"))


def placeholder_branch(name: str) -> str:
    return PLACEHOLDER_PREFIX + name


def is_placeholder(branch: str | None) -> bool:
    return bool(branch) and str(branch).startswith(PLACEHOLDER_PREFIX)


def derive_name(branch: str) -> str:
    """The worktree name a branch implies: a placeholder's own slug
    (``treebox/fix-auth`` -> ``fix-auth``), else the flattened branch
    (``create --checkout feature/auth`` -> ``feature--auth``)."""
    if is_placeholder(branch):
        return branch.removeprefix(PLACEHOLDER_PREFIX)
    return flatten_branch(branch)


def flatten_branch(branch: str) -> str:
    """Derive a directory-safe name from a branch: slashes flattened to ``--``.

    ``feature/auth`` -> ``feature--auth``. Only used to *derive* a worktree
    name from a branch-shaped input (``create feature/auth``,
    ``create --checkout <branch>``); the name — not the branch — is the
    worktree's permanent identity from then on.
    """
    return branch.replace("/", "--")


def worktree_root(repo: str, root: str) -> Path:
    """Absolute worktree root. ``root`` may be absolute or relative to repo."""
    p = Path(root)
    if p.is_absolute():
        return p
    return Path(repo) / root


def worktree_path(repo: str, root: str, name: str) -> Path:
    return worktree_root(repo, root) / name


def same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def path_is_under(path: str | Path, root: str | Path) -> bool:
    return Path(path).resolve(strict=False).is_relative_to(Path(root).resolve(strict=False))


DepsFreshness = Literal["fresh", "stale", "unknown"]


class WorktreeRow(TypedDict):
    """One worktree as shown by ``list`` and the teardown chooser.

    This IS the ``--json`` list schema: rows are emitted verbatim as the
    ``worktrees`` payload, so fields only get added here (porcelain
    discipline) — a breaking reshape bumps ``cli.SCHEMA_VERSION``.
    """

    name: str
    branch: str
    unnamed: bool  # branch is a treebox/<name> placeholder
    missing: bool  # registration whose working dir is gone (prunable)
    last_commit: str
    commit_epoch: int
    path: str
    base: str
    isolation: str
    harness: str
    deps: DepsFreshness
    env: Literal["present", "absent"]


class TemplateRow(TypedDict):
    """One sandbox template as shown by ``template list`` / ``template ls``.

    This IS the ``--json`` ``templates`` schema: rows are emitted verbatim, so
    fields only get added here (porcelain discipline) — a breaking reshape
    bumps ``cli.SCHEMA_VERSION``.
    """

    name: str
    path: str
    source: str  # where it resolves from: env | user | bundled
    default: bool  # matches the configured default template
    valid: bool  # no required files missing
    missing: list[str]


@dataclass(frozen=True)
class Worktree:
    """A provisioned (or to-be-provisioned) worktree.

    ``name`` is the permanent identity: the directory leaf, the lock key, and
    what container names/labels derive from. It is never renamed — the docker
    runner bind-mounts the absolute path and a live agent's CWD sits in it.
    ``branch`` is a mutable attribute: the branch the worktree was materialized
    on, expected to be renamed by the agent (``git branch -m``); always read it
    live from git when it matters.
    """

    repo: str
    name: str
    branch: str
    base: str
    path: Path

    @classmethod
    def locate(cls, repo: str, root: str, name: str, branch: str = "", base: str = "") -> Worktree:
        return cls(
            repo=repo,
            name=name,
            branch=branch,
            base=base,
            path=worktree_path(repo, root, name),
        )

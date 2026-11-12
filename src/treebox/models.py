"""Core value objects shared across modules."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

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


# A sandbox template name is an identifier, never a path: one segment of a
# conservative, filesystem-safe charset, so it can only select a directory
# under the operator-owned templates root (see ``assets.template_dir``).
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def is_valid_template_name(name: str) -> bool:
    """Whether ``name`` is a template identifier: no separators, no
    traversal (``.`` / ``..``), and therefore no absolute path."""
    return name not in (".", "..") and bool(TEMPLATE_NAME_RE.match(name))


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
    ``create --checkout <branch>``).
    """
    return branch.replace("/", "--")


def expand_user(path: str | Path) -> Path:
    """``path`` with a leading ``~`` marker expanded. An unresolvable marker
    (``~nosuchuser``) stays literal — a config typo must surface as a normal
    path error, never a traceback (unlike ``Path.expanduser``, which raises)."""
    return Path(os.path.expanduser(path))


def worktree_root(repo: str, root: str) -> Path:
    """Absolute worktree root. ``root`` may be absolute, ``~``-prefixed, or
    relative to repo."""
    p = expand_user(root)
    if p.is_absolute():
        return p
    return Path(repo) / p


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
    # The recorded limits ({cpus, memory, pids}, null = no treebox cap);
    # null when the worktree has no valid host-owned record.
    resources: dict[str, Any] | None
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
    valid: bool  # no required files missing, JSON contents parse
    missing: list[str]
    invalid: list[str]  # present JSON files that fail to parse
    firewall: bool  # has valid firewall.json, so it can serve --firewall


@dataclass(frozen=True)
class Worktree:
    """A provisioned (or to-be-provisioned) worktree.

    ``path`` is the identity: the registered directory every command acts on,
    what the lock, the runner's resources, and the state lookup all derive
    from. It is never renamed - the docker runner bind-mounts the absolute
    path and a live agent's CWD sits in it. ``name`` is its directory leaf,
    the handle users type; it is unique under one parent but two parents (a
    nested root) can each hold the same name, so never rebuild a path from a
    root and a name once a worktree exists - carry the path (``Worktree.at``).
    ``branch`` is a mutable attribute: the branch the worktree was
    materialized on, expected to be renamed by the agent (``git branch -m``);
    always read it live from git when it matters.
    """

    repo: str
    name: str
    branch: str
    base: str
    path: Path

    @classmethod
    def at(cls, repo: str, path: Path, *, branch: str = "", base: str = "") -> Worktree:
        """An *existing* worktree, identified by its registered path (the
        route for ``enter``/``teardown``, which resolve a ref to a path)."""
        return cls(repo=repo, name=path.name, branch=branch, base=base, path=path)

    @classmethod
    def locate(cls, repo: str, root: str, name: str, branch: str = "", base: str = "") -> Worktree:
        """A worktree *to be created* directly under ``root`` (the route for
        ``create``, the only command that derives a path from root + name)."""
        return cls(
            repo=repo,
            name=name,
            branch=branch,
            base=base,
            path=worktree_path(repo, root, name),
        )

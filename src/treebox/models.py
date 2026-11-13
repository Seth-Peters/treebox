"""Core value objects shared across modules."""

from __future__ import annotations

import hashlib
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
    """The worktree name a branch-shaped input implies. This is the one place
    a name comes from a branch (``create NAME``, ``create --checkout``, the
    teardown fallback for a branch without a worktree).

    A placeholder drops its ``treebox/`` prefix, then every remaining slash
    flattens to ``--``: ``treebox/fix-auth`` -> ``fix-auth``,
    ``treebox/a/b`` -> ``a--b``, ``feature/auth`` -> ``feature--auth``. The
    name is always one directory leaf, so treebox never nests a worktree.
    """
    return branch.removeprefix(PLACEHOLDER_PREFIX).replace("/", "--")


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


# The variables treebox exports to setup and to the agent entry (see
# ``Worktree.treebox_env``). Repo-owned scripts read them to run services in
# parallel worktrees; treebox starts no service itself.
ENV_NAME = "TREEBOX_NAME"
ENV_ID = "TREEBOX_ID"
ENV_PORT_BASE = "TREEBOX_PORT_BASE"
ENV_PORT_COUNT = "TREEBOX_PORT_COUNT"

# The ``worktree_id`` format is documented contract: exactly this many
# lowercase hex characters. Repo scripts put it in database and Compose project
# names, and the docker runner's sandbox names embed it, so it never changes.
WORKTREE_ID_LENGTH = 10


def worktree_id(path: Path) -> str:
    """The machine-unique id of the worktree registered at ``path``:
    ``WORKTREE_ID_LENGTH`` lowercase hex characters of the SHA-256 of the path.

    Derived from the path, never from the name: a name repeats across repos
    and nested roots, the registered path does not. This is the one
    derivation of a worktree's hash: the docker runner's container and image
    names embed it, so ``TREEBOX_ID`` and the sandbox name always agree."""
    return hashlib.sha256(str(path).encode()).hexdigest()[:WORKTREE_ID_LENGTH]


def same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def path_is_under(path: str | Path, root: str | Path) -> bool:
    return Path(path).resolve(strict=False).is_relative_to(Path(root).resolve(strict=False))


@dataclass(frozen=True)
class PortBlock:
    """A block of ``count`` consecutive TCP/UDP port numbers from ``base``,
    reserved for one worktree by the machine-wide allocator (``ports.py``)."""

    base: int
    count: int

    @property
    def last(self) -> int:
        return self.base + self.count - 1

    def overlaps(self, other: PortBlock) -> bool:
        return self.base <= other.last and other.base <= self.last

    def __str__(self) -> str:
        return f"{self.base}-{self.last}"


DepsFreshness = Literal["fresh", "stale", "unknown"]

# The setup model's states (see ``lifecycle.py``).
SetupStatus = Literal["pending", "failed", "done"]

# The one classification of a worktree's health (``provision.worktree_health``):
#   healthy            registered, linked to its own registration, record readable
#   directory_missing  the directory is gone (a registration may remain)
#   unregistered       a directory git has no registration for
#   invalid_linkage    registered, but its .git pointer is missing or leads
#                      elsewhere: git inside it would act on the main checkout
#   unreadable_state   git is fine, but the host-owned record is missing or invalid
Health = Literal[
    "healthy", "directory_missing", "unregistered", "invalid_linkage", "unreadable_state"
]


class WorktreeRow(TypedDict):
    """One worktree as shown by ``list`` and the teardown chooser.

    This IS the ``--json`` list schema: rows are emitted verbatim as the
    ``worktrees`` payload, so fields only get added here (porcelain
    discipline) — a breaking reshape bumps ``cli.SCHEMA_VERSION``.
    """

    name: str
    id: str  # TREEBOX_ID: models.worktree_id of the registered path
    branch: str
    unnamed: bool  # branch is a treebox/<name> placeholder
    # Kept for older consumers: true when ``health`` is directory_missing or
    # invalid_linkage (only the git registration is reliable).
    missing: bool
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
    health: Health
    setup: SetupStatus | Literal[""]  # "" when the record is missing or invalid
    # The reserved port block (TREEBOX_PORT_BASE / TREEBOX_PORT_COUNT); null when
    # the record holds none (missing or invalid record, or not reserved yet).
    port_base: int | None
    port_count: int | None


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
    # The reserved port block, once ``provision`` has it from the record;
    # None for a worktree that has none yet (a dry run, a teardown).
    ports: PortBlock | None = None

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

    @property
    def id(self) -> str:
        """``TREEBOX_ID``: see ``worktree_id``."""
        return worktree_id(self.path)

    def treebox_env(self) -> dict[str, str]:
        """The variables setup and the agent entry get, in both runners:
        ``TREEBOX_NAME`` (the directory leaf, the name users type),
        ``TREEBOX_ID``, and with a reserved block ``TREEBOX_PORT_BASE`` and
        ``TREEBOX_PORT_COUNT``."""
        env = {ENV_NAME: self.name, ENV_ID: self.id}
        if self.ports is not None:
            env[ENV_PORT_BASE] = str(self.ports.base)
            env[ENV_PORT_COUNT] = str(self.ports.count)
        return env

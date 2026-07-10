"""Per-worktree state, stored in the worktree's private git dir.

The git dir for a linked worktree (``.git/worktrees/<id>``) is not part of the
working tree, so state here never shows up in ``git status`` and is removed when
the worktree is pruned. We record the lockfile hash (to detect dep changes on
``enter``) plus the provisioning choices for ``list`` and for
existing-worktree sessions to recover their created-time isolation, firewall,
harness, and template defaults.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import git

_STATE_FILE = "treebox-state.json"


# Deliberately no branch here: the branch is a mutable attribute the agent is
# expected to rename (git branch -m), so a recorded copy would go silently
# stale. Anything branch-shaped is read live from git.
@dataclass
class WorktreeState:
    base: str
    isolation: str
    harness: str
    lockfile_hash: str = ""
    # Recorded False *before* setup runs (so the runner is known even if a docker
    # build/run leaves a container behind on failure), flipped True once setup
    # completes. `create` uses it to tell a half-built tree (finish it) from a
    # fully-provisioned one (a slug conflict).
    provisioned: bool = False
    # The firewall choice this worktree was created with, so `enter` honors the
    # created-time decision instead of re-resolving the config default (which
    # would hard-error when `create --no-firewall` ran under a firewall=true
    # config). `create` always records a real bool.
    firewall: bool = False
    # The sandbox template this worktree was created with, so `enter`/`teardown`
    # render the same container (user, mounts, volume names) instead of drifting
    # to the config default when `--template` is omitted. None means unrecorded —
    # the caller falls back to the config default.
    template: str | None = None


def _state_path(worktree: str | Path) -> Path:
    return Path(git.git_dir(worktree)) / _STATE_FILE


def load(worktree: str | Path) -> WorktreeState | None:
    try:
        path = _state_path(worktree)
    except git.GitError:
        return None
    return _read(path)


def load_registered(repo: str | Path, worktree: str | Path) -> WorktreeState | None:
    """Load state through the repo's own worktree registration instead of the
    worktree's ``.git`` pointer. A corrupt worktree (missing pointer) makes the
    pointer route resolve into the MAIN repo and read nothing, while the state
    recorded at create time still sits in the surviving registration dir."""
    gitdir = git.registered_gitdir(repo, worktree)
    if gitdir is None:
        return None
    return _read(gitdir / _STATE_FILE)


def _read(path: Path) -> WorktreeState | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return WorktreeState(
        base=data.get("base", ""),
        isolation=data.get("isolation", ""),
        harness=data.get("harness", ""),
        lockfile_hash=data.get("lockfile_hash", ""),
        provisioned=data.get("provisioned", False),
        firewall=data.get("firewall", False),
        # None means unrecorded, so enter/teardown fall back to the config default.
        template=data.get("template"),
    )


def save(worktree: str | Path, state: WorktreeState) -> None:
    path = _state_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")

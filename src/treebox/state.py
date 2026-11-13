"""Per-worktree observations, stored in the worktree's private git dir.

The git dir for a linked worktree (``.git/worktrees/<id>``) is not part of the
working tree, so state here never shows up in ``git status`` and is removed when
the worktree is pruned. It holds only *observations* of what setup did: the
lockfile hash (to detect dep changes on ``enter``) and whether setup completed.

This file is sandbox-writable (the docker runner mounts the git common dir), so
it never decides policy: a worktree's isolation, firewall, template, harness,
and base live in the host-owned record (``record.py``). A forged observation
can at most make ``enter`` re-run setup under the recorded policy. Reads and
writes never follow a link a boxed agent planted at the state file: a symlink
reads as no state, and a write replaces the link instead of its target.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from . import git

_STATE_FILE = "treebox-state.json"


# Deliberately no policy and no branch here: policy lives in the host-owned
# record, and the branch is a mutable attribute the agent is expected to
# rename (git branch -m), so anything branch-shaped is read live from git.
@dataclass
class WorktreeState:
    lockfile_hash: str = ""
    # Recorded False *before* setup runs, flipped True once setup completes.
    # `create` uses it to tell a half-built tree (finish it) from a
    # fully-provisioned one (a slug conflict); `enter` uses it to finish an
    # interrupted setup even when the lockfile hash is unchanged.
    provisioned: bool = False


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
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None  # absent, a planted symlink (ELOOP), or unreadable
    try:
        with os.fdopen(fd, encoding="utf-8") as f:
            data = json.loads(f.read())
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    lockfile_hash = data.get("lockfile_hash", "")
    provisioned = data.get("provisioned", False)
    return WorktreeState(
        lockfile_hash=lockfile_hash if isinstance(lockfile_hash, str) else "",
        provisioned=provisioned is True,
    )


def save(worktree: str | Path, state: WorktreeState) -> None:
    """Publish ``state`` by rename, relative to a descriptor of the git dir
    opened without following links: a symlink planted at the state file is
    replaced, never written through."""
    gitdir = Path(git.git_dir(worktree))
    dir_fd = os.open(gitdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp = f".{_STATE_FILE}.{secrets.token_hex(8)}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(asdict(state), indent=2) + "\n")
            os.replace(tmp, _STATE_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
    finally:
        os.close(dir_fd)

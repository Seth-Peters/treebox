"""Host-environment helpers for SSH-Linux ergonomics.

We resolve the invoking user's UID/GID so any bind mounts keep host ownership,
and probe PATH for required tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from shutil import which


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int


def identity() -> Identity:
    """Invoking user's UID/GID (for correct bind-mount ownership)."""
    try:
        return Identity(uid=os.getuid(), gid=os.getgid())
    except AttributeError:  # non-POSIX; mounts won't be used there anyway
        return Identity(uid=1000, gid=1000)


def have(cmd: str) -> bool:
    return which(cmd) is not None

"""Cross-process advisory locking during provisioning and teardown.

For an existing worktree, callers must pass its actual parent as ``root``.
The discovery root can be an ancestor and would lock a different worktree
with the same name. A non-blocking POSIX ``flock`` makes a second caller fail
before it can corrupt a worktree while another operation is in progress.
"""

from __future__ import annotations

import errno
from collections.abc import Iterator
from contextlib import contextmanager

from .models import worktree_root


class LockError(RuntimeError):
    pass


@contextmanager
def worktree_lock(repo: str, root: str, name: str) -> Iterator[None]:
    """Hold an exclusive lock until the caller completes its operation."""
    try:
        import fcntl
    except ImportError:  # non-POSIX: locking unsupported, proceed unguarded
        yield
        return

    lock_dir = worktree_root(repo, root) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    # Deliberately not a `with`: the descriptor must stay open (holding the
    # flock) for the whole yielded block; the finally below closes it.
    fd = open(lock_path, "w")  # noqa: SIM115
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise LockError(
                    f"Another treebox is already working on '{name}'. "
                    "Wait for it to finish or use a different worktree."
                ) from exc
            raise
        yield
    finally:
        fd.close()

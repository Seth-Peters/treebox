"""Merge submodule contents without following worktree-controlled links.

Destination directories are opened once with O_NOFOLLOW. All descendants are
accessed relative to those descriptors. Files are copied into new inodes and
then renamed, so neither existing hardlinks nor raced symlinks redirect writes.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


class SubmoduleCopyError(RuntimeError):
    def __init__(self, path: Path, reason: str, *, conflict: bool = True) -> None:
        super().__init__(f"Cannot safely copy submodule at {path}: {reason}.")
        self.path = path
        self.conflict = conflict
        self.hint = (
            "Inspect this path and move any conflicting entry aside, then retry create. "
            "Treebox does not remove conflicting directories."
        )


@dataclass
class _Directory:
    fd: int
    path: Path
    parent: _Directory | None = None
    name: str | None = None

    def check(self) -> None:
        """Refuse a changed directory chain before the next write."""
        if self.parent is not None:
            self.parent.check()
        current = os.stat(
            self.name if self.name is not None else self.path,
            dir_fd=self.parent.fd if self.parent is not None else None,
            follow_symlinks=False,
        )
        opened = os.fstat(self.fd)
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise SubmoduleCopyError(self.path, "directory changed during copying")


@contextlib.contextmanager
def _directory(
    path: Path, *, parent: _Directory | None = None, create: bool = False
) -> Iterator[_Directory]:
    name = path.name if parent is not None else None
    target = name if name is not None else path
    parent_fd = parent.fd if parent is not None else None
    if parent is not None:
        parent.check()
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(target, dir_fd=parent_fd)
    try:
        fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise SubmoduleCopyError(path, str(exc), conflict=exc.errno in _CONFLICT_ERRNOS) from exc
    try:
        directory = _Directory(fd, path, parent, name)
        directory.check()
        yield directory
    finally:
        os.close(fd)


_CONFLICT_ERRNOS = {errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENOENT, errno.EEXIST}


def _copy_leaf(source: _Directory, dest: _Directory, name: str, mode: int) -> None:
    """Never open an existing destination inode for writing, even on retries."""
    dest.check()
    temporary = f".treebox-copy-{secrets.token_hex(16)}"
    created = False
    try:
        if stat.S_ISLNK(mode):
            link = os.readlink(name, dir_fd=source.fd)
            os.symlink(link, temporary, dir_fd=dest.fd)
            created = True
        elif stat.S_ISREG(mode):
            source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source.fd)
            with os.fdopen(source_fd, "rb") as src:
                metadata = os.fstat(src.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise SubmoduleCopyError(source.path / name, "source is no longer a file")
                dest.check()
                dest_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=dest.fd,
                )
                created = True
                with os.fdopen(dest_fd, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fchmod(dst.fileno(), stat.S_IMODE(metadata.st_mode))
                    os.utime(dst.fileno(), ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        else:
            raise SubmoduleCopyError(source.path / name, "source is not a regular file or link")
        dest.check()
        os.replace(temporary, name, src_dir_fd=dest.fd, dst_dir_fd=dest.fd)
        created = False
        dest.check()
    finally:
        if created:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=dest.fd)


def _copy_contents(source: _Directory, dest: _Directory) -> None:
    dest.check()
    final_mode = stat.S_IMODE(os.fstat(dest.fd).st_mode)
    writable_mode = final_mode | stat.S_IRWXU
    if writable_mode != final_mode:
        os.fchmod(dest.fd, writable_mode)
    try:
        for name in os.listdir(source.fd):
            if name == ".git":
                continue
            try:
                metadata = os.stat(name, dir_fd=source.fd, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    with (
                        _directory(source.path / name, parent=source) as child_source,
                        _directory(dest.path / name, parent=dest, create=True) as child_dest,
                    ):
                        _copy_contents(child_source, child_dest)
                else:
                    _copy_leaf(source, dest, name, metadata.st_mode)
            except OSError as exc:
                raise SubmoduleCopyError(
                    dest.path / name, str(exc), conflict=exc.errno in _CONFLICT_ERRNOS
                ) from exc
        dest.check()
        metadata = os.fstat(source.fd)
        os.utime(dest.fd, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        final_mode = stat.S_IMODE(metadata.st_mode)
    finally:
        os.fchmod(dest.fd, final_mode)


def copy_submodule(repo: Path, worktree: Path, relative: Path) -> None:
    """Copy a validated repo-relative submodule, retaining extra destination files."""
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"..", ".git"} for part in relative.parts)
    ):
        raise SubmoduleCopyError(worktree / relative, "invalid submodule path")
    try:
        with contextlib.ExitStack() as stack:
            source = stack.enter_context(_directory(repo))
            dest = stack.enter_context(_directory(worktree))
            for name in relative.parts:
                source = stack.enter_context(_directory(source.path / name, parent=source))
                dest = stack.enter_context(_directory(dest.path / name, parent=dest, create=True))
            _copy_contents(source, dest)
    except OSError as exc:
        raise SubmoduleCopyError(
            worktree / relative, str(exc), conflict=exc.errno in _CONFLICT_ERRNOS
        ) from exc

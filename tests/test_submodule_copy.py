"""Submodule copies must not modify files through worktree-controlled links."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox.cli import app
from treebox.submodule_copy import SubmoduleCopyError, copy_submodule


def _create_link_checkout(repo: Path, tmp_path: Path, link_path: str, target: Path) -> None:
    env = dict(
        os.environ,
        GIT_INDEX_FILE=str(tmp_path / "attack-index"),
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@e",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@e",
    )

    def git(*args: str, data: str | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            input=data,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    git("read-tree", "main")
    git("update-index", "--force-remove", "sub")
    link = git("hash-object", "-w", "--stdin", data=str(target))
    git("update-index", "--add", "--cacheinfo", f"120000,{link},{link_path}")
    commit = git("commit-tree", git("write-tree"), "-p", "main", data="unsafe checkout\n")
    git("branch", "copy-target", commit)


@pytest.mark.parametrize(
    ("relative", "link_path"),
    [
        ("sub", "sub/inner"),
        ("sub", "sub"),
        ("vendor/lib", "vendor"),
        ("vendor/lib", "vendor/lib"),
    ],
)
def test_checkout_submodule_link_cannot_overwrite_host_file(
    repo: Path, tmp_path: Path, hermetic_config: Path, relative: str, link_path: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_text("host data\n")
    if relative != "sub":
        (repo / relative).parent.mkdir(parents=True)
        (repo / "sub").rename(repo / relative)
        modules = repo / ".gitmodules"
        modules.write_text(modules.read_text().replace("path = sub", f"path = {relative}"))
    (repo / relative / "inner").mkdir()
    (repo / relative / "inner" / "victim").write_text("source data\n")
    _create_link_checkout(repo, tmp_path, link_path, outside)

    root = tmp_path / "worktrees"
    args = ["create", "--checkout", "copy-target", "--repo", str(repo), "--root", str(root)]
    result = CliRunner().invoke(app, [*args, "--json"], catch_exceptions=False)
    assert victim.read_text() == "host data\n"
    assert result.exit_code == 5, result.output
    assert result.stdout == ""
    error = json.loads(result.stderr)
    assert error["schemaVersion"] == 1
    assert error["error"]["code"] == "DESTINATION_CONFLICT"
    assert error["error"]["path"] == str(root / "copy-target" / link_path)
    assert "hint" in error["error"]

    # Failed copies remain retryable after the operator moves the unsafe link.
    dest = root / "copy-target" / link_path
    dest.unlink()
    retry = CliRunner().invoke(app, [*args, "--json"], catch_exceptions=False)
    assert retry.exit_code == 0, retry.output
    assert (root / "copy-target" / relative / "inner" / "victim").read_text() == "source data\n"
    assert victim.read_text() == "host data\n"


def test_create_retry_preserves_readonly_submodule_directories(
    repo: Path, tmp_path: Path, hermetic_config: Path
) -> None:
    source = repo / "sub"
    nested = source / "nested"
    nested.mkdir()
    (nested / "run").write_text("source data\n")
    (nested / "run").chmod(0o751)
    (repo / "later" / "inner").mkdir(parents=True)
    (repo / "later" / "inner" / "victim").write_text("later data\n")
    with (repo / ".gitmodules").open("a") as modules:
        modules.write('\n[submodule "later"]\n\tpath = later\n\turl = unused\n')
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim").write_text("host data\n")
    _create_link_checkout(repo, tmp_path, "later/inner", outside)
    root = tmp_path / "worktrees"
    dest = root / "copy-target"
    args = ["create", "--checkout", "copy-target", "--repo", str(repo), "--root", str(root)]
    source.chmod(0o555)
    nested.chmod(0o550)
    try:
        result = CliRunner().invoke(app, [*args, "--json"], catch_exceptions=False)
        assert result.exit_code == 5, result.output
        error = json.loads(result.stderr)["error"]
        assert error["code"] == "DESTINATION_CONFLICT"
        assert error["path"] == str(dest / "later" / "inner")
        assert (dest / "sub").stat().st_mode & 0o777 == 0o555
        assert (dest / "sub" / "nested").stat().st_mode & 0o777 == 0o550

        (dest / "later" / "inner").unlink()
        retry = CliRunner().invoke(app, [*args, "--json"], catch_exceptions=False)
        assert retry.exit_code == 0, retry.output
        enter = CliRunner().invoke(
            app,
            ["enter", "copy-target", "--repo", str(repo), "--root", str(root), "--json"],
            catch_exceptions=False,
        )
        assert enter.exit_code == 0, enter.output
        assert (dest / "sub" / "nested" / "run").read_text() == "source data\n"
        assert (dest / "sub" / "nested" / "run").stat().st_mode & 0o777 == 0o751
        assert (dest / "later" / "inner" / "victim").read_text() == "later data\n"
        assert (outside / "victim").read_text() == "host data\n"
        for path, mode in [(source, 0o555), (nested, 0o550)]:
            assert path.stat().st_mode & 0o777 == mode
            assert (dest / path.relative_to(repo)).stat().st_mode & 0o777 == mode
    finally:
        for path in [source, nested, dest / "sub", dest / "sub" / "nested"]:
            if path.is_dir():
                path.chmod(0o755)


@pytest.mark.parametrize("relative", ["sub", "sub/nested"])
def test_readonly_directory_modes_are_restored_after_conflict_and_retry(
    tmp_path: Path, relative: str
) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    source, dest = repo / relative, worktree / relative
    source.mkdir(parents=True)
    (source / "entry").write_text("source data")
    (dest / "entry").mkdir(parents=True)
    (dest / "entry" / "keep").write_text("user data")
    directories = {source: 0o555, repo / "sub": 0o555, dest: 0o500, worktree / "sub": 0o500}
    for path, mode in directories.items():
        path.chmod(mode)
    try:
        with pytest.raises(SubmoduleCopyError) as error:
            copy_submodule(repo, worktree, Path("sub"))
        assert error.value.conflict
        assert error.value.path == dest / "entry"
        for path, mode in directories.items():
            assert path.stat().st_mode & 0o777 == mode
        assert (dest / "entry" / "keep").read_text() == "user data"
        assert not list(worktree.rglob(".treebox-copy-*"))

        dest.chmod(0o700)
        (dest / "entry").rename(dest / "extra")
        dest.chmod(0o500)
        for _ in range(2):
            copy_submodule(repo, worktree, Path("sub"))
            assert (dest / "entry").read_text() == "source data"
            assert (dest / "extra" / "keep").read_text() == "user data"
            for path in directories:
                assert path.stat().st_mode & 0o777 == 0o555
    finally:
        for path in directories:
            path.chmod(0o755)


@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
def test_existing_leaf_links_are_replaced_without_writing_their_targets(
    tmp_path: Path, kind: str
) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    source, dest = repo / "sub", worktree / "sub"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "file").write_text("source data")
    (source / "file").chmod(0o751)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("host data")
    if kind == "symlink":
        (dest / "file").symlink_to(sentinel)
    else:
        os.link(sentinel, dest / "file")
    copy_submodule(repo, worktree, Path("sub"))
    assert sentinel.read_text() == "host data"
    assert (dest / "file").read_text() == "source data"
    assert not (dest / "file").is_symlink()
    assert (dest / "file").stat().st_ino != sentinel.stat().st_ino
    assert (dest / "file").stat().st_mode & 0o777 == 0o751


def test_merge_preserves_source_links_destination_extras_and_excludes_git(tmp_path: Path) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    source, dest = repo / "vendor" / "sub", worktree / "vendor" / "sub"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "file").write_text("source")
    (source / "link").symlink_to("file")
    (source / "dangling").symlink_to("absent")
    (source / ".git").write_text("gitdir: host metadata")
    (source / "nested" / ".git").mkdir(parents=True)
    (source / "nested" / ".git" / "config").write_text("host config")
    (dest / "keep").write_text("user data")
    for _ in range(2):
        copy_submodule(repo, worktree, Path("vendor/sub"))
        assert (dest / "keep").read_text() == "user data"
        assert (dest / "link").readlink() == Path("file")
        assert (dest / "dangling").readlink() == Path("absent")
        assert not (dest / ".git").exists()
        assert not (dest / "nested" / ".git").exists()
        assert not list(dest.rglob(".treebox-copy-*"))


@pytest.mark.parametrize("source_kind", ["file", "link"])
def test_nonempty_destination_directory_is_not_deleted(tmp_path: Path, source_kind: str) -> None:
    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    source, dest = repo / "sub", worktree / "sub"
    source.mkdir(parents=True)
    (dest / "entry").mkdir(parents=True)
    sentinel = dest / "entry" / "keep"
    sentinel.write_text("user data")
    if source_kind == "file":
        (source / "entry").write_text("source")
    else:
        (source / "entry").symlink_to("other")
    with pytest.raises(SubmoduleCopyError) as error:
        copy_submodule(repo, worktree, Path("sub"))
    assert error.value.conflict
    assert error.value.path == dest / "entry"
    assert sentinel.read_text() == "user data"
    assert not list(dest.glob(".treebox-copy-*"))


@pytest.mark.parametrize("timing", ["before-open", "after-open"])
def test_directory_swap_does_not_redirect_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timing: str
) -> None:
    from treebox import submodule_copy

    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    source, dest = repo / "sub", worktree / "sub"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file").write_text("source")
    (dest / "nested").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("host data")
    original_open = os.open
    swapped = False

    def race_open(path, flags, *args, **kwargs):
        nonlocal swapped
        is_dest = (
            path == "nested"
            and kwargs.get("dir_fd") is not None
            and os.fstat(kwargs["dir_fd"]).st_ino == dest.stat().st_ino
        )
        if is_dest and not swapped:
            swapped = True
            if timing == "after-open":
                fd = original_open(path, flags, *args, **kwargs)
            (dest / "nested").rename(dest / "original")
            (dest / "nested").symlink_to(outside)
            if timing == "after-open":
                return fd
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(submodule_copy.os, "open", race_open)
    with pytest.raises(SubmoduleCopyError):
        copy_submodule(repo, worktree, Path("sub"))
    assert swapped
    assert (outside / "file").read_text() == "host data"
    assert not (dest / "original" / "file").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_leaf_swap_before_publication_cannot_modify_external_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from treebox import submodule_copy

    repo, worktree = tmp_path / "repo", tmp_path / "worktree"
    source, dest = repo / "sub", worktree / "sub"
    source.mkdir(parents=True)
    dest.mkdir(parents=True)
    (source / "file").write_text("source")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("host data")
    replace = os.replace

    def race_replace(src, dst, **kwargs):
        if dst == "file":
            if kind == "symlink":
                (dest / "file").symlink_to(sentinel)
            else:
                os.link(sentinel, dest / "file")
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(submodule_copy.os, "replace", race_replace)
    copy_submodule(repo, worktree, Path("sub"))
    assert sentinel.read_text() == "host data"
    assert (dest / "file").read_text() == "source"

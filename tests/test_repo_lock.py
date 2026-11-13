"""Creates and teardowns of *different* worktrees share one repository.

Each test pins one mechanism of the parallel-create race deterministically -
no timing luck: the state another process would have produced at the unlucky
instant is put in place by hand.
"""

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import git, locking, record
from treebox.cli import app


def _run(args: list[str]):
    return CliRunner().invoke(app, args, catch_exceptions=False)


def _git(cwd: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _branch_exists(repo: Path, name: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{name}").returncode == 0


@pytest.fixture
def root(tmp_path: Path) -> str:
    return str(tmp_path / "wts")


@pytest.fixture
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "_CONFIG_LOCK_BACKOFF", 0.0)


# --- the repository lock itself ------------------------------------------------


def test_repo_lock_is_keyed_by_the_repository_not_the_path(repo: Path, root: str, hermetic_config):
    """The main checkout and a linked worktree are the same repository: a
    holder reached through one path excludes a caller arriving by the other."""
    assert _run(["create", "one", "--repo", str(repo), "--root", root, "--json"]).exit_code == 0
    worktree = Path(root) / "one"

    with (
        locking.repo_lock(repo),
        pytest.raises(locking.LockError),
        locking.repo_lock(worktree, timeout=0.2),
    ):
        pytest.fail("a second holder got the repository lock")

    with locking.repo_lock(worktree):  # released: the next caller gets it
        pass


def test_repo_lock_waits_for_the_holder_instead_of_failing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    """Queueing is the point: a second caller gets the lock when the first
    lets go, and is told once that it is waiting."""
    monkeypatch.setattr(locking, "_ANNOUNCE_AFTER", 0.05)
    order: list[str] = []
    announced: list[bool] = []
    holding = threading.Event()

    def holder() -> None:
        with locking.repo_lock(repo):
            order.append("first in")
            holding.set()
            time.sleep(0.4)
            order.append("first out")

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert holding.wait(timeout=10), "the first holder never got the lock"
        with locking.repo_lock(repo, on_wait=lambda: announced.append(True)):
            order.append("second in")
    finally:
        thread.join(timeout=10)

    assert order == ["first in", "first out", "second in"]
    assert announced == [True]


# --- create ---------------------------------------------------------------------


def test_create_queues_behind_the_repository_lock(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """`create` takes the repository lock before it writes any shared git
    state. With the lock held and no patience, it gives up as LOCK_HELD and
    has created nothing - not a branch, not a directory."""
    monkeypatch.setattr(locking, "REPO_LOCK_TIMEOUT", 0.2)
    base = ["--repo", str(repo), "--root", root, "--json"]

    with locking.repo_lock(repo):
        blocked = _run(["create", "queued", *base])
    assert blocked.exit_code == 5
    assert json.loads(blocked.stderr)["error"]["code"] == "LOCK_HELD"
    assert not _branch_exists(repo, "queued")
    assert not (Path(root) / "queued").exists()
    # The record is written before the repository lock is taken, and a create
    # that never got the lock takes it back.
    assert not record.exists(Path(root) / "queued")

    released = _run(["create", "queued", *base])
    assert released.exit_code == 0, released.output


def test_failed_worktree_add_leaves_no_orphan_branch_and_a_retry_succeeds(
    repo: Path, root: str, hermetic_config, fast_retries
):
    """The dead-end this race used to leave behind.

    `git worktree add -b` creates the branch and *then* writes its upstream
    section to the shared `.git/config`. git takes that config lock without
    waiting, so a held lock makes it exit non-zero with the branch already
    created: an orphan with no worktree, on which a retried `create` reported
    BRANCH_EXISTS and `enter` reported NOT_FOUND. A failed add now takes the
    branch back, so the same command simply works once the lock is gone.
    """
    base = ["--repo", str(repo), "--root", root, "--json"]
    config_lock = repo / ".git" / "config.lock"
    config_lock.touch()  # what another git process holds while it writes config

    failed = _run(["create", "raced", *base])
    assert failed.exit_code == 1
    message = json.loads(failed.stderr)["error"]["message"]
    assert "could not lock config file" in message
    assert "config.lock" in message  # says how to clear a stale one
    assert not _branch_exists(repo, "raced")
    assert not (Path(root) / "raced").exists()
    assert "raced" not in _git(repo, "worktree", "list").stdout
    assert not record.exists(Path(root) / "raced")

    config_lock.unlink()
    retried = _run(["create", "raced", *base])
    assert retried.exit_code == 0, retried.output
    assert json.loads(retried.stdout)["created"] is True


def test_create_outlasts_a_config_lock_that_another_writer_releases(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """A live writer (an agent's `git push -u` in another worktree) holds the
    config lock for an instant. The add is retried and succeeds - with no
    branch left over from the attempt that lost."""
    config_lock = repo / ".git" / "config.lock"
    config_lock.touch()
    attempts: list[bool] = []

    def writer_finishes(_seconds: float) -> None:
        attempts.append(_branch_exists(repo, "contended"))
        config_lock.unlink(missing_ok=True)

    monkeypatch.setattr(git.time, "sleep", writer_finishes)

    created = _run(["create", "contended", "--repo", str(repo), "--root", root, "--json"])

    assert created.exit_code == 0, created.output
    assert attempts == [False]  # one lost attempt, already rolled back when we waited
    assert _git(Path(root) / "contended", "branch", "--show-current").stdout.strip() == "contended"


@pytest.mark.parametrize("kind", ["local", "new"])
def test_a_failed_add_never_deletes_a_branch_it_did_not_create(repo: Path, tmp_path: Path, kind):
    """The rollback is for the branch a failed add created, nothing else. A
    branch that was already there - checked out by plan ("local"), or made by
    someone else between planning and adding ("new") - stays where it was,
    even though it sits on the very commit a new branch would have started at."""
    assert _git(repo, "branch", "keep-me", "origin/main").returncode == 0
    tip = _git(repo, "rev-parse", "keep-me").stdout.strip()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "file").write_text("makes `git worktree add` refuse this path\n")
    plan = git.BranchPlan(kind, "keep-me", None if kind == "local" else "origin/main")

    with pytest.raises(git.GitError):
        git.worktree_add(repo, occupied, plan)

    assert _git(repo, "rev-parse", "keep-me").stdout.strip() == tip


def test_the_shared_config_is_written_once_per_repository(repo: Path, root: str, hermetic_config):
    """`extensions.worktreeConfig` lives in the config every worktree shares.
    Once it is on, turning it on again writes nothing - so it cannot lose the
    config lock, and a second worktree's push guard installs under a held one."""
    assert _run(["create", "first", "--repo", str(repo), "--root", root, "--json"]).exit_code == 0
    assert _git(repo, "config", "--get", "extensions.worktreeConfig").stdout.strip() == "true"
    before = (repo / ".git" / "config").read_bytes()

    (repo / ".git" / "config.lock").touch()
    try:
        git.enable_worktree_config(Path(root) / "first")  # must not raise
    finally:
        (repo / ".git" / "config.lock").unlink()

    assert (repo / ".git" / "config").read_bytes() == before


# --- teardown -------------------------------------------------------------------


def test_teardown_queues_behind_the_repository_lock(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """Removing a worktree rewrites the same shared state a create does, so it
    takes the same lock: held, the teardown gives up and removes nothing."""
    base = ["--repo", str(repo), "--root", root]
    assert _run(["create", "doomed", *base, "--json"]).exit_code == 0
    monkeypatch.setattr(locking, "REPO_LOCK_TIMEOUT", 0.2)

    with locking.repo_lock(repo):
        blocked = _run(["teardown", "doomed", *base, "--force", "--json"])
    assert blocked.exit_code == 5
    assert json.loads(blocked.stderr)["error"]["code"] == "LOCK_HELD"
    assert (Path(root) / "doomed").is_dir()
    # The record goes last, after the locked git steps, so it is still there.
    assert record.exists(Path(root) / "doomed")

    removed = _run(["teardown", "doomed", *base, "--force", "--delete-branch", "--json"])
    assert removed.exit_code == 0, removed.output
    result = json.loads(removed.stdout)["worktrees"][0]
    assert result["removed"] is True
    assert result["branch_deleted"] is True
    assert not record.exists(Path(root) / "doomed")


def test_create_writes_the_record_before_it_takes_the_repository_lock(
    repo: Path, root: str, hermetic_config, monkeypatch: pytest.MonkeyPatch
):
    """Lock order is worktree, then repo. The host-owned record belongs to the
    worktree, so `create` writes it under the worktree lock alone, never while
    it holds the repository lock."""
    real_save = record.save
    held_during_save: list[bool] = []

    def save_and_probe(rec: record.WorktreeRecord) -> None:
        try:
            with locking.repo_lock(repo, timeout=0.05):
                held_during_save.append(False)
        except locking.LockError:
            held_during_save.append(True)
        real_save(rec)

    monkeypatch.setattr(record, "save", save_and_probe)
    created = _run(["create", "ordered", "--repo", str(repo), "--root", root, "--json"])
    assert created.exit_code == 0, created.output
    assert held_during_save and not any(held_during_save)

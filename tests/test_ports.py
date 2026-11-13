"""The machine-wide port-block allocator, driven through the CLI (issue #66).

Concurrency and crash tests run the real ``treebox`` in separate processes
against one scratch treebox home (the autouse ``isolated_treebox_home``
fixture): real flock contention, and a real process death (``os._exit``, no
cleanup handlers) at the exact point between two writes.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from itertools import combinations
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import locking, ports, record
from treebox.cli import EXIT_CONFLICT, EXIT_ERROR, EXIT_USAGE, app
from treebox.models import PortBlock

runner = CliRunner()

# Runs the CLI with one module function replaced by an immediate process exit,
# so the process dies exactly where that function would have run.
_CRASH_AT = """
import os, sys
from treebox import {module}
{module}.{function} = lambda *a, **k: os._exit(137)
from treebox.cli import app
sys.argv[0] = "treebox"
app()
"""


def _run(args: list[str]):
    return runner.invoke(app, args, catch_exceptions=False)


def _treebox(
    *args: str, crash_at: tuple[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run treebox in its own process (inherits TREEBOX_HOME / TREEBOX_CONFIG)."""
    if crash_at is None:
        argv = [sys.executable, "-c", "from treebox.cli import app; app()", *args]
    else:
        module, function = crash_at
        argv = [sys.executable, "-c", _CRASH_AT.format(module=module, function=function), *args]
    return subprocess.run(argv, capture_output=True, text=True)


def _ledger(home: Path) -> list[dict[str, object]]:
    path = home / "ports.json"
    return json.loads(path.read_text())["blocks"] if path.exists() else []


@pytest.fixture
def one_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config whose range holds exactly one block of 10 ports."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('setup_hook = ["echo ran >> setup.log"]\n[ports]\nstart = 21000\nend = 21009\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    return cfg


def _create(repo: Path, root: str, name: str, *extra: str):
    return _run(["create", name, "--repo", str(repo), "--root", root, "--json", *extra])


def test_concurrent_creates_get_distinct_ids_and_non_overlapping_blocks(
    repo: Path, tmp_path: Path, hermetic_config: Path, isolated_treebox_home: Path
) -> None:
    """Eight real processes at once across eight repos, each name used in
    four repos: every create succeeds with its own id and a block no other
    worktree on the machine shares.

    One create per repo on purpose: concurrent creates *inside* one repo race
    on git's own config lock (``git worktree add`` writes the upstream to the
    shared ``.git/config``), which the repository lock of #55 serializes. The
    port allocator is machine-wide, so different repos are its real test."""
    repos = [repo]
    for i in range(1, 8):
        clone = tmp_path / f"clone{i}"
        subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(clone)], check=True)
        repos.append(clone)
    jobs = [
        (repo_dir, tmp_path / f"wts{i}", "api" if i % 2 else "web")
        for i, repo_dir in enumerate(repos)
    ]
    # Hold the ledger lock while the processes start, so they all queue on it
    # and then race for it at once: real contention, not lucky timing.
    with locking.ports_lock(isolated_treebox_home / ports.LOCK_FILE):
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from treebox.cli import app; app()",
                    *("create", name, "--repo", str(repo_dir), "--root", str(root), "--json"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for repo_dir, root, name in jobs
        ]
        # Every process got as far as its worktree lock (past preflight)...
        deadline = time.monotonic() + 60
        while not all((root / ".locks" / f"{name}.lock").exists() for _, root, name in jobs):
            assert time.monotonic() < deadline, "the creates did not start"
            time.sleep(0.05)
        time.sleep(1.0)
        # ...and none can finish without the ledger lock.
        assert all(proc.poll() is None for proc in procs)
    results = [(*proc.communicate(timeout=120), proc.returncode) for proc in procs]
    for _out, err, code in results:
        assert code == 0, err
    payloads = [json.loads(out) for out, _, _ in results]

    assert len({p["id"] for p in payloads}) == len(jobs)
    blocks = [PortBlock(p["port_base"], p["port_count"]) for p in payloads]
    assert all(not a.overlaps(b) for a, b in combinations(blocks, 2))
    # The ledger is the one source of truth, and each record copy agrees with it.
    ledger = {
        e["worktree"]: PortBlock(e["base"], e["count"]) for e in _ledger(isolated_treebox_home)
    }
    assert ledger == {p["worktree_path"]: b for p, b in zip(payloads, blocks, strict=True)}
    for p, block in zip(payloads, blocks, strict=True):
        assert record.load(Path(p["worktree_path"])).ports == block


def test_dry_run_reserves_nothing(
    repo: Path, tmp_path: Path, hermetic_config: Path, isolated_treebox_home: Path
) -> None:
    res = _run(
        [
            "create",
            "plan",
            "--repo",
            str(repo),
            "--root",
            str(tmp_path / "wts"),
            "--dry-run",
            "--json",
        ]
    )
    assert res.exit_code == 0, res.output
    assert any("reserve port block 20000-20009" in c for c in json.loads(res.stdout)["commands"])
    assert not isolated_treebox_home.exists()


def test_crash_between_ledger_and_record_is_repaired_by_the_retry(
    repo: Path, tmp_path: Path, hermetic_config: Path, isolated_treebox_home: Path
) -> None:
    """The process dies after the ledger write, before the record copy: the
    entry stays (no leak: the retry of the same create gets the same block)."""
    root = str(tmp_path / "wts")
    crashed = _treebox(
        "create", "crashy", "--repo", str(repo), "--root", root, "--json",
        crash_at=("record", "save"),
    )  # fmt: skip
    assert crashed.returncode == 137
    wt = Path(root) / "crashy"
    (entry,) = _ledger(isolated_treebox_home)
    assert entry["worktree"] == str(wt)
    assert not wt.exists() and not record.exists(wt)

    retry = _create(repo, root, "crashy")
    assert retry.exit_code == 0, retry.output
    assert json.loads(retry.stdout)["port_base"] == entry["base"]
    assert _ledger(isolated_treebox_home) == [entry]
    assert record.load(wt).ports == PortBlock(entry["base"], entry["count"])  # type: ignore[arg-type]


def test_abandoned_entry_from_a_crash_is_reclaimed(
    repo: Path, tmp_path: Path, one_block: Path, isolated_treebox_home: Path
) -> None:
    """Nobody retries the crashed create: its entry has no directory, no git
    registration and no lock holder, so the next allocation reclaims it."""
    root = str(tmp_path / "wts")
    crashed = _treebox(
        "create", "gone", "--repo", str(repo), "--root", root, "--json",
        crash_at=("record", "save"),
    )  # fmt: skip
    assert crashed.returncode == 137
    assert len(_ledger(isolated_treebox_home)) == 1

    res = _create(repo, root, "next")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["port_base"] == 21000
    (entry,) = _ledger(isolated_treebox_home)
    assert entry["worktree"] == str(Path(root) / "next")


def test_crash_between_teardown_and_release_is_reclaimed(
    repo: Path, tmp_path: Path, one_block: Path, isolated_treebox_home: Path
) -> None:
    """Teardown dies after the record is gone, before the release: the entry
    is stale (the worktree is gone) and the next create takes the block."""
    root = str(tmp_path / "wts")
    assert _create(repo, root, "first").exit_code == 0
    crashed = _treebox(
        "teardown", "first", "--repo", str(repo), "--root", root, "--force",
        crash_at=("ports", "release"),
    )  # fmt: skip
    assert crashed.returncode == 137
    wt = Path(root) / "first"
    assert not wt.exists() and not record.exists(wt)
    assert len(_ledger(isolated_treebox_home)) == 1

    res = _create(repo, root, "second")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["port_base"] == 21000


def test_teardown_releases_the_block_for_the_next_create(
    repo: Path, tmp_path: Path, hermetic_config: Path, isolated_treebox_home: Path
) -> None:
    root = str(tmp_path / "wts")
    first = json.loads(_create(repo, root, "first").stdout)
    second = json.loads(_create(repo, root, "second").stdout)
    assert (first["port_base"], second["port_base"]) == (20000, 20010)

    torn = _run(["teardown", "first", "--repo", str(repo), "--root", root, "--force"])
    assert torn.exit_code == 0, torn.output
    assert "released 20000-20009" in torn.stderr
    assert [e["base"] for e in _ledger(isolated_treebox_home)] == [20010]

    third = json.loads(_create(repo, root, "third").stdout)
    assert third["port_base"] == 20000  # the lowest free block


def test_failed_cleanup_keeps_the_block_until_the_retry_completes(
    repo: Path,
    tmp_path: Path,
    one_block: Path,
    isolated_treebox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete teardown (TEARDOWN_INCOMPLETE) keeps the worktree, its
    record and its block; only the completed retry releases it."""
    from treebox.runners.host import HostRunner

    root = str(tmp_path / "wts")
    assert _create(repo, root, "kept").exit_code == 0

    def boom(self, wt, *, reporter):
        raise RuntimeError("daemon exploded")

    with monkeypatch.context() as patched:
        patched.setattr(HostRunner, "teardown", boom)
        res = _run(["teardown", "kept", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert res.exit_code == EXIT_ERROR
    assert json.loads(res.stderr)["error"]["code"] == "TEARDOWN_INCOMPLETE"
    assert [e["base"] for e in _ledger(isolated_treebox_home)] == [21000]
    # The block is still taken: the one-block range is full.
    full = _create(repo, root, "other")
    assert full.exit_code == EXIT_CONFLICT
    assert json.loads(full.stderr)["error"]["code"] == "PORTS_EXHAUSTED"

    retry = _run(["teardown", "kept", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert retry.exit_code == 0, retry.output
    assert _ledger(isolated_treebox_home) == []
    assert _create(repo, root, "other").exit_code == 0


def test_range_exhaustion_is_classified_before_anything_is_provisioned(
    repo: Path, tmp_path: Path, one_block: Path, isolated_treebox_home: Path
) -> None:
    root = str(tmp_path / "wts")
    assert _create(repo, root, "first").exit_code == 0

    for extra in ((), ("--dry-run",)):
        res = _create(repo, root, "second", *extra)
        assert res.exit_code == EXIT_CONFLICT
        err = json.loads(res.stderr)["error"]
        assert err["code"] == "PORTS_EXHAUSTED"
        assert "21000-21009" in err["message"]
        assert "[ports]" in err["hint"] and "teardown" in err["hint"]
        assert err["path"] == str(isolated_treebox_home / "ports.json")

    wt = Path(root) / "second"
    assert not wt.exists() and not record.exists(wt)
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "second"], capture_output=True, text=True
    )
    assert branches.stdout == ""
    assert len(_ledger(isolated_treebox_home)) == 1


@pytest.mark.parametrize(
    ("damage", "code"),
    [
        (lambda path: path.unlink(), "RECORD_MISSING"),
        # A record without the required ports key does not parse: there is
        # one schema version and no upgrade.
        (
            lambda path: path.write_text(
                json.dumps({k: v for k, v in json.loads(path.read_text()).items() if k != "ports"})
            ),
            "RECORD_INVALID",
        ),
    ],
    ids=["missing", "without-ports"],
)
def test_teardown_without_a_usable_record_releases_the_block(
    repo: Path,
    tmp_path: Path,
    hermetic_config: Path,
    isolated_treebox_home: Path,
    damage,
    code: str,
) -> None:
    """A worktree without a usable record cannot be entered, and a plain
    teardown removes it and releases the block the ledger holds for its path
    (the ledger, not the record, says which blocks are taken)."""
    root = str(tmp_path / "wts")
    made = json.loads(_create(repo, root, "gone").stdout)
    other = json.loads(_create(repo, root, "kept").stdout)
    damage(record.record_path(Path(root) / "gone"))

    entered = _run(["enter", "gone", "--repo", str(repo), "--root", root, "--json"])
    assert entered.exit_code == EXIT_CONFLICT
    assert json.loads(entered.stderr)["error"]["code"] == code

    torn = _run(["teardown", "gone", "--repo", str(repo), "--root", root, "--force", "--json"])
    assert torn.exit_code == 0, torn.stderr
    assert not (Path(root) / "gone").exists()
    assert [e["base"] for e in _ledger(isolated_treebox_home)] == [other["port_base"]]
    assert made["port_base"] != other["port_base"]


def test_a_lost_ledger_heals_from_the_record_copy(
    repo: Path, tmp_path: Path, hermetic_config: Path, isolated_treebox_home: Path
) -> None:
    """With the ledger deleted, enter takes the recorded block back (still
    free), so the worktree's services keep their ports."""
    root = str(tmp_path / "wts")
    _create(repo, root, "a")
    b = json.loads(_create(repo, root, "b").stdout)
    (isolated_treebox_home / "ports.json").unlink()
    entered = json.loads(_run(["enter", "b", "--repo", str(repo), "--root", root, "--json"]).stdout)
    assert entered["port_base"] == b["port_base"] == 20010
    assert [e["base"] for e in _ledger(isolated_treebox_home)] == [20010]


def test_an_invalid_ledger_is_a_classified_failure(
    repo: Path, tmp_path: Path, hermetic_config: Path, isolated_treebox_home: Path
) -> None:
    isolated_treebox_home.mkdir()
    (isolated_treebox_home / "ports.json").write_text("{not json")
    res = _create(repo, str(tmp_path / "wts"), "x")
    assert res.exit_code == EXIT_ERROR
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "PORT_LEDGER_INVALID"
    assert err["path"] == str(isolated_treebox_home / "ports.json")
    assert not (tmp_path / "wts" / "x").exists()


@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ("blocksize = 5", "unknown key 'ports.blocksize'"),
        ('start = "20000"', "ports.start must be an integer"),
        ("start = 80\nend = 90", "1024 <= start <= end <= 65535"),
        ("start = 30000\nend = 20000", "1024 <= start <= end <= 65535"),
        ("start = 20000\nend = 20004\nblock_size = 10", "ports.block_size"),
        ("block_size = 0", "ports.block_size"),
    ],
)
def test_ports_config_is_validated(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str, reason: str
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"[ports]\n{table}\n")
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    res = _create(repo, str(tmp_path / "wts"), "x")
    assert res.exit_code == EXIT_USAGE
    err = json.loads(res.stderr)["error"]
    assert err["code"] == "INVALID_CONFIG"
    assert reason in err["message"]


def test_ports_lock_waits_then_fails_as_lock_held(tmp_path: Path) -> None:
    """The ledger lock queues; a holder that never lets go becomes LOCK_HELD
    with a hint, never a hang."""
    from treebox.cli import _classify

    lock = tmp_path / "ports.lock"
    lock.touch()
    with open(lock) as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(locking.LockError) as exc, locking.ports_lock(lock, timeout=0.2):
            pass
    info = _classify(exc.value)
    assert (info.exit_code, info.error_code) == (EXIT_CONFLICT, "LOCK_HELD")
    assert info.hint and "lsof" in info.hint


def test_stale_check_never_reclaims_a_block_in_use(tmp_path: Path, repo: Path) -> None:
    """Directory present, git registration present, or the worktree lock held:
    each alone keeps the block."""
    gone = tmp_path / "wts" / "gone"
    entry = ports._Entry(PortBlock(20000, 10), str(gone), str(repo))
    assert ports._stale(entry)
    with locking.worktree_lock(gone):
        assert not ports._stale(entry)
    gone.mkdir(parents=True)
    assert not ports._stale(entry)
    os.rmdir(gone)
    missing_repo = ports._Entry(PortBlock(20000, 10), str(gone), str(tmp_path / "no-repo"))
    assert ports._stale(missing_repo)

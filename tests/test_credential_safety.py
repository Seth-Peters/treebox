"""Exercise entry credential refresh with sandbox-controlled destinations."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import state
from treebox.cli import app
from treebox.config import Config
from treebox.harnesses import Harness
from treebox.models import Worktree
from treebox.output import Reporter
from treebox.runners.docker import DockerRunner


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_docker_refresh_does_not_overwrite_link_target(tmp_path, monkeypatch, link):
    live = tmp_path / "live"
    live.mkdir()
    (live / "auth.json").write_text("new synthetic token")
    monkeypatch.setattr(Harness, "credential_path", lambda self: live)
    runner = DockerRunner(Config(isolation="docker"))
    wt = Worktree(str(tmp_path), "test", "test", "main", tmp_path / "trees" / "test")
    staged = runner._creds_dir(wt) / "codex"
    staged.mkdir(parents=True)
    victim = tmp_path / "host-file"
    victim.write_text("keep me")
    victim.chmod(0o640)
    if link == "symlink":
        (staged / "auth.json").symlink_to(victim)
    else:
        (staged / "auth.json").hardlink_to(victim)

    runner.refresh(wt, reporter=Reporter(quiet=True))

    assert victim.read_text() == "keep me"
    assert victim.stat().st_mode & 0o777 == 0o640
    assert (staged / "auth.json").read_text() == "new synthetic token"
    assert not (staged / "auth.json").is_symlink()
    assert (staged / "auth.json").stat().st_mode & 0o777 == 0o600


def _fixture(tmp_path: Path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "auth.json").write_text("synthetic token")
    dest = tmp_path / "host-owned" / "mounted"
    dest.mkdir(parents=True)
    return Harness("test", ("test",), str(live), ("auth.json",)), live, dest


@pytest.mark.parametrize("logged_in", [True, False])
def test_credential_directory_conflict_preserves_contents(tmp_path, logged_in):
    harness, live, dest = _fixture(tmp_path)
    conflict = dest / "auth.json"
    conflict.mkdir()
    (conflict / "keep").write_text("user data")
    if not logged_in:
        (live / "auth.json").unlink()
    with pytest.raises(RuntimeError, match="Cannot safely refresh credentials"):
        harness.stage_credentials(dest)
    assert (conflict / "keep").read_text() == "user data"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["mounted"]


def test_credential_logout_unlinks_broken_symlink(tmp_path):
    harness, live, dest = _fixture(tmp_path)
    (live / "auth.json").unlink()
    (dest / "auth.json").symlink_to(tmp_path / "absent")
    harness.stage_credentials(dest)
    assert not (dest / "auth.json").is_symlink()


def test_credential_refresh_rejects_symlink_mount_root(tmp_path):
    harness, _, dest = _fixture(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    dest.rmdir()
    dest.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="Cannot safely refresh credentials"):
        harness.stage_credentials(dest)
    assert list(target.iterdir()) == []


def test_credential_refresh_replaces_raced_destination_without_following(tmp_path, monkeypatch):
    harness, _, dest = _fixture(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("unchanged")
    replace = os.replace

    def race(src, dst, **kwargs):
        (dest / "auth.json").symlink_to(victim)
        return replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", race)
    harness.stage_credentials(dest)
    assert victim.read_text() == "unchanged"
    assert (dest / "auth.json").read_text() == "synthetic token"


def test_credential_refresh_pins_directory_during_replacement(tmp_path, monkeypatch):
    harness, _, dest = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = dest.with_name("original")
    open_file = os.open

    def race(path, flags, *args, **kwargs):
        fd = open_file(path, flags, *args, **kwargs)
        if path == dest.name and flags & os.O_DIRECTORY:
            dest.rename(original)
            dest.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", race)
    harness.stage_credentials(dest)
    assert list(outside.iterdir()) == []
    assert (original / "auth.json").read_text() == "synthetic token"


@pytest.mark.parametrize("conflict", [False, True])
def test_enter_refresh_uses_safe_credentials_and_classified_errors(
    tmp_path, repo, hermetic_config, monkeypatch, conflict
):
    cli = CliRunner()
    root = tmp_path / "trees"
    args = ["safe-auth", "--repo", str(repo), "--root", str(root), "--json"]
    created = cli.invoke(app, ["create", *args], catch_exceptions=False)
    assert created.exit_code == 0, created.output
    wt_path = root / "safe-auth"
    recorded = state.load(wt_path)
    assert recorded is not None
    recorded.isolation = "docker"
    state.save(wt_path, recorded)
    # Only the engine is simulated. Entry, state, Git, and credential I/O are real.
    monkeypatch.setattr(DockerRunner, "preflight", lambda self, reporter: None)
    monkeypatch.setattr(DockerRunner, "prepare_entry", lambda self, wt: None)
    live = tmp_path / "login"
    live.mkdir()
    (live / "auth.json").write_text("synthetic token")
    monkeypatch.setattr(Harness, "credential_path", lambda self: live)
    staged = root / ".containers" / "safe-auth" / "credentials" / "codex"
    staged.mkdir(parents=True)
    victim = tmp_path / "host-data"
    victim.write_text("preserved")
    if conflict:
        (staged / "auth.json").mkdir()
        (staged / "auth.json" / "keep").write_text("preserved")
    else:
        (staged / "auth.json").symlink_to(victim)
    result = cli.invoke(app, ["enter", *args], catch_exceptions=False)
    assert victim.read_text() == "preserved"
    if conflict:
        assert result.exit_code == 1
        assert result.stdout == ""
        error = json.loads(result.stderr)
        assert error["schemaVersion"] == 1
        assert "Cannot safely refresh credentials" in error["error"]["message"]
        assert (staged / "auth.json" / "keep").read_text() == "preserved"
    else:
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["entry_command"][0] == "docker"
        assert (staged / "auth.json").read_text() == "synthetic token"
        (live / "auth.json").unlink()
        logout = cli.invoke(app, ["enter", *args], catch_exceptions=False)
        assert logout.exit_code == 0, logout.output
        assert not (staged / "auth.json").exists()

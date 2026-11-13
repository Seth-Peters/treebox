"""The macOS Keychain login source, through the ``Harness`` interface and the CLI.

Claude Code on macOS keeps its login in the Keychain item
``Claude Code-credentials``, not in ``~/.claude/.credentials.json``. A
sandbox still needs that file, so treebox reads the item and stages it as
the file. No test touches the real Keychain: ``security`` is the conftest
fake first on PATH, and the platform is monkeypatched.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import harnesses
from treebox.cli import app
from treebox.harnesses import get_harness

SECRET = "kc-access-token-do-not-leak"
PAYLOAD = json.dumps({"claudeAiOauth": {"accessToken": SECRET, "expiresAt": 1}})
KEYCHAIN = "macOS Keychain (Claude Code-credentials)"


def _fake(tool: Path, body: str) -> Path:
    """Make the fake ``security`` run ``body``; return its call log."""
    calls = tool.with_name("calls.log")
    tool.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\n{body}\n')
    return calls


def _item(tool: Path) -> Path:
    """The fake holds the Keychain item of user ``kc-user``: it prints the
    payload only for the exact read treebox makes (that user's item, as
    Claude Code reads it), and only with no terminal on stdin."""
    return _fake(
        tool,
        '[ $# -eq 6 ] && [ "$1" = find-generic-password ] && [ "$2" = -a ] && [ "$3" = kc-user ] '
        '&& [ "$4" = -s ] && [ "$5" = \'Claude Code-credentials\' ] && [ "$6" = -w ] || exit 44\n'
        "[ -t 0 ] && exit 98\n"
        f"cat >/dev/null\nprintf '%s\\n' '{PAYLOAD}'",
    )


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harnesses.sys, "platform", "darwin")
    for var in ("LOGNAME", "USER"):
        monkeypatch.setenv(var, "kc-user")


@pytest.fixture
def claude_dir(scratch_home: Path) -> Path:
    """The live login dir with settings only: no login file."""
    live = scratch_home / ".claude"
    live.mkdir()
    (live / "settings.json").write_text("{}\n")
    return live


def test_login_file_wins_and_the_keychain_is_never_read(macos, claude_dir, keychain, tmp_path):
    calls = _item(keychain)
    (claude_dir / ".credentials.json").write_text('{"from": "file"}')
    claude = get_harness("claude")
    assert claude.login() == str(claude_dir / ".credentials.json")
    claude.stage_credentials(tmp_path / "staged" / "claude")
    assert (tmp_path / "staged" / "claude" / ".credentials.json").read_text() == '{"from": "file"}'
    assert not calls.exists()


def test_keychain_login_is_staged_as_the_login_file(macos, claude_dir, keychain, tmp_path):
    """No file, a Keychain item: the item is the login and lands as the
    login file, owner-only, through the same safe write as a file."""
    calls = _item(keychain)
    claude = get_harness("claude")
    assert claude.login() == KEYCHAIN
    staged = tmp_path / "staged" / "claude"
    claude.stage_credentials(staged)
    login = staged / ".credentials.json"
    assert login.read_text() == PAYLOAD  # the trailing newline security adds is dropped
    assert stat.S_IMODE(login.stat().st_mode) == 0o600
    assert stat.S_IMODE(staged.stat().st_mode) == 0o700
    assert (staged / "settings.json").is_file()
    assert sorted(p.name for p in staged.iterdir()) == [".credentials.json", "settings.json"]
    read = "find-generic-password -a kc-user -s Claude Code-credentials -w"
    assert calls.read_text().splitlines() == [read, read]


def test_no_file_and_no_keychain_item_is_no_login(macos, claude_dir, keychain, tmp_path):
    claude = get_harness("claude")
    assert claude.login() is None
    staged = tmp_path / "staged" / "claude"
    claude.stage_credentials(staged)
    assert not (staged / ".credentials.json").exists()


def test_keychain_logout_removes_the_staged_copy(macos, claude_dir, keychain, tmp_path):
    _item(keychain)
    claude = get_harness("claude")
    staged = tmp_path / "staged" / "claude"
    claude.stage_credentials(staged)
    assert (staged / ".credentials.json").is_file()
    _fake(keychain, "exit 44")  # the item is gone: a host logout
    claude.stage_credentials(staged)
    assert not (staged / ".credentials.json").exists()


def test_keychain_read_that_times_out_is_no_login(
    macos, claude_dir, keychain, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """An unanswered macOS dialog never blocks treebox: the read is cut off."""
    monkeypatch.setattr(harnesses, "KEYCHAIN_TIMEOUT", 0.5)
    _fake(keychain, "exec sleep 30")
    claude = get_harness("claude")
    started = time.monotonic()
    assert claude.login() is None
    claude.stage_credentials(tmp_path / "staged" / "claude")
    assert time.monotonic() - started < 10
    assert not (tmp_path / "staged" / "claude" / ".credentials.json").exists()


def test_keychain_read_that_is_refused_is_no_login(macos, claude_dir, keychain):
    """Denied in the dialog (security exits non-zero): no login, and what
    security printed never becomes the login."""
    _fake(keychain, f"echo '{PAYLOAD}'; echo denied >&2; exit 51")
    assert get_harness("claude").login() is None


def test_another_users_keychain_item_is_not_the_login(macos, claude_dir, keychain, monkeypatch):
    """A stale item under the same service with another account is not read."""
    _item(keychain)
    monkeypatch.setenv("LOGNAME", "someone-else")
    assert get_harness("claude").login() is None


def test_keychain_is_never_read_off_macos(claude_dir, keychain, tmp_path, monkeypatch):
    monkeypatch.setattr(harnesses.sys, "platform", "linux")
    calls = _item(keychain)
    claude = get_harness("claude")
    assert claude.login() is None
    claude.stage_credentials(tmp_path / "staged" / "claude")
    assert not (tmp_path / "staged" / "claude" / ".credentials.json").exists()
    assert not calls.exists()


def test_a_harness_without_a_keychain_service_never_reads_it(macos, keychain, scratch_home):
    calls = _item(keychain)
    assert get_harness("codex").login() is None
    assert not calls.exists()


# --- through the CLI ------------------------------------------------------------


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TREEBOX_HOME", str(tmp_path / "treebox-home"))
    cfg = tmp_path / "config.toml"
    cfg.write_text('setup_hook = ["true"]\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))
    return lambda *args: CliRunner().invoke(app, list(args), catch_exceptions=False)


def test_doctor_reports_the_keychain_login_for_a_sandbox(macos, claude_dir, keychain, repo, run):
    """doctor, the LOGIN_MISSING gate and staging agree: a Keychain login
    is a sandbox login. Its JSON names the source, never the secret."""
    _item(keychain)
    res = run("doctor", "--repo", str(repo), "--isolation", "docker", "--json")
    checks = {c["name"]: c for c in json.loads(res.stdout)["checks"]}
    assert checks["login: claude"]["ok"] is True
    assert checks["login: claude"]["detail"] == KEYCHAIN
    assert SECRET not in res.output


@pytest.mark.parametrize("security", ["exec sleep 30", f"echo '{PAYLOAD}'; exit 51"])
def test_unanswered_or_refused_keychain_is_login_missing(
    macos, agent_ready, scratch_home, keychain, repo, run, tmp_path, monkeypatch, security
):
    """A timed-out or refused read is the classified LOGIN_MISSING with a hint
    that says what to do, and the error text holds no secret."""
    monkeypatch.setattr(harnesses, "KEYCHAIN_TIMEOUT", 0.5)
    (scratch_home / ".claude" / ".credentials.json").unlink()
    _fake(keychain, security)
    res = run(
        "--non-interactive",
        "create",
        "kc",
        "--repo",
        str(repo),
        "--root",
        str(tmp_path / "wts"),
        "--isolation",
        "host",
    )
    assert res.exit_code == 1, res.output
    assert "No claude login for an unattended launch" in res.output
    assert "Keychain item 'Claude Code-credentials': allow access if asked" in res.output
    assert SECRET not in res.output
    assert not (tmp_path / "wts" / "kc").exists()

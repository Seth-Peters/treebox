"""The unattended lifecycle contract (issue #64), through the real CLI.

Every case runs treebox as a separate process, the way a coordinator or
another agent drives it, in two terminal situations:

- ``pipe``: stdin is a pipe that is already closed (no terminal at all);
- ``pty``: a pseudo-terminal is the controlling terminal and stdin (from the
  standard library ``pty``), so ``/dev/tty`` exists and ``isatty()`` is true.

Unattended mode (``--non-interactive``) must never open a picker, ask a
question, prompt for credentials, or wait on the terminal in either case:
a run that waited would hang until ``_TIMEOUT`` and fail the test with what
it printed on the terminal. A missing prerequisite is a classified failure,
and under ``--json`` stderr holds exactly one JSON error object.

No real agent is ever launched: launches use stub executables named like
the harness binary, first on PATH. HOME is a scratch dir (conftest).
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from treebox import locking, system

_CLI = [sys.executable, "-c", "from treebox.cli import app; app()"]
_TIMEOUT = 60.0
MODES = ("pipe", "pty")


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str
    tty: str  # what the run wrote to the terminal (pty mode only)

    def error(self) -> dict[str, str]:
        """The one JSON error object stderr must hold under --json."""
        payload = json.loads(self.stderr)  # raises unless stderr is exactly one object
        assert set(payload) == {"schemaVersion", "error"}, self.stderr
        error: dict[str, str] = payload["error"]
        return error


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env.update(extra)
    return env


def run_cli(
    mode: str, args: list[str], *, env: dict[str, str], tmp: Path, unattended: bool = True
) -> Result:
    """Run the CLI with no input in ``mode``; fail if it waits on anything."""
    argv = [*_CLI, *(["--non-interactive"] if unattended else []), *args]
    if mode == "pipe":
        proc = subprocess.run(
            argv,
            env=env,
            input="",  # a pipe, closed at once: no terminal and no input
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr, "")

    out, err = tmp / "pty.stdout", tmp / "pty.stderr"
    pid, master = pty.fork()
    if pid == 0:  # child: the pty is the controlling terminal and stdin
        try:
            os.dup2(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 1)
            os.dup2(os.open(err, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 2)
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    seen = b""
    deadline = time.monotonic() + _TIMEOUT
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                with contextlib.suppress(OSError):  # the child closed the terminal
                    seen += os.read(master, 4096)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                return Result(
                    os.waitstatus_to_exitcode(status),
                    out.read_text(),
                    err.read_text(),
                    seen.decode(errors="replace"),
                )
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        pytest.fail(f"treebox waited on the terminal: {shlex.join(args)}; tty saw {seen!r}")
    finally:
        os.close(master)


@pytest.fixture
def cli(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``cli(mode, command, *args)`` runs a worktree command against the
    fixture repo, with the environment as it is at call time. Setup is a
    no-op hook that leaves the new worktree clean."""
    root = tmp_path / "wts"
    cfg = tmp_path / "config.toml"
    cfg.write_text('setup_hook = ["true"]\n')
    monkeypatch.setenv("TREEBOX_CONFIG", str(cfg))

    def call(mode: str, command: str, *args: str, unattended: bool = True, **extra: str) -> Result:
        env = _env(**extra)
        launches = command in ("create", "enter") and not {"--json", "--print", "--dry-run"} & {
            *args
        }
        if launches:  # never a real agent: the harness binary must be a test stub
            found = shutil.which("claude", path=env["PATH"])
            assert found is None or Path(found).is_relative_to(tmp_path), found
        # Options first: anything after `--` goes to the agent.
        where = ["--repo", str(repo)]
        if command != "doctor":
            where += ["--root", str(root)]
        return run_cli(mode, [command, *where, *args], env=env, tmp=tmp_path, unattended=unattended)

    call.root = root  # type: ignore[attr-defined]
    return call


# --- the failure cases: each one classified, none waits ---------------------------


_HTTP_401 = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="treebox-test"')
        self.send_header("Content-Length", "0")
        self.end_headers()
    do_POST = do_GET
    def log_message(self, *a):
        pass
s = http.server.HTTPServer(("127.0.0.1", 0), H)
print(s.server_address[1], flush=True)
s.serve_forever()
"""


@pytest.mark.parametrize("mode", MODES)
def test_fetch_that_needs_credentials_fails_fast(cli, repo: Path, mode: str):
    """A remote that answers 401 (it wants a username and password) must not
    make git ask for them on the terminal: FETCH_FAILED, exit 4."""
    server = subprocess.Popen([sys.executable, "-c", _HTTP_401], stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout is not None
        port = server.stdout.readline().strip()
        subprocess.run(
            ["git", "-C", str(repo), "remote", "set-url", "origin", f"http://127.0.0.1:{port}/r"],
            check=True,
        )
        res = cli(mode, "create", "needs-auth", "--json")
    finally:
        server.kill()
        server.wait()
    assert res.code == 4, res
    assert res.error()["code"] == "FETCH_FAILED"
    assert "Username" not in res.tty and "Password" not in res.tty
    assert not (cli.root / "needs-auth").exists()


@pytest.mark.parametrize("mode", MODES)
def test_missing_login_is_classified(cli, agent_ready: Path, scratch_home: Path, mode: str):
    """doctor fails the selected harness's login (exit 1), and an unattended
    launch without a login is LOGIN_MISSING before anything is provisioned -
    the agent would otherwise wait in its own login flow."""
    (scratch_home / ".claude" / ".credentials.json").unlink()
    (scratch_home / ".claude" / "settings.json").write_text("{}\n")  # dir non-empty, no login
    doc = cli(mode, "doctor", "--json")
    assert doc.code == 1, doc
    payload = json.loads(doc.stdout)
    assert payload["ok"] is False
    checks = {c["name"]: c["ok"] for c in payload["checks"]}
    assert checks["login: claude"] is False and checks["agent: claude"] is True

    res = cli(mode, "create", "no-login")
    assert res.code == 1, res
    assert "No claude login for an unattended launch" in res.stderr
    assert not (cli.root / "no-login").exists()


@pytest.mark.parametrize("mode", MODES)
def test_missing_harness_binary_is_classified(cli, agent_ready: Path, mode: str):
    """No harness binary: doctor fails the agent row (exit 1), and a launch
    is HARNESS_MISSING before anything is provisioned."""
    (agent_ready / "claude").unlink()
    path = [d for d in os.environ["PATH"].split(os.pathsep) if not (Path(d) / "claude").exists()]
    doc = cli(mode, "doctor", "--json", PATH=os.pathsep.join(path))
    assert doc.code == 1, doc
    checks = {c["name"]: c for c in json.loads(doc.stdout)["checks"]}
    assert checks["agent: claude"]["ok"] is False
    assert "not found on PATH" in checks["agent: claude"]["detail"]

    res = cli(mode, "create", "no-binary", PATH=os.pathsep.join(path))
    assert res.code == 1, res
    assert "'claude' not found on PATH" in res.stderr
    assert not (cli.root / "no-binary").exists()


@pytest.mark.parametrize("mode", MODES)
def test_ambiguous_ref_is_classified(cli, mode: str):
    for name in ("amb-one", "amb-two"):
        assert cli(mode, "create", name, "--json", "--no-fetch").code == 0
    res = cli(mode, "enter", "amb", "--json")
    assert res.code == 2, res
    assert res.error()["code"] == "AMBIGUOUS_REF"


@pytest.mark.parametrize("mode", MODES)
def test_held_lock_is_classified(cli, mode: str):
    assert cli(mode, "create", "locked", "--json", "--no-fetch").code == 0
    with locking.worktree_lock(cli.root / "locked"):
        res = cli(mode, "enter", "locked", "--json")
    assert res.code == 5, res
    assert res.error()["code"] == "LOCK_HELD"


@pytest.mark.parametrize("mode", MODES)
def test_failed_setup_is_one_json_error(cli, tmp_path: Path, mode: str):
    """A failing setup step: SETUP_FAILED, exit 1, stderr exactly one JSON
    object (the step's output is in the file the error names, not on stderr)."""
    cfg = tmp_path / "failing.toml"
    cfg.write_text('setup_hook = ["read answer; echo step-output; exit 3"]\n')
    res = cli(mode, "create", "bad-setup", "--json", "--no-fetch", TREEBOX_CONFIG=str(cfg))
    assert res.code == 1, res
    error = res.error()
    assert error["code"] == "SETUP_FAILED"
    assert "step-output" not in res.stderr
    assert "step-output" in Path(error["path"]).read_text()
    # --verbose streams the step output to stderr, and stdout stays clean.
    res = cli(mode, "enter", "bad-setup", "--json", "--verbose", TREEBOX_CONFIG=str(cfg))
    assert res.code == 1 and res.stdout == "", res
    assert "step-output" in res.stderr


@pytest.mark.parametrize("mode", MODES)
def test_unsafe_teardown_is_refused_without_input(cli, mode: str):
    """--yes confirms only: uncommitted changes still block (DIRTY_WORKTREE);
    without --yes there is no prompt but NEEDS_CONFIRMATION; and no refs never
    opens the picker."""
    assert cli(mode, "create", "dirty", "--json", "--no-fetch").code == 0
    (cli.root / "dirty" / "work.txt").write_text("unsaved\n")

    res = cli(mode, "teardown", "dirty", "--yes", "--json")
    assert (res.code, res.error()["code"]) == (5, "DIRTY_WORKTREE"), res
    res = cli(mode, "teardown", "dirty", "--json")
    assert (res.code, res.error()["code"]) == (5, "DIRTY_WORKTREE"), res
    res = cli(mode, "teardown", "--json")
    assert (res.code, res.error()["code"]) == (5, "NEEDS_CONFIRMATION"), res
    assert (cli.root / "dirty" / "work.txt").exists()

    (cli.root / "dirty" / "work.txt").unlink()
    res = cli(mode, "teardown", "dirty")  # clean, but nobody confirmed
    assert res.code == 5 and "--yes" in res.stderr, res
    assert (cli.root / "dirty").is_dir()
    res = cli(mode, "teardown", "dirty", "--yes", "--json")
    assert res.code == 0, res
    assert json.loads(res.stdout)["worktrees"][0]["removed"] is True


@pytest.mark.parametrize("mode", MODES)
def test_unavailable_docker_is_classified(cli, tmp_path: Path, mode: str):
    fake = tmp_path / "fake-docker"
    fake.mkdir()
    (fake / "docker").write_text(
        "#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n"
    )
    (fake / "docker").chmod(0o755)
    path = f"{fake}{os.pathsep}{os.environ['PATH']}"
    res = cli(mode, "create", "boxed", "--isolation", "docker", "--json", PATH=path)
    assert res.code == 1, res
    assert res.error()["code"] == "DOCKER_UNAVAILABLE"


def test_no_terminal_needs_no_flag(cli):
    """Without a terminal treebox is unattended already: no flag needed."""
    assert cli("pipe", "create", "plain", "--json", "--no-fetch", unattended=False).code == 0
    res = cli("pipe", "teardown", "plain", unattended=False)
    assert res.code == 5 and "--yes" in res.stderr, res


def test_global_flag_and_env_var_are_the_same_switch(cli):
    """$TREEBOX_NON_INTERACTIVE=1 is --non-interactive: with a terminal
    attached, teardown still refuses instead of prompting."""
    assert cli("pty", "create", "envvar", "--json", "--no-fetch").code == 0
    res = cli("pty", "teardown", "envvar", unattended=False, TREEBOX_NON_INTERACTIVE="1")
    assert res.code == 5 and "Refusing to remove non-interactively" in res.stderr, res


# --- the documented clean flow ---------------------------------------------------

_RECORDING_AGENT = """#!/bin/sh
# A stub agent: records where it ran and its args, then exits $STUB_EXIT.
pwd > "$STUB_LOG"
for a in "$@"; do printf '%s\\n' "$a" >> "$STUB_LOG"; done
exit "${STUB_EXIT:-0}"
"""


@pytest.mark.parametrize("mode", MODES)
def test_documented_clean_flow(cli, agent_ready: Path, tmp_path: Path, mode: str):
    """docs/usage.md#unattended-use: create --json with the task, replay the
    entry command, inspect with list --json, teardown --yes --json."""
    (agent_ready / "claude").write_text(_RECORDING_AGENT)
    log = tmp_path / "agent.log"

    res = cli(mode, "create", "task-1", "--json", "--", "-p", "fix the login race")
    assert res.code == 0, res
    created = json.loads(res.stdout)
    assert created["warnings"] == []
    # The host entry command is `sh -c "cd <worktree> && exec <agent argv>"`.
    assert created["entry_command"][2].endswith(
        "exec claude --dangerously-skip-permissions -p 'fix the login race'"
    )

    replay = subprocess.run(
        created["entry_command"],
        env=_env(STUB_LOG=str(log)),
        cwd=tmp_path,
        input="",
        timeout=_TIMEOUT,
    )
    assert replay.returncode == 0
    ran_in, *agent_args = log.read_text().splitlines()
    assert Path(ran_in).resolve() == Path(created["worktree_path"]).resolve()
    assert agent_args == ["--dangerously-skip-permissions", "-p", "fix the login race"]

    listed = json.loads(cli(mode, "list", "--json").stdout)
    (row,) = listed["worktrees"]
    assert (row["name"], row["health"], row["setup"]) == ("task-1", "healthy", "done")

    res = cli(mode, "teardown", "task-1", "--yes", "--json")
    assert res.code == 0, res
    torn = json.loads(res.stdout)
    assert torn["warnings"] == [] and torn["worktrees"][0]["complete"] is True


@pytest.mark.parametrize("mode", MODES)
def test_launch_passes_the_agent_exit_code_and_names_it(
    cli, agent_ready: Path, tmp_path: Path, mode: str
):
    """create NAME -- ARGS launches the agent with the task; its exit code
    passes through, and a non-zero one is named in the one final stderr line."""
    (agent_ready / "claude").write_text(_RECORDING_AGENT)
    log = tmp_path / "agent.log"
    res = cli(mode, "create", "exit-3", "--", "-p", "task", STUB_LOG=str(log), STUB_EXIT="3")
    assert res.code == 3, res
    assert res.stderr.splitlines()[-1] == "treebox: agent exited with status 3"
    assert log.read_text().splitlines()[1:] == ["--dangerously-skip-permissions", "-p", "task"]

    res = cli(mode, "enter", "exit-3", STUB_LOG=str(log), STUB_EXIT="0")
    assert res.code == 0, res
    assert "agent exited" not in res.stderr


# --- cancellation (host runner) ---------------------------------------------------

_TRAPPING_AGENT = """#!/bin/sh
# A stub agent that records its pid and the cancel signal it gets.
echo $$ > "$STUB_PID"
trap 'echo TERM > "$STUB_GOT"; exit 143' TERM
trap 'echo INT > "$STUB_GOT"; exit 130' INT
trap 'echo HUP > "$STUB_GOT"; exit 129' HUP
while :; do sleep 0.1; done
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_cancel_signal_stops_the_host_agent(
    cli, agent_ready: Path, tmp_path: Path, sig: signal.Signals
):
    """A cancel signal to the treebox process is passed on to the agent; the
    agent is gone when treebox exits, and treebox exits 128 + signum with
    CANCELLED (never the agent-exit line)."""
    (agent_ready / "claude").write_text(_TRAPPING_AGENT)
    assert cli("pipe", "create", "cancel-me", "--json", "--no-fetch").code == 0
    pidfile, got = tmp_path / "agent.pid", tmp_path / "agent.got"
    proc = subprocess.Popen(
        [
            *_CLI,
            "enter",
            "cancel-me",
            "--repo",
            str(cli.root.parent / "repo"),
            "--root",
            str(cli.root),
        ],
        env=_env(STUB_PID=str(pidfile), STUB_GOT=str(got)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + _TIMEOUT
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
        time.sleep(0.05)
    agent = int(pidfile.read_text())
    proc.send_signal(sig)
    _, stderr = proc.communicate(timeout=_TIMEOUT)
    assert proc.returncode == 128 + sig, stderr
    assert not _alive(agent)
    assert got.read_text().strip() == sig.name.removeprefix("SIG")
    assert f"Cancelled by {sig.name}" in stderr
    assert "agent exited with status" not in stderr


def test_agent_that_ignores_the_signal_is_killed_after_the_grace_time(tmp_path: Path):
    """run_agent's escalation: the agent gets the cancel signal first and
    SIGKILL once the grace time is over."""
    script = tmp_path / "stubborn"
    script.write_text("#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 0.1; done\n")
    script.chmod(0o755)
    driver = (
        "import os, signal, sys, threading\n"
        "from treebox import system\n"
        "threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()\n"
        "code, sig = system.run_agent([sys.argv[1]], grace=0.5)\n"
        "print(code, sig)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", driver, str(script)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )
    assert out.stdout.split() == [str(-signal.SIGKILL), str(int(signal.SIGTERM))], out


def test_cancel_signals_are_the_documented_set():
    assert system.CANCEL_SIGNALS == (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

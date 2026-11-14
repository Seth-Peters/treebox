"""The treebox end-to-end harness: fixture, capture, scenarios, evidence bundle.

Standard library only. Scenarios (``scenarios.py``) drive the real ``treebox``
console script of this checkout against a disposable fixture and judge
behavior: exit codes, JSON fields, files on disk, git and container state.

Every run is hermetic:

- ``HOME``, ``TREEBOX_HOME`` and ``TREEBOX_CONFIG`` point into the fixture, so
  nothing reads or writes the real ``~/.treebox``, ``~/.claude`` or
  ``~/.codex``. The fixture's ``~/.claude`` / ``~/.codex`` hold fake login
  files only.
- ``PATH`` is rebuilt from scratch: the controlled stub agent first, then a
  toolbox of symlinks to the few host tools treebox needs (git, uv, docker),
  then the system directories. The harness refuses to run when any other
  ``claude`` or ``codex`` binary is reachable, so no scenario can start a real
  autonomous agent, even by mistake.
- The stub dir also holds a fake macOS ``security`` that finds no Keychain
  item, so on macOS no scenario reads the developer's real Keychain login.
- The CLI is the console script beside ``sys.executable``, called directly:
  no ``uv run`` wrapper adds lines to a scenario's stderr.

Evidence is written while the run goes, outside the fixture, so it survives a
failed scenario and a crash: see ``Bundle``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import errno
import json
import os
import platform
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, NoReturn

E2E_DIR = Path(__file__).resolve().parent
PROJECT = E2E_DIR.parent.parent
STUB_AGENT = E2E_DIR / "stub-agent"
FAKES_DIR = E2E_DIR / "fakes"
OFFLINE_TEMPLATE_DIR = E2E_DIR / "offline-template"
OFFLINE_TEMPLATE = "e2e-offline"
GOLDEN_DIR = PROJECT / "tests" / "golden"

BUNDLE_SCHEMA_VERSION = 1

# Docker names: every docker scenario names its worktree "e2e-...", so its
# container and image are "treebox-e2e-..." and the offline template's volume
# is "treebox-e2e-vol-e2e-...". The bundled template's history volume is
# "treebox-shellhistory-<name>". The reaper removes exactly these prefixes.
DOCKER_PREFIX = "treebox-e2e-"
DOCKER_VOLUME_PREFIXES = (DOCKER_PREFIX, "treebox-shellhistory-e2e-")

AGENT_NAMES = ("claude", "codex")
# The macOS Keychain tool treebox reads a login from when the login file is
# absent. The fixture's copy finds nothing ("item not found" is status 44).
FAKE_SECURITY = "#!/bin/sh\n# treebox e2e: no Keychain item, never the real Keychain.\nexit 44\n"
# Opt-in for the scenario that builds the bundled sandbox image (about
# 5.6 GB, needs the network): set to 1.
BUNDLED_IMAGE_OPT_IN = "TREEBOX_E2E_BUNDLED_IMAGE"
DOCUMENTED_EXIT_CODES = frozenset({0, 1, 2, 3, 4, 5})

# "known-bug": the scenario failed exactly as a documented, unfixed product
# bug predicts. It is shown on its own, never counted as passed, and does not
# fail the run; remove the scenario's ``known_bug`` once the bug is fixed.
Verdict = Literal["pass", "fail", "skip", "known-bug"]
VERDICTS: tuple[Verdict, ...] = ("pass", "fail", "skip", "known-bug")
Evidence = Literal["live", "boundary"]


class SkipScenario(Exception):
    """Raised by ``Ctx.skip``: the scenario cannot run here (never a pass)."""


class HarnessError(RuntimeError):
    """The harness itself cannot run safely or at all."""


# --- running processes -------------------------------------------------------


@dataclasses.dataclass
class RunRecord:
    """One captured process: the unit of evidence."""

    index: int
    kind: Literal["cli", "setup"]
    mode: Literal["pipe", "pty"]
    argv: list[str]
    cwd: str
    env: dict[str, str]  # only the variables that differ from the scenario env
    exit: int
    stdout: str
    stderr: str
    duration: float
    tags: frozenset[str] = frozenset()

    @property
    def command(self) -> str:
        """The treebox subcommand words, e.g. ``template init``."""
        words: list[str] = []
        for arg in self.argv[1:]:
            if arg.startswith("-"):
                break
            words.append(arg)
            if words[0] != "template" or len(words) == 2:
                break
        return " ".join(words)

    def has_flag(self, flag: str) -> bool:
        # Agent args after `--` belong to the agent, not to treebox.
        args = self.argv[1:]
        if "--" in args:
            args = args[: args.index("--")]
        return flag in args


def strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[()][A-Za-z0-9]", "", text)
    return re.sub(r"\x1b[=>78]", "", text)


ANSI_RE = re.compile(r"\x1b\[")


class PtySession:
    """A process on a pseudo-terminal, driven like a person at a keyboard.

    Uses ``pty.fork`` so the child gets the terminal as its controlling tty,
    sets a fixed window size, and answers cursor-position requests the way a
    terminal does (prompt_toolkit asks for one before it draws)."""

    def __init__(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, rows: int, cols: int
    ) -> None:
        import fcntl
        import pty
        import struct
        import termios

        self.argv = list(argv)
        self._raw = bytearray()
        self._pos = 0
        self.started = time.monotonic()
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
                os.execve(self.argv[0], self.argv, dict(env))
            finally:
                os._exit(127)
        self.pid = pid
        self.fd = fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.exit: int | None = None

    def _pump(self, timeout: float) -> bool:
        """Read what is available; False at end of output."""
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return True
        try:
            chunk = os.read(self.fd, 65536)
        except OSError as exc:
            if exc.errno == errno.EIO:  # Linux: the child closed the terminal
                return False
            raise
        if not chunk:
            return False
        self._raw.extend(chunk)
        for _ in range(chunk.count(b"\x1b[6n")):
            os.write(self.fd, b"\x1b[1;1R")
        return True

    @property
    def transcript(self) -> str:
        return self._raw.decode("utf-8", errors="replace")

    def screen_text(self) -> str:
        return strip_ansi(self.transcript)

    def expect(self, pattern: str, timeout: float = 30.0) -> bool:
        """Wait until ``pattern`` (a regex) appears in the output after the
        previous match. Returns False on timeout or end of output."""
        deadline = time.monotonic() + timeout
        regex = re.compile(pattern)
        while True:
            text = strip_ansi(self._raw[self._pos :].decode("utf-8", errors="replace"))
            if regex.search(text):
                self._pos = len(self._raw)
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not self._pump(min(remaining, 0.2)):
                text = strip_ansi(self._raw[self._pos :].decode("utf-8", errors="replace"))
                return bool(regex.search(text))

    def send(self, data: str) -> None:
        os.write(self.fd, data.encode())

    def wait(self, timeout: float = 60.0) -> int:
        deadline = time.monotonic() + timeout
        while self.exit is None:
            with contextlib.suppress(OSError):
                self._pump(0.1)
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                # Drain what the child wrote before it exited.
                with contextlib.suppress(OSError):
                    while self._pump(0.05) and select.select([self.fd], [], [], 0)[0]:
                        pass
                self.exit = os.waitstatus_to_exitcode(status)
            elif time.monotonic() > deadline:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
                self.exit = -9
        with contextlib.suppress(OSError):
            os.close(self.fd)
        return self.exit


# --- the hermetic environment --------------------------------------------------


def _tool(name: str) -> str | None:
    return shutil.which(name)


@dataclasses.dataclass
class Fixture:
    """One disposable sandbox: bare origin, clone, worktree root, scratch
    home and treebox home, stub bin dir, toolbox. Everything lives under
    ``sbx``; ``remove`` deletes it."""

    sbx: Path
    env: dict[str, str]
    tb_bin: Path

    @property
    def repo(self) -> Path:
        return self.sbx / "repo"

    @property
    def origin(self) -> Path:
        return self.sbx / "origin.git"

    @property
    def root(self) -> Path:
        return self.sbx / "wts"

    @property
    def home(self) -> Path:
        return self.sbx / "home"

    @property
    def treebox_home(self) -> Path:
        return self.sbx / "treebox-home"

    @property
    def config(self) -> Path:
        return self.treebox_home / "config.toml"

    @property
    def stub_bin(self) -> Path:
        return self.sbx / "stub-bin"

    @property
    def toolbox(self) -> Path:
        return self.sbx / "toolbox"

    @classmethod
    def create(cls, *, tb_bin: Path, base_env: Mapping[str, str]) -> Fixture:
        sbx = Path(tempfile.mkdtemp(prefix="treebox-e2e.")).resolve()
        fx = cls(sbx=sbx, env={}, tb_bin=tb_bin)
        try:
            fx._build(base_env)
        except BaseException:
            fx.remove()
            raise
        return fx

    def _build(self, base_env: Mapping[str, str]) -> None:
        for d in (self.home, self.treebox_home, self.stub_bin, self.toolbox, self.root.parent):
            d.mkdir(parents=True, exist_ok=True)
        for agent in AGENT_NAMES:
            (self.stub_bin / agent).symlink_to(STUB_AGENT)
        security = self.stub_bin / "security"
        security.write_text(FAKE_SECURITY)
        security.chmod(0o755)
        for tool in ("git", "uv", "docker"):
            found = _tool(tool)
            if found:
                (self.toolbox / tool).symlink_to(found)
        docker = _tool("docker")
        if docker:
            # Docker Desktop resolves registry credentials through helpers
            # that live beside the docker binary.
            for helper in Path(os.path.realpath(docker)).parent.glob("docker-credential-*"):
                link = self.toolbox / helper.name
                if not link.exists():
                    link.symlink_to(helper)
        # Fake subscription logins: doctor sees a login, docker staging has
        # something to copy, and no real credential is ever read.
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / ".credentials.json").write_text('{"fake": "treebox-e2e"}\n')
        (self.home / ".claude" / "settings.json").write_text('{"treebox-e2e": true}\n')
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").write_text('{"fake": "treebox-e2e"}\n')
        (self.home / ".codex" / "config.toml").write_text("# treebox-e2e\n")
        self.config.write_text("")  # empty config: built-in defaults
        self.env = hermetic_env(self, base_env)
        assert_no_real_agent(self.env["PATH"], self.stub_bin)
        self._build_repo()

    def _build_repo(self) -> None:
        def run(*argv: str, cwd: Path | None = None) -> None:
            subprocess.run(
                argv,
                cwd=cwd,
                env=self.env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

        run("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        run("git", "clone", "-q", str(self.origin), str(self.repo))
        repo = self.repo
        # Repo-local identity, so a manual sweep's own git commits work too.
        run("git", "config", "user.name", "treebox-e2e", cwd=repo)
        run("git", "config", "user.email", "e2e@treebox.test", cwd=repo)
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
            "dependencies = []\n"
        )
        run("uv", "lock", "-q", cwd=repo)
        # .env is the host's secrets file: untracked and ignored, as in a real
        # repo. A tracked .env would dirty every worktree once a scenario
        # rotates the host copy.
        (repo / ".gitignore").write_text(".env\n")
        (repo / ".env").write_text("SECRET=canonical\n")
        run("git", "add", "-A", cwd=repo)
        run("git", "commit", "-qm", "init", cwd=repo)
        run("git", "push", "-q", "origin", "main", cwd=repo)
        # A second branch so --base and --checkout scenarios have a target.
        run("git", "branch", "dev", cwd=repo)
        run("git", "push", "-q", "origin", "dev", cwd=repo)

    def install_offline_template(self) -> Path:
        """Copy the offline template into the scratch treebox home, the way
        an operator installs any template. Symlinks (post-create.sh is the
        stub agent) are copied as the files they point at."""
        dest = self.treebox_home / "templates" / OFFLINE_TEMPLATE
        if not dest.exists():
            shutil.copytree(OFFLINE_TEMPLATE_DIR, dest, symlinks=False)
        return dest

    def remove(self) -> None:
        if not self.sbx.exists():
            return
        # Scenarios make read-only directories on purpose; restore write
        # access so the sandbox can always be removed.
        for path in [self.sbx, *self.sbx.rglob("*")]:
            with contextlib.suppress(OSError):
                if path.is_dir() and not path.is_symlink():
                    path.chmod(0o755)
        shutil.rmtree(self.sbx, ignore_errors=True)


def hermetic_env(fx: Fixture, base_env: Mapping[str, str]) -> dict[str, str]:
    """The complete environment every scenario process gets. Built from an
    allowlist, never inherited wholesale, so the developer's or CI's settings
    (VIRTUAL_ENV, ANTHROPIC_API_KEY, NO_COLOR, TREEBOX_*) cannot leak in."""
    env: dict[str, str] = {}
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "DOCKER_HOST"):
        if base_env.get(key):
            env[key] = base_env[key]
    env["PATH"] = os.pathsep.join(
        [str(fx.stub_bin), str(fx.toolbox), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    )
    env["HOME"] = str(fx.home)
    env["TREEBOX_HOME"] = str(fx.treebox_home)
    env["TREEBOX_CONFIG"] = str(fx.config)
    # Hide the host's git configuration (hooks, signing, templates).
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "treebox-e2e"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "e2e@treebox.test"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # The scratch HOME hides uv's managed interpreters and the docker CLI's
    # plugins and contexts; point both tools at the real locations (read).
    uv_python = base_env.get("UV_PYTHON_INSTALL_DIR") or _uv_python_dir(base_env)
    if uv_python:
        env["UV_PYTHON_INSTALL_DIR"] = uv_python
    docker_config = base_env.get("DOCKER_CONFIG") or str(Path.home() / ".docker")
    if Path(docker_config).is_dir():
        env["DOCKER_CONFIG"] = docker_config
    if base_env.get("DOCKER_CONTEXT"):
        env["DOCKER_CONTEXT"] = base_env["DOCKER_CONTEXT"]
    env.setdefault("TMPDIR", tempfile.gettempdir())
    return env


def _uv_python_dir(base_env: Mapping[str, str]) -> str | None:
    uv = shutil.which("uv", path=base_env.get("PATH"))
    if not uv:
        return None
    proc = subprocess.run([uv, "python", "dir"], capture_output=True, text=True, env=dict(base_env))
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def assert_no_real_agent(path: str, stub_bin: Path) -> None:
    """Refuse to run when an agent CLI other than the stub is reachable."""
    for agent in AGENT_NAMES:
        resolved = shutil.which(agent, path=path)
        if resolved is None or Path(resolved).parent != stub_bin:
            raise HarnessError(f"'{agent}' does not resolve to the stub agent (got {resolved})")
        for directory in path.split(os.pathsep):
            candidate = Path(directory) / agent
            if Path(directory) != stub_bin and candidate.exists():
                raise HarnessError(
                    f"a real '{agent}' binary is reachable at {candidate}; refusing to run "
                    "because a scenario could launch it"
                )


def without_path_entry(env: Mapping[str, str], entry: Path) -> dict[str, str]:
    out = dict(env)
    out["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if p != str(entry))
    return out


# --- prerequisites -------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Prerequisite:
    name: str
    available: bool
    detail: str


def offline_base_image() -> str:
    for line in (OFFLINE_TEMPLATE_DIR / "Dockerfile").read_text().splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    raise HarnessError("offline template Dockerfile has no FROM line")


def probe_prerequisites(base_env: Mapping[str, str]) -> dict[str, Prerequisite]:
    found: dict[str, Prerequisite] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        found[name] = Prerequisite(name, ok, detail)

    docker = shutil.which("docker", path=base_env.get("PATH"))
    add("docker-cli", docker is not None, docker or "no docker CLI on PATH")
    if not docker:
        add("docker", False, "no docker CLI on PATH")
    else:
        proc = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}} {{.OSType}}/{{.Architecture}}"],
            capture_output=True,
            text=True,
            env=dict(base_env),
        )
        if proc.returncode != 0:
            add("docker", False, "docker daemon not reachable: " + proc.stderr.strip()[:200])
        else:
            add("docker", True, "engine " + proc.stdout.strip())
    if found["docker"].available:
        image = offline_base_image()
        # Inspect by digest alone: "name:tag@digest" never matches a local image.
        by_digest = re.sub(r":[^/@]+@", "@", image)
        inspect = subprocess.run(
            [docker or "docker", "image", "inspect", by_digest],
            capture_output=True,
            env=dict(base_env),
        )
        if inspect.returncode == 0:
            add("offline-base-image", True, f"{image} present")
        else:
            pull = subprocess.run(
                [docker or "docker", "pull", "-q", image],
                capture_output=True,
                text=True,
                env=dict(base_env),
            )
            ok = pull.returncode == 0
            detail = f"{image} pulled" if ok else f"{image} absent and pull failed"
            add("offline-base-image", ok, detail)
    else:
        add("offline-base-image", False, "needs docker")
    add("pty", hasattr(os, "openpty"), "POSIX pseudo-terminal")
    add(
        "non-root",
        os.geteuid() != 0,
        "file-permission failures need a non-root user (root ignores modes)",
    )
    add(
        "root",
        os.geteuid() == 0,
        "running as root (uid 0)" if os.geteuid() == 0 else "the run is not root (uid 0)",
    )
    add(
        "macos",
        sys.platform == "darwin",
        f"platform {sys.platform} (the Keychain login source is macOS only)",
    )
    opted = base_env.get(BUNDLED_IMAGE_OPT_IN) == "1"
    add(
        "bundled-image",
        opted and found["docker"].available,
        "opted in"
        if opted
        else f"opt-in: set {BUNDLED_IMAGE_OPT_IN}=1 to build the bundled image "
        "(about 5.6 GB, needs the network)",
    )
    shell_ok = _shell_detection_works()
    add(
        "shell-detection",
        shell_ok,
        "typer detects the parent shell (shellingham)"
        if shell_ok
        else "shellingham cannot detect the parent shell here",
    )
    return found


def _shell_detection_works() -> bool:
    bash = shutil.which("bash")
    if not bash:
        return False
    code = "import shellingham; shellingham.detect_shell()"
    proc = subprocess.run([bash, "-c", f'"{sys.executable}" -c "{code}"'], capture_output=True)
    return proc.returncode == 0


# --- scenarios -------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Scenario:
    id: str
    group: str
    title: str
    func: Callable[[Ctx], None]
    requires: tuple[str, ...] = ()
    evidence: Evidence = "live"
    # A documented, unfixed product bug this scenario reproduces, and a regex
    # its failing runs' output must match: any other failure is a real FAIL.
    known_bug: str = ""
    known_bug_signature: str = ""


@dataclasses.dataclass(frozen=True)
class Group:
    letter: str
    title: str
    setup: Callable[[Ctx], None] | None = None
    requires: tuple[str, ...] = ()


SCENARIOS: list[Scenario] = []
GROUPS: dict[str, Group] = {}


def group(
    letter: str, title: str, *, requires: tuple[str, ...] = ()
) -> Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]:
    """Register a group and its fresh-fixture setup."""

    def wrap(setup: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        GROUPS[letter] = Group(letter, title, setup, requires)
        return setup

    return wrap


def scenario(
    sid: str,
    title: str,
    *,
    requires: tuple[str, ...] = (),
    evidence: Evidence = "live",
    known_bug: str = "",
    known_bug_signature: str = "",
) -> Callable[[Callable[[Ctx], None]], Callable[[Ctx], None]]:
    """Register a scenario. Its id is the one in references/scenarios.md; its
    group is the id's leading letters."""

    def wrap(func: Callable[[Ctx], None]) -> Callable[[Ctx], None]:
        letters = re.match(r"[A-Z]+", sid)
        assert letters, sid
        if bool(known_bug) != bool(known_bug_signature):
            raise HarnessError(f"{sid}: known_bug needs a known_bug_signature")
        if any(s.id == sid for s in SCENARIOS):
            raise HarnessError(f"duplicate scenario id {sid}")
        SCENARIOS.append(
            Scenario(
                sid,
                letters.group(0),
                title,
                func,
                requires,
                evidence,
                known_bug,
                known_bug_signature,
            )
        )
        return func

    return wrap


@dataclasses.dataclass
class Check:
    ok: bool
    message: str


class Ctx:
    """What a scenario sees: the fixture, the CLI, checks, and its records."""

    def __init__(self, fx: Fixture, sink: Callable[[RunRecord], None]) -> None:
        self.fx = fx
        self.checks: list[Check] = []
        self.runs: list[RunRecord] = []
        self._sink = sink
        self.notes: list[str] = []

    # paths
    @property
    def repo(self) -> Path:
        return self.fx.repo

    @property
    def root(self) -> Path:
        return self.fx.root

    @property
    def sbx(self) -> Path:
        return self.fx.sbx

    @property
    def env(self) -> dict[str, str]:
        return self.fx.env

    def wt_args(self, root: Path | None = None) -> list[str]:
        return ["--repo", str(self.repo), "--root", str(root or self.root)]

    # processes
    def _record(self, record: RunRecord) -> RunRecord:
        self.runs.append(record)
        self._sink(record)
        return record

    def _proc(
        self,
        kind: Literal["cli", "setup"],
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        input: str | None,
        tags: Sequence[str],
        timeout: float,
    ) -> RunRecord:
        full_env = dict(env if env is not None else self.env)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                list(argv),
                cwd=cwd or self.sbx,
                env=full_env,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code = -9
            out = _text(exc.stdout)
            err = _text(exc.stderr) + f"\n[harness] timed out after {timeout}s\n"
        return self._record(
            RunRecord(
                index=len(self.runs) + 1,
                kind=kind,
                mode="pipe",
                argv=list(argv),
                cwd=str(cwd or self.sbx),
                env=_env_delta(self.env, full_env),
                exit=code,
                stdout=out,
                stderr=err,
                duration=round(time.monotonic() - started, 3),
                tags=frozenset(tags),
            )
        )

    def tb(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        input: str | None = None,
        tags: Sequence[str] = (),
        timeout: float = 600,
    ) -> RunRecord:
        """Run the treebox CLI exactly as a user's shell would."""
        return self._proc(
            "cli",
            [str(self.fx.tb_bin), *args],
            cwd=cwd,
            env=env,
            input=input,
            tags=tags,
            timeout=timeout,
        )

    def tb_background(
        self, *args: str, env: Mapping[str, str] | None = None, tags: Sequence[str] = ()
    ) -> Background:
        """Start the CLI and return at once; ``Background.wait`` records it."""
        return Background(self, [str(self.fx.tb_bin), *args], env, tags)

    def tb_parallel(
        self, arglists: Sequence[Sequence[str]], *, tags: Sequence[str] = ()
    ) -> list[RunRecord]:
        """Start several CLI runs at the same moment, as a dispatcher of
        parallel agents does, and wait for all of them."""
        started = [self.tb_background(*args, tags=tags) for args in arglists]
        return [bg.wait() for bg in started]

    def tb_shell(
        self, shell: str, script: str, *, env: Mapping[str, str] | None = None
    ) -> RunRecord:
        """Run the CLI from a real shell (``$TB`` is the CLI), for routes that
        depend on the parent shell, such as completion."""
        full_env = dict(env if env is not None else self.env)
        full_env["TB"] = str(self.fx.tb_bin)
        return self._proc(
            "cli", [shell, "-c", script], cwd=None, env=full_env, input=None, tags=(), timeout=120
        )

    def sh(
        self,
        *argv: str,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input: str | None = None,
    ) -> RunRecord:
        """A setup or inspection command. With ``check`` a failure aborts the
        scenario as a FAIL (the precondition could not be built)."""
        run = self._proc("setup", argv, cwd=cwd, env=env, input=input, tags=(), timeout=300)
        if check and run.exit != 0:
            raise AssertionError(
                f"setup command failed ({run.exit}): {' '.join(argv)}\n{run.stderr.strip()}"
            )
        return run

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        return self.sh("git", "-C", str(cwd or self.repo), *args, check=check).stdout.strip()

    @contextlib.contextmanager
    def pty(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        rows: int = 40,
        cols: int = 120,
    ) -> Iterator[PtySession]:
        """Run the CLI on a pseudo-terminal (stdin, stdout and stderr are the
        terminal). The transcript is recorded as the run's stdout."""
        full_env = dict(env if env is not None else self.env)
        full_env.setdefault("TERM", "xterm-256color")
        argv = [str(self.fx.tb_bin), *args]
        session = PtySession(argv, env=full_env, cwd=self.sbx, rows=rows, cols=cols)
        try:
            yield session
        finally:
            if session.exit is None:
                session.wait(timeout=30)
            self._record(
                RunRecord(
                    index=len(self.runs) + 1,
                    kind="cli",
                    mode="pty",
                    argv=argv,
                    cwd=str(self.sbx),
                    env=_env_delta(self.env, full_env),
                    exit=session.exit if session.exit is not None else -9,
                    stdout=session.transcript,
                    stderr="",
                    duration=round(time.monotonic() - session.started, 3),
                )
            )

    # judgement
    def check(self, ok: bool, message: str) -> bool:
        self.checks.append(Check(bool(ok), message))
        return bool(ok)

    def exit_is(self, run: RunRecord, code: int) -> bool:
        return self.check(
            run.exit == code,
            f"`{_short_argv(run, self.sbx)}` exits {code} (got {run.exit})",
        )

    def eq(self, actual: object, expected: object, what: str) -> bool:
        return self.check(actual == expected, f"{what} == {expected!r} (got {actual!r})")

    def json_out(self, run: RunRecord) -> Any:
        """stdout parsed as one JSON document; a failed parse is a failed check."""
        try:
            return json.loads(run.stdout)
        except json.JSONDecodeError as exc:
            self.check(False, f"stdout of `{_short_argv(run, self.sbx)}` is JSON ({exc})")
            return None

    def json_err(self, run: RunRecord, code: str | None = None) -> dict[str, Any]:
        """The structured --json error on stderr; checks its code when given."""
        try:
            payload = json.loads(run.stderr)
            error = payload["error"]
            assert isinstance(error, dict)
        except (json.JSONDecodeError, KeyError, TypeError, AssertionError):
            self.check(False, f"stderr of `{_short_argv(run, self.sbx)}` is one JSON error object")
            return {}
        if code is not None:
            self.eq(error.get("code"), code, "error.code")
        return error

    def skip(self, reason: str) -> NoReturn:
        raise SkipScenario(reason)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def write_config(self, text: str) -> None:
        self.fx.config.write_text(text)


class Background:
    """A CLI run that continues while the scenario does other things."""

    def __init__(
        self,
        ctx: Ctx,
        argv: list[str],
        env: Mapping[str, str] | None,
        tags: Sequence[str],
    ) -> None:
        self.ctx = ctx
        self.argv = argv
        self.env = dict(env if env is not None else ctx.env)
        self.tags = frozenset(tags)
        self.started = time.monotonic()
        self._out = tempfile.TemporaryFile()  # noqa: SIM115 (closed in wait)
        self._err = tempfile.TemporaryFile()  # noqa: SIM115 (closed in wait)
        self.proc = subprocess.Popen(
            argv,
            cwd=ctx.sbx,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=self._out,
            stderr=self._err,
        )

    def running(self) -> bool:
        return self.proc.poll() is None

    def stdout_so_far(self) -> str:
        """What the process wrote to stdout until now (it may still run)."""
        # pread: the child shares this file's offset, so never move it.
        fd = self._out.fileno()
        return os.pread(fd, os.fstat(fd).st_size, 0).decode("utf-8", "replace")

    def signal(self, signum: int) -> None:
        self.proc.send_signal(signum)

    def wait(self, timeout: float = 600) -> RunRecord:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        outputs = []
        for f in (self._out, self._err):
            f.seek(0)
            outputs.append(f.read().decode("utf-8", "replace"))
            f.close()
        return self.ctx._record(
            RunRecord(
                index=len(self.ctx.runs) + 1,
                kind="cli",
                mode="pipe",
                argv=self.argv,
                cwd=str(self.ctx.sbx),
                env=_env_delta(self.ctx.env, self.env),
                exit=code,
                stdout=outputs[0],
                stderr=outputs[1],
                duration=round(time.monotonic() - self.started, 3),
                tags=self.tags,
            )
        )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _env_delta(base: Mapping[str, str], env: Mapping[str, str]) -> dict[str, str]:
    delta = {k: v for k, v in env.items() if base.get(k) != v}
    delta.update({k: "<unset>" for k in base if k not in env})
    return delta


def _short_argv(run: RunRecord, sbx: Path | None = None) -> str:
    """A readable command line: the binary's name, fixture paths as $SBX."""
    text = shlex.join([Path(run.argv[0]).name, *run.argv[1:]])
    if sbx is not None:
        text = text.replace(str(sbx), "$SBX")
    return text if len(text) < 200 else text[:197] + "..."


# --- state capture ---------------------------------------------------------------


def capture_state(fx: Fixture, docker: bool) -> dict[str, Any]:
    """The git, filesystem and container state a scenario left behind."""

    def out(argv: list[str]) -> str:
        proc = subprocess.run(argv, capture_output=True, text=True, env=fx.env, errors="replace")
        return proc.stdout if proc.returncode == 0 else f"<exit {proc.returncode}> {proc.stderr}"

    files: list[str] = []
    for base in (fx.root, fx.treebox_home):
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel = Path(dirpath).relative_to(fx.sbx)
            dirnames[:] = sorted(d for d in dirnames if d not in (".git", ".venv"))
            files.append(f"{rel}/")
            files.extend(str(rel / f) for f in sorted(filenames))
    state: dict[str, Any] = {
        "git": {
            "worktrees": out(["git", "-C", str(fx.repo), "worktree", "list", "--porcelain"]),
            "branches": out(
                [
                    "git",
                    "-C",
                    str(fx.repo),
                    "for-each-ref",
                    "--format=%(refname) %(objectname:short)",
                    "refs/heads",
                    "refs/remotes",
                ]
            ),
            "status": out(["git", "-C", str(fx.repo), "status", "--porcelain"]),
        },
        "files": files[:2000],
    }
    if docker:
        state["docker"] = docker_resources(fx.env)
    return state


def e2e_owned(kind: str, lines: Sequence[str]) -> list[str]:
    """The listing lines whose resource name (the first field) starts with an
    e2e prefix. A prefix, never a substring: ``treebox-keen-eagle-*`` or
    ``my-treebox-e2e-x`` are someone else's."""
    prefixes = DOCKER_VOLUME_PREFIXES if kind == "volumes" else (DOCKER_PREFIX,)
    return [line for line in lines if line.split(" ", 1)[0].startswith(prefixes)]


def docker_resources(env: Mapping[str, str]) -> dict[str, list[str]]:
    """Containers, images and volumes that carry the e2e prefixes."""

    def lines(argv: list[str]) -> list[str]:
        proc = subprocess.run(argv, capture_output=True, text=True, env=dict(env))
        return [line for line in proc.stdout.splitlines() if line.strip()]

    containers = e2e_owned(
        "containers", lines(["docker", "ps", "-a", "--format", "{{.Names}} {{.ID}} {{.State}}"])
    )
    images = sorted(
        set(
            e2e_owned("images", lines(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]))
        )
    )
    volumes = e2e_owned("volumes", lines(["docker", "volume", "ls", "--format", "{{.Name}}"]))
    return {"containers": containers, "images": images, "volumes": volumes}


def reap_docker(env: Mapping[str, str]) -> dict[str, list[str]]:
    """Remove every e2e-prefixed container, image and volume, and nothing
    else: names are matched by exact prefix, never by substring."""
    if not shutil.which("docker", path=env.get("PATH")):
        return {"containers": [], "images": [], "volumes": []}
    found = docker_resources(env)
    run_env = dict(env)
    ids = [line.split()[1] for line in found["containers"]]
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, env=run_env)
    if found["images"]:
        subprocess.run(["docker", "rmi", "-f", *found["images"]], capture_output=True, env=run_env)
    if found["volumes"]:
        subprocess.run(
            ["docker", "volume", "rm", "-f", *found["volumes"]], capture_output=True, env=run_env
        )
    return found


# --- the evidence bundle ---------------------------------------------------------


@dataclasses.dataclass
class Result:
    id: str
    group: str
    title: str
    verdict: Verdict
    evidence: Evidence
    reason: str
    checks: list[Check]
    runs: list[RunRecord]
    notes: list[str]
    duration: float


class Bundle:
    """The evidence directory. Written incrementally: results.json is
    rewritten after every scenario, so a crash still leaves every finished
    scenario's evidence behind."""

    def __init__(self, out: Path) -> None:
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        (out / "scenarios").mkdir(exist_ok=True)
        self.results: list[Result] = []
        self._json: list[dict[str, Any]] = []

    def scenario_dir(self, sid: str) -> Path:
        d = self.out / "scenarios" / sid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_run(self, sid: str, run: RunRecord) -> dict[str, Any]:
        d = self.scenario_dir(sid)
        stem = f"run-{run.index:02d}"
        (d / f"{stem}.stdout").write_text(run.stdout)
        (d / f"{stem}.stderr").write_text(run.stderr)
        return {
            "index": run.index,
            "kind": run.kind,
            "mode": run.mode,
            "argv": run.argv,
            "cwd": run.cwd,
            "env": run.env,
            "exit": run.exit,
            "duration": run.duration,
            "tags": sorted(run.tags),
            "stdout": f"scenarios/{sid}/{stem}.stdout",
            "stderr": f"scenarios/{sid}/{stem}.stderr",
        }

    def add(self, result: Result, state: dict[str, Any] | None) -> None:
        self.results.append(result)
        d = self.scenario_dir(result.id)
        if state is not None:
            (d / "state.json").write_text(json.dumps(state, indent=2) + "\n")
        self._json.append(self.result_json(result))
        self.write_results()

    def add_setup(self, setup_id: str, ctx: Ctx, error: str) -> dict[str, Any]:
        """A scenario's group setup: its runs are evidence too, but it
        is not a scenario and never counts as a pass."""
        entry = {
            "id": setup_id,
            "ok": not error,
            "error": error,
            "notes": ctx.notes,
            "runs": [self.write_run(setup_id, run) for run in ctx.runs],
        }
        return entry

    def result_json(self, r: Result) -> dict[str, Any]:
        return {
            "id": r.id,
            "group": r.group,
            "title": r.title,
            "verdict": r.verdict,
            "evidence": r.evidence,
            "reason": r.reason,
            "duration": r.duration,
            "checks": [dataclasses.asdict(c) for c in r.checks],
            "notes": r.notes,
            "runs": [self.write_run(r.id, run) for run in r.runs],
            "state": f"scenarios/{r.id}/state.json"
            if (self.out / "scenarios" / r.id / "state.json").exists()
            else None,
        }

    def write_results(self) -> None:
        payload = {
            "schemaVersion": BUNDLE_SCHEMA_VERSION,
            "results": self._json,
        }
        tmp = self.out / ".results.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.out / "results.json")

    def counts(self) -> dict[str, dict[str, int]]:
        table: dict[str, dict[str, int]] = {}
        for r in self.results:
            row = table.setdefault(r.group, dict.fromkeys(VERDICTS, 0))
            row[r.verdict] += 1
        return table

    def write_manifest(self, manifest: dict[str, Any]) -> None:
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def write_summary(self, manifest: Mapping[str, Any]) -> str:
        text = render_summary(self.results, manifest)
        (self.out / "summary.md").write_text(text)
        return text


def run_exit_status(
    results: Sequence[Result], required_missing: Sequence[str], interrupted: bool
) -> int:
    """1 when any scenario failed, a required prerequisite was missing, or the
    run was interrupted; else 0. Skips and known bugs never make it pass or
    fail: they are reported on their own."""
    failed = any(r.verdict == "fail" for r in results)
    return 1 if failed or required_missing or interrupted else 0


def render_summary(results: Sequence[Result], manifest: Mapping[str, Any]) -> str:
    groups: dict[str, dict[str, int]] = {}
    for r in results:
        groups.setdefault(r.group, dict.fromkeys(VERDICTS, 0))[r.verdict] += 1
    total = {v: sum(g[v] for g in groups.values()) for v in VERDICTS}
    verdict = "FAILED" if manifest.get("exit_status") else "PASSED"
    lines = [
        f"## treebox e2e evidence: {verdict}",
        "",
        f"Commit `{manifest.get('commit')}` · treebox `{manifest.get('treebox_version')}` · "
        f"{manifest.get('platform')}",
        "",
        "| Group | Title | Pass | Fail | Skip | Known bug |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    titles = {g["letter"]: g["title"] for g in manifest.get("groups", [])}
    for letter in sorted(groups, key=_group_order):
        g = groups[letter]
        ran = g["pass"] or g["fail"] or g["known-bug"]
        status = " (skipped)" if g["skip"] and not ran else ""
        lines.append(
            f"| {letter}{status} | {titles.get(letter, '')} | {g['pass']} | {g['fail']} "
            f"| {g['skip']} | {g['known-bug']} |"
        )
    lines.append(
        f"| **all** | | **{total['pass']}** | **{total['fail']}** | **{total['skip']}** "
        f"| **{total['known-bug']}** |"
    )
    missing = [p for p in manifest.get("required_missing", [])]
    if missing:
        lines += ["", f"Required prerequisite missing: {', '.join(missing)} (the run fails)."]
    failed = [r for r in results if r.verdict == "fail"]
    if failed:
        lines += ["", "### Failed", ""]
        for r in failed:
            lines.append(f"- **{r.id}** {r.title}: {r.reason}")
    known = [r for r in results if r.verdict == "known-bug"]
    if known:
        lines += ["", "### Known product bugs (reproduced; not passed, not failing the run)", ""]
        for r in known:
            lines.append(f"- **{r.id}** {r.title}: {r.reason}")
    skipped = [r for r in results if r.verdict == "skip"]
    if skipped:
        lines += ["", "### Skipped (not passed)", ""]
        reasons: dict[str, list[str]] = {}
        for r in skipped:
            reasons.setdefault(r.reason, []).append(r.id)
        for reason, ids in reasons.items():
            lines.append(f"- {', '.join(ids)}: {reason}")
    lines.append("")
    return "\n".join(lines)


def _group_order(letter: str) -> tuple[int, str]:
    order = "ABCDEFGHIPRUXZ"
    return (order.index(letter) if letter in order else len(order), letter)


# --- invariants over the whole run (X1 - X4) ---------------------------------------


def golden_schema_version() -> int | None:
    versions = {
        int(m)
        for f in GOLDEN_DIR.glob("*.txt")
        for m in re.findall(r'"schemaVersion": (\d+)', f.read_text())
    }
    return versions.pop() if len(versions) == 1 else None


def stream_violations(run: RunRecord) -> list[str]:
    """X1: data on stdout, diagnostics on stderr, for machine-output runs."""
    if run.kind != "cli" or run.mode != "pipe" or "agent-exit" in run.tags:
        return []
    problems: list[str] = []
    who = _short_argv(run)
    if run.has_flag("--json"):
        if run.command == "doctor" or run.exit == 0:
            try:
                json.loads(run.stdout)
            except json.JSONDecodeError:
                problems.append(f"{who}: stdout is not one JSON document")
        elif run.exit in DOCUMENTED_EXIT_CODES:
            # A teardown that did not finish prints its records first, then
            # fails with TEARDOWN_INCOMPLETE (docs/usage.md): stdout is that
            # one payload. Every other failure writes nothing to stdout.
            incomplete = run.command in ("teardown", "rm") and '"TEARDOWN_INCOMPLETE"' in run.stderr
            if incomplete:
                try:
                    json.loads(run.stdout)
                except json.JSONDecodeError:
                    problems.append(f"{who}: incomplete teardown's stdout is not one JSON document")
            elif run.stdout.strip():
                problems.append(f"{who}: failed --json run wrote to stdout")
            try:
                payload = json.loads(run.stderr)
                if not isinstance(payload, dict) or "error" not in payload:
                    problems.append(f"{who}: stderr JSON has no error object")
            except json.JSONDecodeError:
                problems.append(f"{who}: stderr is not one JSON error object")
    elif run.has_flag("--print") and run.exit == 0:
        if len(run.stdout.splitlines()) != 1:
            problems.append(f"{who}: --print stdout is not exactly one line")
    elif (run.has_flag("--dry-run") or run.has_flag("-n")) and run.exit == 0 and run.stdout.strip():
        problems.append(f"{who}: human --dry-run wrote to stdout")
    return problems


def json_payloads(run: RunRecord) -> list[Any]:
    payloads: list[Any] = []
    if run.kind != "cli" or run.mode != "pipe" or not run.has_flag("--json"):
        return payloads
    for text in (run.stdout, run.stderr):
        with contextlib.suppress(json.JSONDecodeError):
            if text.strip():
                payloads.append(json.loads(text))
    return payloads


def invariant_results(all_runs: Sequence[tuple[str, RunRecord]]) -> list[Result]:
    """The cross-cutting invariants, judged over every CLI run of the sweep."""
    cli = [(sid, r) for sid, r in all_runs if r.kind == "cli"]
    out: list[Result] = []

    def result(sid: str, title: str, problems: list[str], checked: int) -> Result:
        checks = [Check(False, p) for p in problems] or [Check(True, f"{checked} CLI runs checked")]
        return Result(
            id=sid,
            group="X",
            title=title,
            verdict="fail" if problems else "pass",
            evidence="live",
            reason=problems[0] if problems else f"{checked} CLI runs checked",
            checks=checks,
            runs=[],
            notes=[],
            duration=0.0,
        )

    x1 = [f"{sid}: {p}" for sid, r in cli for p in stream_violations(r)]
    out.append(result("X1", "stream discipline", x1, len(cli)))
    x2 = [
        f"{sid}: {_short_argv(r)} exited {r.exit}"
        for sid, r in cli
        if r.exit not in DOCUMENTED_EXIT_CODES and "agent-exit" not in r.tags
    ]
    out.append(result("X2", "exit-code table", x2, len(cli)))
    expected = golden_schema_version()
    x3: list[str] = []
    if expected is None:
        x3.append("tests/golden does not carry one schemaVersion")
    for sid, r in cli:
        for payload in json_payloads(r):
            if not isinstance(payload, dict) or payload.get("schemaVersion") != expected:
                got = payload.get("schemaVersion") if isinstance(payload, dict) else None
                x3.append(f"{sid}: {_short_argv(r)} schemaVersion {got!r} != {expected}")
    out.append(result("X3", "schemaVersion", x3, len(cli)))
    x4 = [
        f"{sid}: {_short_argv(r)} hit a held lock"
        for sid, r in cli
        if "lock-expected" not in r.tags and '"LOCK_HELD"' in r.stderr
    ]
    out.append(result("X4", "worktree lock released", x4, len(cli)))
    return out


# --- host pollution (X5) -----------------------------------------------------------


def snapshot_tree(path: Path) -> dict[str, tuple[int, int]]:
    if not path.exists():
        return {}
    snap: dict[str, tuple[int, int]] = {}
    for p in [path, *path.rglob("*")]:
        with contextlib.suppress(OSError):
            st = p.lstat()
            snap[str(p.relative_to(path))] = (st.st_size, int(st.st_mtime))
    return snap


def git_status(project: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
    )
    return proc.stdout


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def describe_platform() -> str:
    return f"{platform.system()} {platform.release()} {platform.machine()}"


def format_exception() -> str:
    return traceback.format_exc(limit=8)

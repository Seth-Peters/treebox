"""The harness registry: one deep module for agent-CLI wiring.

A *harness* is the coding-agent CLI treebox launches inside a worktree
(``claude``, ``codex``). Everything treebox knows about a harness — its
autonomous launch argv, where its subscription login lives on the host, which
login files a sandbox gets copies of, and how to tell a user to log in —
lives behind the ``Harness`` interface below. Adding a harness is one
registry entry plus the matching ``config.Harness`` Literal alias update; the
drift test pins those vocabularies together.

Invariants (part of this module's interface, relied on by every caller):

- **Subscription auth only.** Agents authenticate via their host login dirs;
  ``ANTHROPIC_API_KEY`` is never used.
- **Live login dirs are never mounted into a sandbox.** They hold
  host-executed config (e.g. ``settings.json`` hooks run with the operator's
  full host privileges), so sandboxes only ever see the scoped, disposable
  copies ``stage_credentials`` produces.
- **Staged copies are refreshed on every entry — auth is not a cache.** A
  fresh host login and a host logout/revocation both propagate to the staged
  copies the next time a worktree is entered.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

# Seconds a Keychain read may wait for a person to answer a macOS dialog.
KEYCHAIN_TIMEOUT = 30


@dataclass(frozen=True)
class Harness:
    """One coding agent's complete wiring behind a small interface.

    ``name`` is the persisted identity — recorded in state files, accepted by
    ``--harness``, shown by ``list`` — and is stable forever. The private
    fields are wiring no caller may read: consumers go through the methods,
    so a harness whose credentials are *behavior* rather than plain files
    (generated settings, non-file auth stores) extends this class instead of
    growing branches in every caller.
    """

    name: str
    # The full fully-autonomous launch argv.
    _argv: tuple[str, ...]
    # Host dir the subscription login lives in. Kept un-expanded so human
    # output (doctor advice, launch errors) shows the familiar ~/ form.
    _credential_dir: str
    # The only login files staged into sandboxes: what the agent needs to
    # authenticate plus its user-level settings. Everything else in the live
    # dir (state, history, host-executed hooks) stays on the host.
    _credential_files: tuple[str, ...]
    # The one staged file that holds the subscription login itself (a JSON
    # object); the other staged files are settings.
    _login_file: str
    # The macOS Keychain service where the CLI keeps its login on that
    # platform instead of ``_login_file`` (None: the file is the only store).
    _keychain_service: str | None = None

    def __post_init__(self) -> None:
        if self._login_file not in self._credential_files:
            raise ValueError(f"{self.name}: the login file must be one of the staged files")

    def launch_argv(self, extra: Sequence[str]) -> list[str]:
        """The exact fully-autonomous agent argv (extra args appended verbatim)."""
        return [*self._argv, *extra]

    def binary(self) -> str:
        """The executable the launch argv starts (looked up on PATH)."""
        return self._argv[0]

    def login_dir(self) -> str:
        """The host login dir as shown to humans (``~/.claude``)."""
        return self._credential_dir

    def credential_path(self) -> Path:
        """The expanded host login dir: doctor's row detail, staging's source."""
        return Path(self._credential_dir).expanduser()

    def login_file(self) -> Path:
        """The host file that holds the subscription login: the one file a
        sandbox needs a copy of to authenticate."""
        return self.credential_path() / self._login_file

    def login(self) -> str | None:
        """Where the login an agent uses (and a sandbox gets a copy of) lives
        on this machine, or None: a non-empty JSON object from ``_read``.
        """
        try:
            where, content = self._read(self._login_file) or ("", b"")
            data = json.loads(content)
        except (OSError, ValueError):
            return None
        return where if isinstance(data, dict) and data else None

    def _read(self, name: str) -> tuple[str, bytes] | None:
        """``(where, contents)`` of one staged file, or None. The one rule:
        the host file first; for the login file only, when it is absent on
        macOS, this user's item in the harness's Keychain service. The read
        has a time limit and no stdin: an unanswered or denied macOS dialog
        is None, never a wait. A stale item of another account can exist.
        """
        path = self.credential_path() / name
        if path.is_file():
            return str(path), path.read_bytes()
        if name != self._login_file or not self._keychain_service or sys.platform != "darwin":
            return None
        item = ["-a", getpass.getuser(), "-s", self._keychain_service, "-w"]
        try:
            read = subprocess.run(
                ["security", "find-generic-password", *item],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=KEYCHAIN_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if read.returncode != 0:
            return None
        return f"macOS Keychain ({self._keychain_service})", read.stdout.removesuffix(b"\n")

    def login_hint(self) -> str:
        """How to obtain a login, for launch errors and doctor advice."""
        where = self._credential_dir
        if self._keychain_service:
            where += (
                f"; on macOS the Keychain item '{self._keychain_service}': allow access if asked"
            )
        return f"Install the {self.name} CLI and log in (writes {where}) before launching."

    def stage_credentials(self, dest: Path) -> None:
        """Stage scoped copies of the login files into ``dest`` (created 0700).

        Copy-or-drop semantics: a present source (see ``_read``) is copied (0600) so
        in-container token refresh and agent state work against the copy; a
        staged copy whose source is gone (host logout) is deleted. Both a
        re-login and a revocation therefore reach the sandbox on the next
        entry. The live login dir itself is never touched. Filesystem errors
        raise ``RuntimeError``; see ``docs/usage.md`` for recovery guidance.

        The caller must keep ``dest.parent`` under host control and outside
        all sandbox mounts. Only ``dest`` may be mounted into the sandbox.
        """
        # Prepare complete files in the host-owned parent, then atomically
        # replace destination entries. Never open or chmod a sandbox-controlled
        # file for writing.
        # Pin the mounted directory so a replacement cannot redirect writes.
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with ExitStack() as stack:
                parent_fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
                stack.callback(os.close, parent_fd)
                with suppress(FileExistsError):
                    os.mkdir(dest.name, mode=0o700, dir_fd=parent_fd)
                dest_fd = os.open(
                    dest.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
                stack.callback(os.close, dest_fd)
                for name in self._credential_files:
                    if found := self._read(name):
                        _replace_credential(found[1], name, parent_fd, dest_fd)
                    else:
                        with suppress(FileNotFoundError):
                            os.unlink(name, dir_fd=dest_fd)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot safely refresh credentials at {dest}: {exc.strerror}. "
                "Check the staged credential directory and remove conflicting directories."
            ) from exc

    def sandbox_mount(self, staged: Path, user: str) -> tuple[Path, str]:
        """Where a sandbox mounts the staged copies: ``(host source, target)``.

        The target is the in-container path the agent CLI reads its login
        from (the sandbox template points the CLI's config-dir env var at
        it), for the in-container user ``user``.
        """
        return staged, f"/home/{user}/.{self.name}"


def _replace_credential(content: bytes, name: str, parent_fd: int, dest_fd: int) -> None:
    """Publish a private host-side copy without following destination links."""
    temporary = f".treebox-credential-{secrets.token_hex(16)}"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(content)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=dest_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)


CLAUDE = Harness(
    name="claude",
    _argv=("claude", "--dangerously-skip-permissions"),
    _credential_dir="~/.claude",
    _credential_files=(".credentials.json", "settings.json"),
    _login_file=".credentials.json",
    _keychain_service="Claude Code-credentials",
)

CODEX = Harness(
    name="codex",
    _argv=("codex", "--dangerously-bypass-approvals-and-sandbox"),
    _credential_dir="~/.codex",
    _credential_files=("auth.json", "config.toml"),
    _login_file="auth.json",
)

# The registry. Order is user-facing: doctor's login rows and help text list
# harnesses in this order, and the first entry is the default whose CLI the
# no-login advice names.
HARNESSES: tuple[Harness, ...] = (CLAUDE, CODEX)

VALID_HARNESSES: tuple[str, ...] = tuple(h.name for h in HARNESSES)


def get_harness(name: str) -> Harness:
    """Resolve a validated boundary name to its ``Harness`` — loud on unknown.

    Boundary values (CLI flags, TOML, state files) arrive as plain ``str``
    and are validated where they arrive (``config.validate_config``, the
    state fallbacks); internal seams take the resolved object.
    """
    for harness in HARNESSES:
        if harness.name == name:
            return harness
    raise ValueError(f"Unknown harness '{name}'. Use one of {VALID_HARNESSES}.")

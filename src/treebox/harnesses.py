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

import json
import os
import secrets
import shutil
import subprocess
import sys
from collections.abc import Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path


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

    def sandbox_login(self) -> str | None:
        """Where a sandbox's login comes from, or None when there is none.

        A sandbox only ever gets a copy of ``login_file``, so only a real
        login file counts: a regular file that holds a non-empty JSON object.
        A login dir that is merely non-empty (settings, history) is no login.
        """
        path = self.login_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return str(path) if isinstance(data, dict) and data else None

    def host_login(self) -> str | None:
        """Where an agent launched on the host finds its login, or None.

        The login file first; on macOS, also the Keychain item the CLI keeps
        its login in there. The Keychain probe reads only the item's
        attributes (never the secret), so it never asks for anything.
        """
        found = self.sandbox_login()
        if found or self._keychain_service is None or sys.platform != "darwin":
            return found
        try:
            probe = subprocess.run(
                ["security", "find-generic-password", "-s", self._keychain_service],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return f"macOS Keychain ({self._keychain_service})" if probe.returncode == 0 else None

    def login_hint(self) -> str:
        """How to obtain a login, for launch errors and doctor advice."""
        return (
            f"Install the {self.name} CLI and log in "
            f"(writes {self._credential_dir}) before launching."
        )

    def stage_credentials(self, dest: Path) -> None:
        """Stage scoped copies of the login files into ``dest`` (created 0700).

        Copy-or-drop semantics: a present source file is copied (0600) so
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
                src_dir = self.credential_path()
                for name in self._credential_files:
                    src = src_dir / name
                    if src.is_file():
                        _replace_credential(src, name, parent_fd, dest_fd)
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


def _replace_credential(src: Path, name: str, parent_fd: int, dest_fd: int) -> None:
    """Publish a private host-side copy without following destination links."""
    temporary = f".treebox-credential-{secrets.token_hex(16)}"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as source:
            os.fchmod(out.fileno(), 0o600)
            shutil.copyfileobj(source, out)
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

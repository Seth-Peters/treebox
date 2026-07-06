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

import shutil
from collections.abc import Sequence
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

    def launch_argv(self, extra: Sequence[str]) -> list[str]:
        """The exact fully-autonomous agent argv (extra args appended verbatim)."""
        return [*self._argv, *extra]

    def login_dir(self) -> str:
        """The host login dir as shown to humans (``~/.claude``)."""
        return self._credential_dir

    def credential_path(self) -> Path:
        """The expanded host login dir: doctor's row detail, staging's source."""
        return Path(self._credential_dir).expanduser()

    def credentials_present(self) -> bool:
        """True when the subscription-login dir exists and is non-empty."""
        d = self.credential_path()
        return d.is_dir() and any(d.iterdir())

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
        entry. The live login dir itself is never touched.
        """
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        src_dir = self.credential_path()
        for name in self._credential_files:
            src, dst = src_dir / name, dest / name
            if src.is_file():
                shutil.copy2(src, dst)
                dst.chmod(0o600)
            elif dst.is_file():
                dst.unlink()

    def sandbox_mount(self, staged: Path, user: str) -> tuple[Path, str]:
        """Where a sandbox mounts the staged copies: ``(host source, target)``.

        The target is the in-container path the agent CLI reads its login
        from (the sandbox template points the CLI's config-dir env var at
        it), for the in-container user ``user``.
        """
        return staged, f"/home/{user}/.{self.name}"


CLAUDE = Harness(
    name="claude",
    _argv=("claude", "--dangerously-skip-permissions"),
    _credential_dir="~/.claude",
    _credential_files=(".credentials.json", "settings.json"),
)

CODEX = Harness(
    name="codex",
    _argv=("codex", "--dangerously-bypass-approvals-and-sandbox"),
    _credential_dir="~/.codex",
    _credential_files=("auth.json", "config.toml"),
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

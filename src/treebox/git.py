"""Typed wrappers over the ``git`` subprocess.

Everything the tool needs from git lives here so the rest of the code never
shells out to git directly. Functions raise ``GitError`` on failure.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from .models import same_path

# Config keys git would use to run code *on the host* when we invoke it. The
# docker runner mounts the repo's git common dir writable so in-container git
# can commit/fetch (see runners/docker.py), so a boxed agent can write the
# shared ``.git/config``. Pinning these per-invocation with ``-c`` means a
# hostile ``core.hooksPath`` / ``core.fsmonitor`` (or a paging command) planted
# there is ignored whenever *treebox* shells out to git — without touching the
# agent's own in-container git, which resolves its config independently and
# stays free to use hooks/fsmonitor normally.
_SAFE_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.pager=cat",
)


def _git(args: list[str]) -> list[str]:
    """A git argv with the host-safety config pinned ahead of the subcommand."""
    return ["git", *_SAFE_CONFIG, *args]


class GitError(RuntimeError):
    pass


class FetchError(GitError):
    """A required ``git fetch`` could not complete (usually auth/network)."""


def _run(
    args: list[str], *, cwd: str | Path | None = None, env: dict[str, str] | None = None
) -> str:
    proc = subprocess.run(
        _git(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError((proc.stderr or proc.stdout or "").strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def have_git() -> bool:
    from shutil import which

    return which("git") is not None


def version() -> tuple[int, int, int]:
    out = _run(["--version"]).strip()  # "git version 2.43.0"
    parts = out.split()[-1].split(".")
    nums = []
    for p in parts[:3]:
        digits = "".join(c for c in p if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def version_str() -> str:
    return ".".join(str(n) for n in version())


def repo_root(start: str | Path = ".") -> str:
    try:
        return _run(["-C", str(start), "rev-parse", "--show-toplevel"]).strip()
    except GitError as exc:
        raise GitError(f"Not inside a git repository: {start}") from exc


def main_worktree(start: str | Path = ".") -> str:
    """The repo's *main* worktree, resolved from anywhere inside it — including a
    linked worktree, where ``--show-toplevel`` would return the linked tree
    instead. treebox anchors every command here so ``.treebox/`` and the worktree
    root are computed against the same repo no matter the caller's CWD.

    ``git worktree list --porcelain`` always lists the main worktree first."""
    out = _run(["-C", str(start), "worktree", "list", "--porcelain"])
    for line in out.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree ") :].strip()
    # No worktree line should be impossible for a valid repo, but fall back to the
    # plain toplevel rather than crash.
    return repo_root(start)


def git_dir(path: str | Path) -> str:
    """Absolute per-worktree git dir (``.git/worktrees/<id>`` for a linked tree)."""
    out = _run(["-C", str(path), "rev-parse", "--absolute-git-dir"]).strip()
    return out


def common_dir(repo: str | Path) -> Path:
    """Absolute git common dir for ``repo`` (the main repo's ``.git``) — what a
    linked worktree's gitdir pointers ultimately resolve into."""
    out = _run(["-C", str(repo), "rev-parse", "--git-common-dir"]).strip()
    p = Path(out)
    if not p.is_absolute():
        p = Path(os.path.normpath(Path(repo) / p))
    return p


def info_exclude_path(worktree: str | Path) -> Path:
    """The local exclude file git actually consults for ``worktree``.

    For a linked worktree this resolves through the common git dir, so entries
    written here are ignored everywhere but never tracked or pushed.
    """
    out = _run(["-C", str(worktree), "rev-parse", "--git-path", "info/exclude"]).strip()
    path = Path(out)
    return path if path.is_absolute() else Path(worktree) / path


class LastCommit(NamedTuple):
    """HEAD's commit subject and unix epoch — what ``list`` shows as
    LAST COMMIT / AGE. ``("", 0)`` when there is no commit to describe."""

    subject: str
    epoch: int


def last_commit(worktree: str | Path) -> LastCommit:
    try:
        out = _run(["-C", str(worktree), "log", "-1", "--format=%ct%x00%s"]).strip()
    except GitError:
        return LastCommit("", 0)
    epoch, _, subject = out.partition("\x00")
    try:
        return LastCommit(subject, int(epoch))
    except ValueError:
        return LastCommit("", 0)


def enable_worktree_config(worktree: str | Path) -> None:
    """Turn on ``extensions.worktreeConfig`` so per-worktree config (and thus a
    per-worktree ``core.hooksPath``) exists at all. Writes the shared config;
    idempotent."""
    _run(["-C", str(worktree), "config", "extensions.worktreeConfig", "true"])


def set_worktree_hooks_path(worktree: str | Path, hooks_dir: str | Path) -> None:
    """Point THIS worktree's ``core.hooksPath`` at ``hooks_dir`` (the pre-push
    guard), leaving the shared repo hooks and every other worktree alone.
    Requires ``enable_worktree_config`` first."""
    _run(["-C", str(worktree), "config", "--worktree", "core.hooksPath", str(hooks_dir)])


def check_ref_format(branch: str) -> bool:
    try:
        _run(["check-ref-format", "--branch", branch])
        return True
    except GitError:
        return False


def local_branch_exists(repo: str | Path, name: str) -> bool:
    return _ref_exists(repo, f"refs/heads/{name}")


def remote_branch_exists(repo: str | Path, name: str) -> bool:
    return _ref_exists(repo, f"refs/remotes/origin/{name}")


def _ref_exists(repo: str | Path, ref: str) -> bool:
    proc = subprocess.run(
        _git(["-C", str(repo), "show-ref", "--verify", "--quiet", ref]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


class HttpsRemote(NamedTuple):
    """A remote rewritten for token-authenticated HTTPS access."""

    host: str
    https_url: str


def _https_remote(url: str) -> HttpsRemote | None:
    """Normalize a git remote URL to its host + HTTPS form.

    Handles the three git remote URL shapes — scp-like (``git@host:owner/repo``),
    ``ssh://``, and ``https://`` — so we can re-fetch over HTTPS with a token.
    Returns None for anything we can't confidently rewrite (e.g. local paths).
    """
    url = url.strip()
    if url.startswith(("https://", "http://", "ssh://")):
        rest = url.split("://", 1)[1]
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    elif "@" in url and ":" in url and "://" not in url:
        authority, _, path = url.partition(":")  # scp-like git@host:owner/repo.git
        host = authority.rsplit("@", 1)[-1]
    else:
        return None
    path = path.strip("/")
    if not host or not path:
        return None
    if not path.endswith(".git"):
        path += ".git"
    return HttpsRemote(host, f"https://{host}/{path}")


def _origin_https(repo: str | Path) -> HttpsRemote | None:
    """Origin's host + HTTPS URL, regardless of host or provider.

    Returns None when there is no origin or its URL can't be rewritten to HTTPS
    (e.g. a local path remote).
    """
    proc = subprocess.run(
        _git(["-C", str(repo), "remote", "get-url", "origin"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return _https_remote(proc.stdout)


class HostCli(NamedTuple):
    """A logged-in host CLI usable as a git credential helper: its binary, the
    probe argv that exits 0 only when it holds a credential for a given host,
    and the helper value to wire into git config."""

    binary: str
    probe: list[str]
    helper: str


# Probed against the real host (not the hostname's shape) so self-hosted
# GitHub Enterprise / GitLab resolve to the right tool.
_HOST_CLIS: tuple[HostCli, ...] = (
    HostCli("gh", ["gh", "auth", "token", "--hostname", "{host}"], "!gh auth git-credential"),
    HostCli(
        "glab",
        ["glab", "auth", "status", "--hostname", "{host}"],
        "!glab auth git-credential",
    ),
)


def _cred_config_for(host: str) -> list[str]:
    """``-c`` flags wiring a host CLI's token as git's credential helper.

    Returns the flags for the first installed CLI that claims ``host``. The
    empty first value clears any inherited (and possibly broken) helper for the
    host so only the CLI is consulted — the wiring ``gh auth setup-git`` does,
    scoped to a single invocation. Returns ``[]`` when no host CLI applies, in
    which case the HTTPS fetch still benefits from git's own configured helpers
    (credential-manager, keychain, store) and env tokens, or is simply public.
    """
    from shutil import which

    for binary, probe, helper in _HOST_CLIS:
        if which(binary) is None:
            continue
        args = [a.format(host=host) for a in probe]
        if (
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            == 0
        ):
            return [
                "-c",
                f"credential.https://{host}.helper=",
                "-c",
                f"credential.https://{host}.helper={helper}",
            ]
    return []


def _batch_env() -> dict[str, str]:
    """The env every silent git network call runs under: no terminal prompts
    and ssh in BatchMode, so a missing credential fails fast instead of asking.

    The single definition of 'non-interactive' shared by the fetch cascade and
    doctor's reachability probe — they must agree on what never-prompts means.
    """
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_SSH_COMMAND", env.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes")
    return env


@dataclass(frozen=True)
class _SilentAttempt:
    """One silent credential path: the ``-c`` credential flags, the remote to
    hit, and (for fetch only) the refspec mapping it back onto origin's refs."""

    cred_config: tuple[str, ...]
    remote: str
    refspec: tuple[str, ...] = ()


def _silent_attempts(repo: str | Path) -> Iterator[_SilentAttempt]:
    """The ordered non-interactive credential paths, in cascade order.

    1. **Ambient**: hit ``origin`` as configured, under ``_batch_env()`` —
       succeeds outright when an ssh-agent is loaded or an HTTPS credential is
       cached. Never prompts.
    2. **HTTPS rewrite**: retry over HTTPS so git's credential system (a host
       CLI token like ``gh``/``glab``, a configured helper, an env token, or
       public access) can authenticate. Rescues the common box whose remote is
       SSH but has no ssh-agent loaded. Skipped when origin can't be rewritten
       to HTTPS (e.g. a local path remote). Never prompts.

    The single source of the cascade: ``fetch_origin`` runs ``fetch`` over
    these attempts and ``origin_reachable`` runs ``ls-remote``, so doctor's
    prediction and the real fetch stay in lockstep by construction. Lazy, so
    the HTTPS rewrite (which probes host CLIs) only runs when ambient failed.
    """
    yield _SilentAttempt((), "origin")
    remote = _origin_https(repo)
    if remote is not None:
        host, https_url = remote
        yield _SilentAttempt(
            tuple(_cred_config_for(host)),
            https_url,
            ("+refs/heads/*:refs/remotes/origin/*",),
        )


def _silent_fetch(repo: str | Path, attempt: _SilentAttempt) -> tuple[int, str]:
    """Strictly non-interactive ``git fetch`` over one cascade attempt."""
    proc = subprocess.run(
        _git(
            [
                "-C",
                str(repo),
                *attempt.cred_config,
                "fetch",
                "--quiet",
                "--",
                attempt.remote,
                *attempt.refspec,
            ]
        ),
        env=_batch_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode, (proc.stdout or "").strip()


def _fetch_prompt(repo: str | Path) -> int:
    """Terminal-attached ``git fetch origin`` so ssh/git can prompt.

    Inherits stdio so an SSH key passphrase or HTTPS credential prompt reaches
    the user, then continues — exactly like a manual ``git pull``.
    """
    return subprocess.run(
        _git(["-C", str(repo), "fetch", "origin", "--quiet"]), env=dict(os.environ)
    ).returncode


def fetch_origin(repo: str | Path, *, required: bool, interactive: bool = False) -> bool:
    """Fetch origin, trying the least-disruptive credential path that works.

    A three-step cascade:

    1. **Silent ambient fetch** under ``BatchMode`` — succeeds outright when an
       ssh-agent is loaded or an HTTPS credential is cached. Never prompts.
    2. **Silent HTTPS fallback** — retry over HTTPS so git's credential system
       (host CLI token like ``gh``/``glab``, a credential helper, an env token,
       or public access) can authenticate. Rescues the common box whose remote
       is SSH but has no ssh-agent loaded. Never prompts.
    3. **Interactive prompt** (only when ``interactive`` — a TTY is attached):
       a terminal-attached fetch so ssh/git can ask for the key passphrase or
       credentials, then continue, like a manual ``git pull``. Run last so a
       working silent path never bothers the user.

    Returns True on success. If all steps fail and ``required``, raises
    FetchError; if not ``required``, returns False.
    """
    detail = ""
    for attempt in _silent_attempts(repo):
        code, out = _silent_fetch(repo, attempt)
        if code == 0:
            return True
        detail = out or detail

    if interactive and _fetch_prompt(repo) == 0:
        return True

    if required:
        detail = detail.strip().rstrip(".")
        raise FetchError("Could not fetch origin" + (f": {detail}." if detail else "."))
    return False


def origin_reachable(repo: str | Path) -> bool | None:
    """Whether origin is reachable non-interactively (for doctor).

    Probes the exact silent cascade ``fetch_origin`` uses (via
    ``_silent_attempts``), running ``ls-remote`` instead of a full fetch, so
    doctor's verdict predicts what a real fetch will do. Returns None when no
    origin remote is configured, else True/False.
    """
    if not has_origin(repo):
        return None
    for attempt in _silent_attempts(repo):
        proc = subprocess.run(
            _git(
                [
                    "-C",
                    str(repo),
                    *attempt.cred_config,
                    "ls-remote",
                    "--quiet",
                    "--exit-code",
                    attempt.remote,
                    "HEAD",
                ]
            ),
            env=_batch_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0:
            return True
    return False


def origin_host(repo: str | Path) -> str | None:
    """Hostname of the ``origin`` remote (``github.com``, ``gitlab.com``, a
    self-hosted host, ...), or None when there is no origin or its URL is a
    local path we can't attribute to a forge. The forge providers key on this."""
    parsed = _origin_https(repo)
    return parsed[0] if parsed else None


def upstream_of(worktree: str | Path) -> str | None:
    """The upstream tracking ref for HEAD (e.g. ``origin/feat/x``), or None when
    the branch has no configured upstream (never pushed / detached). Local, no
    network — forge- and auth-agnostic."""
    proc = subprocess.run(
        _git(
            [
                "-C",
                str(worktree),
                "rev-parse",
                "--abbrev-ref",
                "--symbolic-full-name",
                "@{upstream}",
            ]
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


class AheadBehind(NamedTuple):
    """Commit counts of HEAD versus its upstream."""

    ahead: int
    behind: int


def ahead_behind(worktree: str | Path) -> AheadBehind | None:
    """Ahead/behind commit counts of HEAD versus its upstream, or None when
    there is no upstream to compare against. Local, no network."""
    proc = subprocess.run(
        _git(["-C", str(worktree), "rev-list", "--left-right", "--count", "@{upstream}...HEAD"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if len(parts) != 2:
        return None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return AheadBehind(ahead=ahead, behind=behind)


def is_merged_into(repo: str | Path, branch: str, base: str) -> bool:
    """Whether ``branch`` is an ancestor of ``origin/<base>`` (its commits are
    already contained there). Catches merge-commit / fast-forward / rebase
    merges against whatever ``origin/<base>`` was last fetched. A *squash* merge
    rewrites history and is NOT detected here — the forge PR/MR state is the
    authority for that."""
    target = f"origin/{base}" if remote_branch_exists(repo, base) else base
    proc = subprocess.run(
        _git(["-C", str(repo), "merge-base", "--is-ancestor", branch, target]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def has_origin(repo: str | Path) -> bool:
    proc = subprocess.run(
        _git(["-C", str(repo), "remote", "get-url", "origin"]),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


BranchPlanKind = Literal["local", "track-remote", "new"]


@dataclass(frozen=True)
class BranchPlan:
    """How a worktree's branch will be materialized."""

    kind: BranchPlanKind
    name: str
    start_point: str | None  # ref to branch from (for track-remote / new)


def resolve_branch(repo: str | Path, name: str, base: str) -> BranchPlan:
    """Decide how to create the worktree's branch (target-state §create step 2)."""
    if local_branch_exists(repo, name):
        return BranchPlan("local", name, None)
    if remote_branch_exists(repo, name):
        return BranchPlan("track-remote", name, f"origin/{name}")
    # New branch: prefer freshly-fetched origin/<base> over a possibly-stale local.
    start = f"origin/{base}" if remote_branch_exists(repo, base) else base
    return BranchPlan("new", name, start)


def worktree_add(repo: str | Path, worktree: str | Path, plan: BranchPlan) -> None:
    args = ["-C", str(repo), "worktree", "add"]
    # "--" so a flag-shaped path/ref is always parsed as an operand.
    if plan.kind == "local":
        args += ["--", str(worktree), plan.name]
    else:
        args += ["-b", plan.name, "--", str(worktree), plan.start_point or ""]
    _run(args)


def worktree_prune(repo: str | Path) -> None:
    _run(["-C", str(repo), "worktree", "prune"])


def worktree_remove(repo: str | Path, worktree: str | Path, *, force: bool) -> None:
    args = ["-C", str(repo), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree))
    _run(args)


def delete_branch(repo: str | Path, name: str) -> None:
    _run(["-C", str(repo), "branch", "-D", "--", name])


def update_branch(repo: str | Path, name: str, ref: str) -> None:
    """Point the existing branch ``name`` at ``ref`` (not checked out anywhere)."""
    _run(["-C", str(repo), "branch", "-f", "--", name, ref])


def worktree_registered(repo: str | Path, path: str) -> bool:
    """Whether ``path`` is a worktree git itself knows about."""
    return any(same_path(rec.path, path) for rec in worktree_list(repo))


def registered_gitdir(repo: str | Path, path: str | Path) -> Path | None:
    """The per-worktree git dir (``.git/worktrees/<id>``) the repo's own
    registration records for ``path``, found without consulting the worktree's
    ``.git`` pointer - so it still resolves when that pointer is missing or
    corrupt. None when no registration names ``path``."""
    try:
        worktrees = common_dir(repo) / "worktrees"
    except GitError:
        return None
    if not worktrees.is_dir():
        return None
    for admin in sorted(worktrees.iterdir()):
        try:
            pointer = (admin / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if pointer and same_path(Path(pointer).parent, path):
            return admin
    return None


def is_dirty(worktree: str | Path) -> bool:
    # Submodule trees are plain copies with no .git at all (copy_submodules
    # strips it), so ignore submodule state: their dirtiness isn't ours.
    out = _run(["-C", str(worktree), "status", "--porcelain", "--ignore-submodules=all"])
    return bool(out.strip())


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    branch: str | None
    prunable: bool = False  # git's own signal that the working dir is gone


def worktree_list(repo: str | Path) -> list[WorktreeRecord]:
    out = _run(["-C", str(repo), "worktree", "list", "--porcelain"])
    records: list[WorktreeRecord] = []
    path: str | None = None
    branch: str | None = None
    prunable = False
    for line in [*out.splitlines(), ""]:
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            branch = None
            prunable = False
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            branch = ref.removeprefix("refs/heads/")
        elif line == "prunable" or line.startswith("prunable "):
            # A registration whose working dir was removed out from under git
            # (e.g. a manual rm, or a stale nested tree from an old create bug).
            prunable = True
        elif line == "" and path is not None:
            records.append(WorktreeRecord(path, branch, prunable))
            path = None
            branch = None
            prunable = False
    return records


def branch_for_path(repo: str | Path, path: str) -> str | None:
    for rec in worktree_list(repo):
        if same_path(rec.path, path):
            return rec.branch
    return None


def has_gitmodules(repo: str | Path) -> bool:
    return (Path(repo) / ".gitmodules").is_file()


def submodule_paths(repo: str | Path) -> list[str]:
    """Relative paths of configured submodules, from .gitmodules."""
    try:
        out = _run(
            [
                "-C",
                str(repo),
                "config",
                "-f",
                ".gitmodules",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ]
        )
    except GitError:
        return []
    paths = []
    for line in out.splitlines():
        _, _, value = line.partition(" ")
        value = value.strip()
        if value:
            paths.append(value)
    return paths

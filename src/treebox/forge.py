"""Optional pull/merge-request status for the teardown chooser.

Tier-1 status (dirty / ahead / merged-by-ancestor / never-pushed) is pure local
git and forge-agnostic — every user gets it regardless of provider or auth. This
module adds the *optional* Tier-2 signal: whether a branch has an open or merged
PR/MR. That is inherently forge-specific, and it is the only reliable way to see
a **squash** merge (which ``git.is_merged_into`` cannot detect).

It never handles credentials. It delegates entirely to a forge CLI the user has
already logged into — ``gh`` for GitHub, ``glab`` for GitLab — probed against the
*real* ``origin`` host, so github.com / GitHub Enterprise / gitlab.com /
self-hosted GitLab all route to the right tool (the same host-CLI convention
``git._cred_config_for`` uses for fetch). Any failure — no CLI, not authenticated
for that host, unknown host (Bitbucket, raw git), or offline — yields None so the
chooser degrades cleanly to Tier-1.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Literal

from . import git

# How long any single forge CLI call may take before we give up and fall back to
# Tier-1 for that worktree. Keeps a slow/hung network from stalling the picker.
_CLI_TIMEOUT = 6.0


PRState = Literal["open", "draft", "merged", "closed"]


@dataclass(frozen=True)
class PRStatus:
    """A branch's pull/merge request, normalized across forges."""

    state: PRState
    number: int | None = None
    url: str | None = None

    @property
    def merged(self) -> bool:
        return self.state == "merged"

    @property
    def open(self) -> bool:
        return self.state in ("open", "draft")


def _run(argv: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess[str] | None:
    """Run a forge CLI, swallowing every failure mode into None: the CLI may be
    missing (FileNotFoundError), hang (TimeoutExpired), or exit non-zero."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc


class Forge:
    """A forge whose CLI can report PR/MR state for a branch. Bound to a host;
    ``pr_status`` returns None on any failure so callers stay forge-agnostic."""

    cli: str = ""

    def __init__(self, host: str) -> None:
        self.host = host

    def authed(self) -> bool:
        """Whether this forge's CLI is installed and holds a credential for the
        bound host — the gate that decides which provider ``detect`` picks."""
        raise NotImplementedError

    def pr_status(self, repo_path: str, branch: str) -> PRStatus | None:
        raise NotImplementedError


class GhForge(Forge):
    cli = "gh"

    def authed(self) -> bool:
        return _run(["gh", "auth", "token", "--hostname", self.host]) is not None

    def pr_status(self, repo_path: str, branch: str) -> PRStatus | None:
        proc = _run(
            ["gh", "pr", "view", branch, "--json", "state,number,url,isDraft"],
            cwd=repo_path,
        )
        if proc is None:
            return None
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        raw = str(data.get("state", "")).upper()
        state: PRState
        if raw == "MERGED":
            state = "merged"
        elif raw == "CLOSED":
            state = "closed"
        elif raw == "OPEN":
            state = "draft" if data.get("isDraft") else "open"
        else:
            return None
        number = data.get("number")
        return PRStatus(
            state=state,
            number=number if isinstance(number, int) else None,
            url=data.get("url") or None,
        )


class GlabForge(Forge):
    cli = "glab"

    def authed(self) -> bool:
        return _run(["glab", "auth", "status", "--hostname", self.host]) is not None

    def pr_status(self, repo_path: str, branch: str) -> PRStatus | None:
        # glab's default state filter is opened-only; --all is what lets a
        # merged MR surface at all.
        proc = _run(
            [
                "glab",
                "mr",
                "list",
                "--source-branch",
                branch,
                "--all",
                "--output",
                "json",
            ],
            cwd=repo_path,
        )
        if proc is None:
            return None
        try:
            items = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(items, list):
            return None
        statuses = [st for st in (self._parse_mr(mr) for mr in items) if st is not None]
        if not statuses:
            return None
        # A branch can carry several MRs (an abandoned closed one plus the real
        # merged/open one); merged and open outrank closed.
        rank: dict[PRState, int] = {"merged": 0, "open": 1, "draft": 1, "closed": 2}
        return min(statuses, key=lambda st: rank[st.state])

    @staticmethod
    def _parse_mr(mr: object) -> PRStatus | None:
        if not isinstance(mr, dict):
            return None
        raw = str(mr.get("state", "")).lower()
        draft = bool(mr.get("draft") or mr.get("work_in_progress"))
        state: PRState
        if raw == "merged":
            state = "merged"
        elif raw in ("closed", "locked"):
            state = "closed"
        elif raw == "opened":
            state = "draft" if draft else "open"
        else:
            return None
        number = mr.get("iid")
        return PRStatus(
            state=state,
            number=number if isinstance(number, int) else None,
            url=mr.get("web_url") or None,
        )


# Probed in order: the first CLI authenticated for the origin host wins. Ordering
# by auth (not by hostname string) is what lets self-hosted GitHub Enterprise /
# GitLab resolve correctly without hardcoding host patterns.
_PROVIDERS: tuple[type[Forge], ...] = (GhForge, GlabForge)


def detect(repo_path: str) -> Forge | None:
    """The forge for this repo's ``origin``, or None when we can't attribute one
    (no origin, local-path remote, Bitbucket/raw git, or no authed CLI). The
    caller treats None as 'Tier-1 only'."""
    host = git.origin_host(repo_path)
    if not host:
        return None
    for provider in _PROVIDERS:
        forge = provider(host)
        if forge.authed():
            return forge
    return None

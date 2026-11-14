"""Start, find, and steer worker sessions for factory issues.

Building requests is pure (and tested); talking to the API goes through the
small `SessionsApi` protocol so the Anthropic SDK stays at the edge.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from factory_cli.errors import FactoryError, UsageError
from factory_cli.github import Issue
from factory_cli.lockfile import LockedResource
from factory_cli.manifest import Manifest

METADATA_VALUE_MAX = 512
BRANCH_SLUG_MAX = 40
ACTIVE_STATUSES = ("running", "idle", "rescheduling")


@dataclass(frozen=True)
class Locked:
    engineer: LockedResource
    environment: LockedResource
    worker_vault: LockedResource


@dataclass(frozen=True)
class SessionInfo:
    id: str
    status: str
    created_at: str
    title: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class SessionPlan:
    create: dict[str, Any]  # body for sessions.create, minus the GitHub token
    issue: Issue
    branch: str
    base_ref: str  # what the worker checks out, branches from, and targets with its PR


class SessionsApi(Protocol):
    def create_session(self, body: dict[str, Any]) -> str:
        """Create a session and return its ID."""
        ...

    def send_message(self, session_id: str, text: str) -> None: ...

    def list_sessions(self, agent_id: str, statuses: tuple[str, ...] = ()) -> list[SessionInfo]:
        """Sessions for an agent, newest first. Empty `statuses` means all."""
        ...

    def retrieve(self, session_id: str) -> dict[str, Any]: ...

    def events(self, session_id: str) -> Iterator[dict[str, Any]]: ...

    def threads(self, session_id: str) -> list[dict[str, Any]]: ...

    def thread_events(self, session_id: str, thread_id: str) -> Iterator[dict[str, Any]]: ...


def _meta(value: str) -> str:
    return value[:METADATA_VALUE_MAX]


def branch_name(manifest: Manifest, issue: Issue) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", issue.title.lower()).strip("-")
    if len(slug) > BRANCH_SLUG_MAX:  # cut at a word boundary, not mid-word
        slug = slug[: BRANCH_SLUG_MAX + 1].rsplit("-", 1)[0]
    slug = slug or "task"
    return f"{manifest.task.branch_prefix}{issue.number}-{slug}"


def plan_session(
    manifest: Manifest,
    locked: Locked,
    issue: Issue,
    factory_sha: str,
    requested_by: str,
    base_ref: str | None = None,
) -> SessionPlan:
    ref = base_ref or manifest.base_branch
    if not issue.body.strip():
        raise UsageError(f"issue #{issue.number} has an empty body; the body is the brief")
    branch = branch_name(manifest, issue)
    agent: dict[str, Any] = {"type": "agent", "id": locked.engineer.id}
    if locked.engineer.version is not None:
        agent["version"] = locked.engineer.version
    create: dict[str, Any] = {
        "agent": agent,
        "environment_id": locked.environment.id,
        "vault_ids": [locked.worker_vault.id],
        "title": f"#{issue.number} {issue.title}"[:200],
        "metadata": {
            "factory_name": _meta(manifest.name),
            "factory_version": _meta(manifest.version),
            "factory_sha": _meta(factory_sha),
            "agent_version": str(locked.engineer.version or "latest"),
            "role": "worker",
            "issue": str(issue.number),
            "branch": _meta(branch),
            "base_ref": _meta(ref),
            "requested_by": _meta(requested_by),
        },
        "resources": [
            {
                "type": "github_repository",
                "url": manifest.repository_url,
                "checkout": {"type": "branch", "name": ref},
            }
        ],
        "budget": {
            "type": "limit",
            "max_list_cost": {"amount": str(manifest.task.budget_usd * 100), "currency": "USD"},
        },
    }
    return SessionPlan(create=create, issue=issue, branch=branch, base_ref=ref)


def first_message(plan: SessionPlan, manifest: Manifest, factory_sha: str, session_id: str) -> str:
    """The issue body verbatim, plus the few run facts the worker can't know."""
    footer = "\n".join(
        [
            "---",
            f"Factory task: issue #{plan.issue.number} ({plan.issue.url})",
            f"Branch: {plan.branch} (from origin/{plan.base_ref})",
            f"PR base: {plan.base_ref}",
            f"Session {session_id} · factory v{manifest.version} ({factory_sha[:12]})",
        ]
    )
    return f"{plan.issue.body.strip()}\n\n{footer}\n"


def with_github_token(create: dict[str, Any], token: str) -> dict[str, Any]:
    resources = [{**r, "authorization_token": token} for r in create["resources"]]
    return {**create, "resources": resources}


def start(
    api: SessionsApi, plan: SessionPlan, manifest: Manifest, factory_sha: str, github_token: str
) -> str:
    session_id = api.create_session(with_github_token(plan.create, github_token))
    api.send_message(session_id, first_message(plan, manifest, factory_sha, session_id))
    return session_id


def sessions_for_issue(
    api: SessionsApi, manifest: Manifest, engineer: LockedResource, issue: int, active_only: bool
) -> list[SessionInfo]:
    statuses = ACTIVE_STATUSES if active_only else ()
    return [
        s
        for s in api.list_sessions(engineer.id, statuses)
        if s.metadata.get("factory_name") == manifest.name and s.metadata.get("issue") == str(issue)
    ]


def steering_message(author: str, text: str) -> str:
    if not text.strip():
        raise UsageError("the steering message is empty")
    return f"Message from {author} (developer) while you work:\n\n{text.strip()}"


def steer(
    api: SessionsApi,
    manifest: Manifest,
    engineer: LockedResource,
    issue: int,
    author: str,
    text: str,
) -> str:
    message = steering_message(author, text)
    active = sessions_for_issue(api, manifest, engineer, issue, active_only=True)
    if not active:
        raise FactoryError(f"no active factory session for issue #{issue}")
    session_id = active[0].id  # newest first
    api.send_message(session_id, message)
    return session_id

"""Start a worker session for one task.

Building the request is pure (and tested); sending it goes through the small
`SessionsApi` protocol so the Anthropic SDK stays at the edge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from factory_cli.errors import UsageError
from factory_cli.lockfile import LockedResource
from factory_cli.manifest import Manifest
from factory_cli.prompt import TaskContext, render

SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MAX_SLUG = 48
METADATA_VALUE_MAX = 512


@dataclass(frozen=True)
class Task:
    slug: str
    brief: str
    requested_by: str
    source: str

    def __post_init__(self) -> None:
        if not SLUG_RE.match(self.slug) or len(self.slug) > MAX_SLUG:
            raise UsageError(
                f"slug {self.slug!r} must be kebab-case [a-z0-9-], at most {MAX_SLUG} chars"
            )
        if not self.brief.strip():
            raise UsageError("the brief is empty")


@dataclass(frozen=True)
class Locked:
    engineer: LockedResource
    environment: LockedResource
    vault: LockedResource


@dataclass(frozen=True)
class SessionPlan:
    create: dict[str, Any]  # body for sessions.create, minus the GitHub token
    task_prompt_template: str
    context_without_session: TaskContext  # session_id filled in after create


def _meta(value: str) -> str:
    return value[:METADATA_VALUE_MAX]


def plan_session(
    manifest: Manifest, locked: Locked, task: Task, factory_sha: str, prompt_template: str
) -> SessionPlan:
    branch = f"{manifest.session.branch_prefix}{task.slug}"
    agent: dict[str, Any] = {"type": "agent", "id": locked.engineer.id}
    if locked.engineer.version is not None:
        agent["version"] = locked.engineer.version
    create: dict[str, Any] = {
        "agent": agent,
        "environment_id": locked.environment.id,
        "vault_ids": [locked.vault.id],
        "title": f"{task.slug} · {manifest.name} v{manifest.version}",
        "metadata": {
            "factory_name": _meta(manifest.name),
            "factory_version": _meta(manifest.version),
            "factory_sha": _meta(factory_sha),
            "agent_version": str(locked.engineer.version or "latest"),
            "slug": task.slug,
            "branch": _meta(branch),
            "requested_by": _meta(task.requested_by),
            "source": _meta(task.source),
        },
        "resources": [
            {
                "type": "github_repository",
                "url": manifest.repository_url,
                "checkout": {"type": "branch", "name": manifest.base_branch},
            }
        ],
        "budget": {
            "type": "limit",
            "max_list_cost": {
                "amount": str(manifest.session.budget_usd * 100),
                "currency": "USD",
            },
        },
    }
    context = TaskContext(
        slug=task.slug,
        branch=branch,
        base_branch=manifest.base_branch,
        pr_label=manifest.session.pr_label,
        factory_version=manifest.version,
        factory_sha=factory_sha[:12],
        session_id="<assigned at creation>",
        requested_by=task.requested_by,
        source=task.source,
        brief=task.brief.strip(),
    )
    # Render once now so a broken template fails before anything is created.
    render(prompt_template, context)
    return SessionPlan(create, prompt_template, context)


def first_message(plan: SessionPlan, session_id: str) -> str:
    context = TaskContext(**{**plan.context_without_session.__dict__, "session_id": session_id})
    return render(plan.task_prompt_template, context)


class SessionsApi(Protocol):
    def create_session(self, body: dict[str, Any]) -> str:
        """Create a session and return its ID."""
        ...

    def send_message(self, session_id: str, text: str) -> None: ...


def with_github_token(create: dict[str, Any], token: str) -> dict[str, Any]:
    resources = [{**r, "authorization_token": token} for r in create["resources"]]
    return {**create, "resources": resources}


def dispatch(api: SessionsApi, plan: SessionPlan, github_token: str) -> str:
    session_id = api.create_session(with_github_token(plan.create, github_token))
    api.send_message(session_id, first_message(plan, session_id))
    return session_id

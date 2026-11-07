"""`factory delegate`: one GitHub issue + one worker session per task."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory_cli import git
from factory_cli.dispatch import (
    Locked,
    SessionsApi,
    first_message,
    plan_session,
    start,
    with_github_token,
)
from factory_cli.errors import FactoryError, UsageError
from factory_cli.github import GitHubApi, Issue
from factory_cli.manifest import Manifest

REDACTED = "<redacted>"
LABEL_COLORS = {"factory": "2f6f4f", "needs_human": "d93f0b"}


@dataclass(frozen=True)
class DelegateRequest:
    title: str | None
    brief: str | None
    issue_number: int | None
    dry_run: bool
    allow_unpushed: bool
    base_ref: str | None = None  # default: the manifest's base branch


def behavior_pathspecs(repo: Path, manifest: Manifest) -> tuple[str, ...]:
    """Files the worker's behavior depends on. They must match origin before dispatch."""
    factory = manifest.root.resolve().relative_to(repo.resolve()).as_posix()
    res = manifest.resources
    # The lockfile is deliberately absent: dispatch reads it locally; the worker never does.
    owned = (
        "factory.yaml",
        res.engineer_agent,
        res.reviewer_agent,
        res.environment,
    )
    return (".claude", "CLAUDE.md", *(f"{factory}/{p}" for p in owned))


def _ref_hint(repo: Path, manifest: Manifest, ref: str) -> str:
    """Point at --ref when the current branch is pushed and carries exactly this code."""
    current = git.current_branch(repo)
    if current is None or current == ref or not git.try_fetch(repo, current):
        return ""
    if git.changed_paths(repo, f"origin/{current}", behavior_pathspecs(repo, manifest)):
        return ""
    return (
        f" Your branch {current} is pushed with this factory code: to run the factory "
        f"from it, pass --ref {current}."
    )


def check_drift(repo: Path, manifest: Manifest, ref: str, allow: bool) -> list[str]:
    base = f"origin/{ref}"
    drift = git.changed_paths(repo, base, behavior_pathspecs(repo, manifest))
    if drift and not allow:
        raise FactoryError(
            f"local factory code differs from {base}, and the worker runs {base}: "
            + ", ".join(drift)
            + "."
            + (_ref_hint(repo, manifest, ref) or " Push it first.")
            + " (--allow-unpushed runs the worker on "
            + base
            + " anyway, without these changes.)"
        )
    return drift


def _ensure_labels(gh: GitHubApi, manifest: Manifest) -> None:
    repo = manifest.repo_slug
    gh.ensure_label(repo, manifest.task.label, LABEL_COLORS["factory"], "Factory task")
    gh.ensure_label(
        repo,
        manifest.task.needs_human_label,
        LABEL_COLORS["needs_human"],
        "Factory run needs a human",
    )


def _resolve_issue(gh: GitHubApi, manifest: Manifest, req: DelegateRequest) -> Issue:
    repo = manifest.repo_slug
    if req.issue_number is not None:
        issue = gh.get_issue(repo, req.issue_number)
        if manifest.task.label not in issue.labels and not req.dry_run:
            gh.add_labels(repo, issue.number, [manifest.task.label])
        return issue
    if not req.title or not req.title.strip():
        raise UsageError("a new task needs --title (or pass --issue N for an existing issue)")
    if not req.brief or not req.brief.strip():
        raise UsageError("the brief is empty")
    if req.dry_run:
        return Issue(0, req.title.strip(), req.brief, "<you>", "<new issue>", ())
    return gh.create_issue(repo, req.title.strip(), req.brief, [manifest.task.label])


def started_comment(manifest: Manifest, session_id: str, factory_sha: str, branch: str) -> str:
    return (
        f"🏭 Factory run started: session `{session_id}` · factory v{manifest.version} "
        f"(`{factory_sha[:12]}`) · branch `{branch}`.\n\n"
        f'Steer it from Claude Code with `factory steer --issue <n> "..."`.'
    )


def delegate(
    *,
    repo: Path,
    manifest: Manifest,
    locked: Locked,
    gh: GitHubApi,
    api: SessionsApi,
    request: DelegateRequest,
    github_token: str,
) -> dict[str, Any]:
    ref = request.base_ref or manifest.base_branch
    base = f"origin/{ref}"
    git.fetch(repo, ref)
    factory_sha = git.rev_parse(repo, base)
    drift = check_drift(repo, manifest, ref, allow=request.allow_unpushed or request.dry_run)

    requested_by = gh.login()
    if not request.dry_run:
        _ensure_labels(gh, manifest)
    issue = _resolve_issue(gh, manifest, request)
    plan = plan_session(manifest, locked, issue, factory_sha, requested_by, ref)

    if request.dry_run:
        return {
            "dry_run": True,
            "drift": drift,
            "create": with_github_token(plan.create, REDACTED),
            "first_message": first_message(plan, manifest, factory_sha, "<session id>"),
        }

    try:
        session_id = start(api, plan, manifest, factory_sha, github_token)
    except FactoryError as exc:
        gh.comment(manifest.repo_slug, issue.number, f"⚠️ Factory dispatch failed: {exc}")
        raise
    gh.comment(
        manifest.repo_slug,
        issue.number,
        started_comment(manifest, session_id, factory_sha, plan.branch),
    )
    return {
        "issue": issue.number,
        "issue_url": issue.url,
        "session_id": session_id,
        "branch": plan.branch,
        "factory_version": manifest.version,
        "factory_sha": factory_sha,
        "agent_version": plan.create["metadata"]["agent_version"],
    }

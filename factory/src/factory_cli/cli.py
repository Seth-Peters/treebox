"""`factory` command line: run, check, vault, version.

Data goes to stdout as JSON; diagnostics go to stderr. Exit codes:
0 ok, 1 runtime failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from factory_cli import git
from factory_cli.agents import load_agent
from factory_cli.check import GITHUB_MCP, boundary_problems, definition_problems
from factory_cli.dispatch import (
    Locked,
    Task,
    dispatch,
    first_message,
    plan_session,
    with_github_token,
)
from factory_cli.errors import FactoryError, UsageError
from factory_cli.lockfile import LockedResource, load_lockfile
from factory_cli.manifest import Manifest, load_manifest

GITHUB_TOKEN_ENV = "FACTORY_GITHUB_TOKEN"
REDACTED = "<redacted>"
UNLOCKED = LockedResource(kind="unapplied", id="<from claude-lock.json>", version=None)


def _emit(data: dict[str, Any]) -> None:
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _warn(message: str) -> None:
    print(f"factory: {message}", file=sys.stderr)


def _token() -> str:
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if not token:
        raise UsageError(
            f"set {GITHUB_TOKEN_ENV} to a fine-grained GitHub token for the repository "
            "(contents + pull requests: read/write)"
        )
    return token


def _locate(args: argparse.Namespace) -> tuple[Path, Manifest]:
    repo = git.repo_root(Path.cwd())
    factory_dir = Path(args.factory_dir) if args.factory_dir else repo / "factory"
    return repo, load_manifest(factory_dir)


def _locked(manifest: Manifest, required: bool) -> Locked:
    try:
        lock = load_lockfile(manifest.root)
        res = manifest.resources
        return Locked(
            engineer=lock.get(res.engineer_agent, "agent"),
            environment=lock.get(res.environment, "environment"),
            vault=lock.get(res.vault, "vault"),
        )
    except FactoryError:
        if required:
            raise
        _warn("claude-lock.json incomplete; showing placeholder IDs (run `ant apply .`)")
        return Locked(engineer=UNLOCKED, environment=UNLOCKED, vault=UNLOCKED)


def _behavior_pathspecs(repo: Path, manifest: Manifest) -> tuple[str, ...]:
    """Files the worker's behavior depends on. They must match origin before dispatch."""
    factory = manifest.root.resolve().relative_to(repo.resolve()).as_posix()
    res = manifest.resources
    owned = (
        "factory.yaml",
        "claude-lock.json",
        res.engineer_agent,
        res.reviewer_agent,
        res.environment,
        manifest.task_prompt,
    )
    return (".claude", "CLAUDE.md", *(f"{factory}/{p}" for p in owned))


def _read_brief(args: argparse.Namespace) -> str:
    if args.brief is not None:
        brief: str = args.brief
        return brief
    if args.brief_file is not None:
        try:
            return Path(args.brief_file).read_text()
        except OSError as exc:
            raise UsageError(f"cannot read brief file: {exc}") from exc
    if sys.stdin.isatty():
        raise UsageError("give the brief with --brief, --brief-file, or on stdin")
    return sys.stdin.read()


def cmd_run(args: argparse.Namespace) -> int:
    repo, manifest = _locate(args)
    task = Task(
        slug=args.slug,
        brief=_read_brief(args),
        requested_by=args.requested_by,
        source=args.source,
    )
    base = f"origin/{manifest.base_branch}"
    git.fetch(repo, manifest.base_branch)
    factory_sha = git.rev_parse(repo, base)

    drift = git.changed_paths(repo, base, _behavior_pathspecs(repo, manifest))
    if drift:
        detail = f"local factory code differs from {base} (the worker runs {base}): " + ", ".join(
            drift
        )
        if not (args.allow_unpushed or args.dry_run):
            raise FactoryError(f"{detail}. Push it, or pass --allow-unpushed.")
        _warn(detail)

    template = manifest.path(manifest.task_prompt).read_text()
    plan = plan_session(
        manifest, _locked(manifest, required=not args.dry_run), task, factory_sha, template
    )

    if args.dry_run:
        _emit(
            {
                "dry_run": True,
                "create": with_github_token(plan.create, REDACTED),
                "first_message": first_message(plan, "<session id>"),
            }
        )
        return 0

    from factory_cli.anthropic_api import AnthropicApi

    session_id = dispatch(AnthropicApi(), plan, _token())
    _emit(
        {
            "session_id": session_id,
            "title": plan.create["title"],
            "branch": plan.context_without_session.branch,
            "factory_version": manifest.version,
            "factory_sha": factory_sha,
            "agent_version": plan.create["metadata"]["agent_version"],
        }
    )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    repo, manifest = _locate(args)
    problems = definition_problems(manifest)
    changed: list[str] = []
    if args.boundary:
        git.fetch(repo, manifest.base_branch)
        changed = git.changed_paths(repo, args.base or f"origin/{manifest.base_branch}")
        problems += boundary_problems(manifest, changed)
    _emit(
        {
            "ok": not problems,
            "factory_version": manifest.version,
            "problems": problems,
            **({"changed": changed} if args.boundary else {}),
        }
    )
    return 0 if not problems else 1


def cmd_vault_set_github_token(args: argparse.Namespace) -> int:
    _, manifest = _locate(args)
    vault = load_lockfile(manifest.root).get(manifest.resources.vault, "vault")
    engineer = load_agent(manifest.path(manifest.resources.engineer_agent))
    url = engineer.mcp_server_url(GITHUB_MCP)

    from factory_cli.anthropic_api import AnthropicApi

    credential_id = AnthropicApi().set_mcp_bearer(vault.id, url, _token(), "GitHub token")
    _emit({"vault_id": vault.id, "credential_id": credential_id, "mcp_server_url": url})
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    _, manifest = _locate(args)
    _emit({"name": manifest.name, "version": manifest.version})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description=__doc__)
    parser.add_argument("--factory-dir", help="directory holding factory.yaml (default: ./factory)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="dispatch a task to a new worker session")
    run.add_argument("--slug", required=True, help="kebab-case name; becomes the branch")
    run.add_argument("--requested-by", required=True, help="who asked for this")
    run.add_argument("--source", required=True, help="where it came from: dev, slack, po, ...")
    brief = run.add_mutually_exclusive_group()
    brief.add_argument("--brief", help="the brief as text")
    brief.add_argument("--brief-file", help="path to the brief (default: stdin)")
    run.add_argument("--dry-run", action="store_true", help="print the request; create nothing")
    run.add_argument(
        "--allow-unpushed",
        action="store_true",
        help="dispatch even though local factory code differs from origin",
    )
    run.set_defaults(handler=cmd_run)

    check = sub.add_parser("check", help="validate the factory definition")
    check.add_argument(
        "--boundary",
        action="store_true",
        help="also fail if changes vs. the base touch anything but editable factory code",
    )
    check.add_argument("--base", help="ref to diff against (default: origin/<base_branch>)")
    check.set_defaults(handler=cmd_check)

    vault = sub.add_parser("vault", help="manage the factory vault")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    set_token = vault_sub.add_parser(
        "set-github-token", help=f"store ${GITHUB_TOKEN_ENV} for the GitHub MCP server"
    )
    set_token.set_defaults(handler=cmd_vault_set_github_token)

    version = sub.add_parser("version", help="print the factory name and version")
    version.set_defaults(handler=cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.handler(args)
    except FactoryError as exc:
        _warn(str(exc))
        return exc.exit_code
    return code


if __name__ == "__main__":
    raise SystemExit(main())

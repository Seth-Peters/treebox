"""`factory` command line.

delegate    file (or reuse) a GitHub issue for a task and start a worker session
steer       send a message to the running session for an issue
status      list the sessions for an issue; --wait blocks until the newest settles
transcript  export full session transcripts (every tool call, subagent threads)
check       validate the factory definition (and, with --boundary, a change's scope)
vault       store the GitHub token the worker's GitHub MCP server uses
version     print the factory name and version

Data goes to stdout as JSON; diagnostics go to stderr.
Exit codes: 0 ok, 1 runtime failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from factory_cli import git
from factory_cli.agents import load_agent
from factory_cli.check import GITHUB_MCP, boundary_problems, definition_problems
from factory_cli.delegate import DelegateRequest, delegate
from factory_cli.dispatch import Locked, SessionsApi, sessions_for_issue, steer
from factory_cli.errors import FactoryError, UsageError
from factory_cli.github import GhCli, GitHubApi
from factory_cli.lockfile import LockedResource, load_lockfile
from factory_cli.manifest import Manifest, load_manifest
from factory_cli.transcript import export

GITHUB_TOKEN_ENV = "FACTORY_GITHUB_TOKEN"
UNLOCKED = LockedResource(kind="unapplied", id="<from claude-lock.json>", version=None)
SETTLED = ("idle", "terminated")


def _emit(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _warn(message: str) -> None:
    print(f"factory: {message}", file=sys.stderr)


def _token(required: bool) -> str:
    token = os.environ.get(GITHUB_TOKEN_ENV, "").strip()
    if not token and required:
        raise UsageError(
            f"set {GITHUB_TOKEN_ENV} to a fine-grained GitHub token for the repository "
            "(contents + pull requests: read/write)"
        )
    return token


def _locate(args: argparse.Namespace) -> tuple[Path, Manifest]:
    repo = git.repo_root(Path.cwd())
    factory_dir = Path(args.factory_dir) if args.factory_dir else repo / "factory"
    return repo, load_manifest(factory_dir)


def _locked(manifest: Manifest, required: bool = True) -> Locked:
    try:
        lock = load_lockfile(manifest.root)
        res = manifest.resources
        return Locked(
            engineer=lock.get(res.engineer_agent, "agent"),
            environment=lock.get(res.environment, "environment"),
            worker_vault=lock.get(res.worker_vault, "vault"),
        )
    except FactoryError:
        if required:
            raise
        _warn("claude-lock.json incomplete; showing placeholder IDs (run `ant apply .`)")
        return Locked(engineer=UNLOCKED, environment=UNLOCKED, worker_vault=UNLOCKED)


def _read_text(inline: str | None, path: str | None, what: str) -> str | None:
    if inline is not None:
        return inline
    if path is not None:
        try:
            return Path(path).read_text()
        except OSError as exc:
            raise UsageError(f"cannot read {what}: {exc}") from exc
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return None


# Seams for tests: swap these to fakes.
def make_github() -> GitHubApi:
    return GhCli()


def make_sessions() -> SessionsApi:
    from factory_cli.anthropic_api import AnthropicApi

    return AnthropicApi()


def cmd_delegate(args: argparse.Namespace) -> int:
    repo, manifest = _locate(args)
    brief = None if args.issue else _read_text(args.brief, args.brief_file, "the brief")
    request = DelegateRequest(
        title=args.title,
        brief=brief,
        issue_number=args.issue,
        dry_run=args.dry_run,
        allow_unpushed=args.allow_unpushed,
        base_ref=args.ref,
    )
    result = delegate(
        repo=repo,
        manifest=manifest,
        locked=_locked(manifest, required=not args.dry_run),
        gh=make_github(),
        api=make_sessions(),
        request=request,
        github_token=_token(required=not args.dry_run),
    )
    _emit(result)
    return 0


def cmd_steer(args: argparse.Namespace) -> int:
    _, manifest = _locate(args)
    text = _read_text(args.text, None, "the message") or ""
    gh = make_github()
    author = gh.login()
    session_id = steer(
        make_sessions(), manifest, _locked(manifest).engineer, args.issue, author, text
    )
    gh.comment(
        manifest.repo_slug,
        args.issue,
        f"🧭 Steering sent to session `{session_id}` by @{author}:\n\n> "
        + text.strip().replace("\n", "\n> "),
    )
    _emit({"issue": args.issue, "session_id": session_id, "sent": True})
    return 0


def _issue_sessions(manifest: Manifest, issue: int) -> list[dict[str, Any]]:
    found = sessions_for_issue(
        make_sessions(), manifest, _locked(manifest).engineer, issue, active_only=False
    )
    return [s.__dict__ for s in found]


def cmd_status(args: argparse.Namespace) -> int:
    _, manifest = _locate(args)
    sessions = _issue_sessions(manifest, args.issue)
    deadline = time.monotonic() + args.timeout
    while args.wait and (not sessions or sessions[0]["status"] not in SETTLED):
        if time.monotonic() > deadline:
            _emit({"issue": args.issue, "timed_out": True, "sessions": sessions})
            return 1
        newest = sessions[0]["status"] if sessions else "not found"
        _warn(f"issue #{args.issue}: newest session is {newest}; waiting…")
        time.sleep(args.interval)
        sessions = _issue_sessions(manifest, args.issue)
    _emit({"issue": args.issue, "sessions": sessions})
    return 0


def cmd_transcript(args: argparse.Namespace) -> int:
    _, manifest = _locate(args)
    if args.session:
        session_ids = [args.session]
    elif args.issue is not None:
        session_ids = [s["id"] for s in _issue_sessions(manifest, args.issue)]
    else:
        raise UsageError("pass --issue N or --session ID")
    if not session_ids:
        raise FactoryError(f"no factory sessions for issue #{args.issue}")
    api = make_sessions()
    out = Path(args.out)
    summaries = [export(api, sid, out).as_dict() for sid in session_ids]
    _emit({"out": str(out), "sessions": summaries})
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
    vault = load_lockfile(manifest.root).get(manifest.resources.worker_vault, "vault")
    engineer = load_agent(manifest.path(manifest.resources.engineer_agent))
    url = engineer.mcp_server_url(GITHUB_MCP)

    from factory_cli.anthropic_api import AnthropicApi

    credential_id = AnthropicApi().set_mcp_bearer(vault.id, url, _token(True), "GitHub token")
    _emit({"vault_id": vault.id, "credential_id": credential_id, "mcp_server_url": url})
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    _, manifest = _locate(args)
    _emit({"name": manifest.name, "version": manifest.version})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="factory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--factory-dir", help="directory holding factory.yaml (default: ./factory)")
    sub = parser.add_subparsers(dest="command", required=True)

    dlg = sub.add_parser("delegate", help="file a task issue and start a worker session")
    dlg.add_argument("--title", help="issue title for a new task")
    source = dlg.add_mutually_exclusive_group()
    source.add_argument("--brief", help="the brief as text (becomes the issue body)")
    source.add_argument("--brief-file", help="path to the brief (default: stdin)")
    source.add_argument("--issue", type=int, help="delegate an existing issue instead")
    dlg.add_argument(
        "--ref",
        help="branch the worker checks out and targets (default: the base branch); "
        "use it to try unmerged factory changes",
    )
    dlg.add_argument("--dry-run", action="store_true", help="print the request; create nothing")
    dlg.add_argument(
        "--allow-unpushed",
        action="store_true",
        help="dispatch even though local factory code differs from origin",
    )
    dlg.set_defaults(handler=cmd_delegate)

    st = sub.add_parser("steer", help="message the running session for an issue")
    st.add_argument("--issue", type=int, required=True)
    st.add_argument("text", nargs="?", help="the message (default: stdin)")
    st.set_defaults(handler=cmd_steer)

    status = sub.add_parser("status", help="sessions for an issue")
    status.add_argument("--issue", type=int, required=True)
    status.add_argument("--wait", action="store_true", help="block until the newest settles")
    status.add_argument("--timeout", type=int, default=3600, help="seconds to wait (default 3600)")
    status.add_argument("--interval", type=int, default=20, help="poll seconds (default 20)")
    status.set_defaults(handler=cmd_status)

    tr = sub.add_parser("transcript", help="export full transcripts")
    target = tr.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue", type=int, help="every session for this issue")
    target.add_argument("--session", help="one session ID")
    tr.add_argument("--out", required=True, help="directory to write into")
    tr.set_defaults(handler=cmd_transcript)

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

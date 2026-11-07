"""Tests for the factory CLI. No network: GitHub and the Anthropic API are fakes."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from factory_cli import cli
from factory_cli.boundary import matches, violations
from factory_cli.check import boundary_problems, definition_problems
from factory_cli.dispatch import (
    Locked,
    SessionInfo,
    branch_name,
    first_message,
    plan_session,
    sessions_for_issue,
    start,
    steer,
)
from factory_cli.errors import FactoryError, UsageError
from factory_cli.github import Issue
from factory_cli.lockfile import LockedResource, parse_lockfile
from factory_cli.manifest import FactoryCode, Manifest, load_manifest
from factory_cli.transcript import export

FACTORY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_DIR.parent

LOCKED = Locked(
    engineer=LockedResource("agent", "agent_eng", 3),
    environment=LockedResource("environment", "env_1", None),
    worker_vault=LockedResource("vault", "vlt_1", None),
)
ISSUE = Issue(
    61,
    "Teardown exits 3 on a missing worktree!",
    "Make `treebox teardown ghost` exit 3.\n\nCost is $5.",
    "seth",
    "https://github.com/Seth-Peters/treebox/issues/61",
    ("factory",),
)


@pytest.fixture
def manifest() -> Manifest:
    return load_manifest(FACTORY_DIR)


# --- fakes -------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, existing: list[Issue] | None = None) -> None:
        self.issues = {i.number: i for i in existing or []}
        self.labels_created: list[str] = []
        self.labels_added: list[tuple[int, list[str]]] = []
        self.comments: list[tuple[int, str]] = []

    def login(self) -> str:
        return "seth"

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        self.labels_created.append(name)

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> Issue:
        number = 100 + len(self.issues)
        issue = Issue(number, title, body, "seth", f"https://x/issues/{number}", tuple(labels))
        self.issues[number] = issue
        return issue

    def get_issue(self, repo: str, number: int) -> Issue:
        return self.issues[number]

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self.labels_added.append((number, labels))

    def comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((number, body))


class FakeApi:
    def __init__(self, sessions: list[SessionInfo] | None = None) -> None:
        self.created: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str]] = []
        self.sessions = sessions or []
        self.fail_create = False

    def create_session(self, body: dict[str, Any]) -> str:
        if self.fail_create:
            raise FactoryError("creating the session failed: boom")
        self.created.append(body)
        return "sesn_123"

    def send_message(self, session_id: str, text: str) -> None:
        self.sent.append((session_id, text))

    def list_sessions(self, agent_id: str, statuses: tuple[str, ...] = ()) -> list[SessionInfo]:
        return [s for s in self.sessions if not statuses or s.status in statuses]

    def retrieve(self, session_id: str) -> dict[str, Any]:
        return {"id": session_id, "status": "idle"}

    def events(self, session_id: str) -> Iterator[dict[str, Any]]:
        yield {"type": "user.message"}
        yield {"type": "agent.tool_use", "name": "bash"}
        yield {
            "type": "agent.mcp_tool_use",
            "name": "create_pull_request",
            "mcp_server_name": "github",
        }

    def threads(self, session_id: str) -> list[dict[str, Any]]:
        return [{"id": "sthr_rev", "parent_thread_id": "sthr_main"}]

    def thread_events(self, session_id: str, thread_id: str) -> Iterator[dict[str, Any]]:
        yield {"type": "agent.tool_use", "name": "read"}


def _session(
    sid: str, status: str, issue: str = "61", name: str = "treebox-factory"
) -> SessionInfo:
    return SessionInfo(
        sid, status, "2026-11-07T00:00:00", "t", {"factory_name": name, "issue": issue}
    )


# --- the real factory definition ---------------------------------------------


def test_real_factory_definition_is_coherent(manifest: Manifest) -> None:
    assert definition_problems(manifest) == []


def test_repo_slug(manifest: Manifest) -> None:
    assert manifest.repo_slug == "Seth-Peters/treebox"


# --- lockfile ----------------------------------------------------------------


def test_lockfile_resolves_paths_with_or_without_dot_slash() -> None:
    lock = parse_lockfile(
        {"resources": {"./agents/e.md": {"kind": "agent", "id": "agent_1", "version": "2"}}}
    )
    assert lock.get("agents/e.md", "agent") == LockedResource("agent", "agent_1", 2)


def test_lockfile_errors_on_missing_or_wrong_kind() -> None:
    lock = parse_lockfile({"resources": {"./v.yaml": {"kind": "vault", "id": "vlt_1"}}})
    with pytest.raises(FactoryError, match="ant apply"):
        lock.get("agents/e.md", "agent")
    with pytest.raises(FactoryError, match="expected agent"):
        lock.get("v.yaml", "agent")


# --- boundary ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("factory/agents/engineer.md", "factory/**", True),
        ("src/treebox/cli.py", "factory/**", False),
        ("factory/x.md", "factory/*.md", True),
        ("factory/a/x.md", "factory/*.md", False),
    ],
)
def test_glob_matching(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


def test_boundary_blocks_product_code_and_history() -> None:
    rules = FactoryCode(editable=("factory/**", ".claude/**"), never=("factory/retros/**",))
    bad = {
        v.path
        for v in violations(
            ["factory/agents/e.md", "factory/retros/old.md", "src/treebox/cli.py"], rules
        )
    }
    assert bad == {"factory/retros/old.md", "src/treebox/cli.py"}


def test_real_boundary(manifest: Manifest) -> None:
    assert boundary_problems(manifest, ["factory/agents/reviewer.md"]) == []
    assert boundary_problems(manifest, ["factory/claude-lock.json"]) != []
    assert boundary_problems(manifest, ["tests/test_units.py"]) != []


# --- session plan and messages -----------------------------------------------


def test_branch_name_is_issue_numbered_and_safe(manifest: Manifest) -> None:
    assert branch_name(manifest, ISSUE) == "factory/61-teardown-exits-3-on-a-missing-worktree"


def test_branch_name_truncates_at_word_boundary(manifest: Manifest) -> None:
    long = Issue(
        38, "SLUG_CONFLICT recovery hint dead-ends for never-registered dirs", "b", "a", "u", ()
    )
    name = branch_name(manifest, long)
    assert name == "factory/38-slug-conflict-recovery-hint-dead-ends"
    assert len(name.split("/", 1)[1]) <= len("38-") + 40


def test_plan_pins_agent_and_stamps_issue(manifest: Manifest) -> None:
    body = plan_session(manifest, LOCKED, ISSUE, "deadbeef" * 5, "seth").create
    assert body["agent"] == {"type": "agent", "id": "agent_eng", "version": 3}
    assert body["vault_ids"] == ["vlt_1"]
    assert body["metadata"]["issue"] == "61"
    assert body["metadata"]["role"] == "worker"
    assert body["metadata"]["factory_version"] == manifest.version
    assert body["budget"]["max_list_cost"]["amount"] == str(manifest.task.budget_usd * 100)
    assert "authorization_token" not in body["resources"][0]


def test_plan_rejects_empty_issue_body(manifest: Manifest) -> None:
    empty = Issue(7, "t", "  ", "a", "u", ())
    with pytest.raises(UsageError, match="empty body"):
        plan_session(manifest, LOCKED, empty, "abc", "seth")


def test_first_message_is_brief_verbatim_plus_footer(manifest: Manifest) -> None:
    plan = plan_session(manifest, LOCKED, ISSUE, "abc123def456789", "seth")
    text = first_message(plan, manifest, "abc123def456789", "sesn_9")
    assert text.startswith(ISSUE.body)
    assert "Cost is $5." in text
    assert "issue #61" in text and "sesn_9" in text and "abc123def456" in text


def test_start_creates_then_sends_with_session_id(manifest: Manifest) -> None:
    api = FakeApi()
    plan = plan_session(manifest, LOCKED, ISSUE, "abc", "seth")
    assert start(api, plan, manifest, "abc", "ghp_secret") == "sesn_123"
    assert api.created[0]["resources"][0]["authorization_token"] == "ghp_secret"
    assert "sesn_123" in api.sent[0][1]
    assert "ghp_secret" not in api.sent[0][1]


# --- finding and steering runs -----------------------------------------------


def test_sessions_for_issue_filters_by_factory_and_issue(manifest: Manifest) -> None:
    api = FakeApi(
        [
            _session("s_new", "running"),
            _session("s_other_issue", "running", issue="62"),
            _session("s_other_factory", "running", name="someone-else"),
            _session("s_done", "terminated"),
        ]
    )
    active = sessions_for_issue(api, manifest, LOCKED.engineer, 61, active_only=True)
    assert [s.id for s in active] == ["s_new"]
    every = sessions_for_issue(api, manifest, LOCKED.engineer, 61, active_only=False)
    assert [s.id for s in every] == ["s_new", "s_done"]


def test_steer_sends_to_newest_active_session(manifest: Manifest) -> None:
    api = FakeApi([_session("s_new", "idle"), _session("s_old", "running")])
    assert steer(api, manifest, LOCKED.engineer, 61, "seth", "also cover --json") == "s_new"
    assert api.sent == [("s_new", api.sent[0][1])]
    assert "seth" in api.sent[0][1] and "also cover --json" in api.sent[0][1]


def test_steer_errors_without_active_session(manifest: Manifest) -> None:
    with pytest.raises(FactoryError, match="no active"):
        steer(FakeApi([_session("s", "terminated")]), manifest, LOCKED.engineer, 61, "a", "x")


# --- transcript --------------------------------------------------------------


def test_export_writes_events_threads_and_summary(tmp_path: Path) -> None:
    summary = export(FakeApi(), "sesn_1", tmp_path)
    root = tmp_path / "sesn_1"
    assert len((root / "events.jsonl").read_text().splitlines()) == 3
    assert (root / "threads" / "sthr_rev.jsonl").is_file()
    assert summary.tool_calls == 3
    assert summary.tools["github:create_pull_request"] == 1
    assert json.loads((root / "summary.json").read_text())["threads"] == {"sthr_rev": 1}


# --- CLI end to end (throwaway origin, fake GitHub + API) ---------------------


def _sh(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    _sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    shutil.copytree(
        FACTORY_DIR, work / "factory", ignore=shutil.ignore_patterns(".venv", "*cache*", "tests")
    )
    shutil.copytree(REPO_ROOT / ".claude", work / ".claude", symlinks=True)
    lock = {
        "resources": {
            "./agents/engineer.md": {"kind": "agent", "id": "agent_eng", "version": 3},
            "./agents/reviewer.md": {"kind": "agent", "id": "agent_rev", "version": 1},
            "./environments/treebox.yaml": {"kind": "environment", "id": "env_1"},
            "./vaults/github-worker.yaml": {"kind": "vault", "id": "vlt_1"},
        }
    }
    (work / "factory" / "claude-lock.json").write_text(json.dumps(lock))
    ident = ("-c", "user.email=t@e", "-c", "user.name=t")
    _sh(work, "git", *ident, "add", "-A")
    _sh(work, "git", *ident, "commit", "-qm", "init")
    _sh(work, "git", "push", "-q", "origin", "main")
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeGitHub, FakeApi]:
    gh, api = FakeGitHub([ISSUE]), FakeApi([_session("s_live", "running")])
    monkeypatch.setattr(cli, "make_github", lambda: gh)
    monkeypatch.setattr(cli, "make_sessions", lambda: api)
    monkeypatch.setenv(cli.GITHUB_TOKEN_ENV, "ghp_secret")
    return gh, api


def test_cli_delegate_new_task_files_issue_starts_session_and_comments(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    gh, api = fakes
    code = cli.main(["delegate", "--title", "Add --quiet", "--brief", "Add a --quiet flag."])
    out = capsys.readouterr()
    assert code == 0, out.err
    data = json.loads(out.out)
    new = gh.issues[data["issue"]]
    assert new.labels == ("factory",) and new.body == "Add a --quiet flag."
    assert set(gh.labels_created) == {"factory", "factory:needs-human"}
    assert data["session_id"] == "sesn_123"
    assert api.created[0]["metadata"]["issue"] == str(new.number)
    assert api.sent[0][1].startswith("Add a --quiet flag.")
    assert "sesn_123" in gh.comments[-1][1]
    assert "ghp_secret" not in out.out + out.err


def test_cli_delegate_existing_issue_adds_label_when_missing(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    gh, api = fakes
    gh.issues[61] = Issue(61, ISSUE.title, ISSUE.body, "pm", ISSUE.url, ())
    assert cli.main(["delegate", "--issue", "61"]) == 0
    assert gh.labels_added == [(61, ["factory"])]
    assert api.created[0]["metadata"]["requested_by"] == "seth"


def test_cli_delegate_dry_run_creates_nothing(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    gh, api = fakes
    assert cli.main(["delegate", "--title", "T", "--brief", "B", "--dry-run"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is True
    assert data["create"]["resources"][0]["authorization_token"] == "<redacted>"
    assert gh.comments == [] and api.created == [] and len(gh.issues) == 1


def test_cli_delegate_comments_on_dispatch_failure(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    gh, api = fakes
    api.fail_create = True
    assert cli.main(["delegate", "--issue", "61"]) == 1
    assert "dispatch failed" in gh.comments[-1][1]


def test_cli_delegate_refuses_unpushed_factory_changes(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".claude" / "skills" / "review" / "SKILL.md").write_text("changed\n")
    assert cli.main(["delegate", "--issue", "61"]) == 1
    assert "differs from origin/main" in capsys.readouterr().err


def test_cli_delegate_suggests_ref_when_branch_carries_the_factory_code(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    _gh, api = fakes
    (repo / ".claude" / "skills" / "review" / "SKILL.md").write_text("changed\n")
    ident = ("-c", "user.email=t@e", "-c", "user.name=t")
    _sh(repo, "git", "switch", "-qc", "feat/factory-next")
    _sh(repo, "git", *ident, "commit", "-qam", "factory change")
    _sh(repo, "git", "push", "-q", "origin", "feat/factory-next")
    assert cli.main(["delegate", "--issue", "61"]) == 1
    err = capsys.readouterr().err
    assert "pass --ref feat/factory-next" in err and api.created == []
    assert cli.main(["delegate", "--issue", "61", "--ref", "feat/factory-next"]) == 0
    assert api.created[0]["resources"][0]["checkout"]["name"] == "feat/factory-next"


def test_cli_steer_sends_and_records_on_issue(
    repo: Path, fakes: tuple[FakeGitHub, FakeApi], capsys: pytest.CaptureFixture[str]
) -> None:
    gh, api = fakes
    assert cli.main(["steer", "--issue", "61", "also handle --json"]) == 0
    assert api.sent[0][0] == "s_live"
    assert "also handle --json" in gh.comments[-1][1]


def test_cli_check_boundary(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--boundary"]) == 0
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    capsys.readouterr()
    assert cli.main(["check", "--boundary"]) == 1
    assert any("src/app.py" in p for p in json.loads(capsys.readouterr().out)["problems"])

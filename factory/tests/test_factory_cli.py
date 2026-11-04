"""Tests for the factory CLI. No network: the Anthropic API is a fake."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from factory_cli import cli
from factory_cli.boundary import matches, violations
from factory_cli.check import boundary_problems, definition_problems
from factory_cli.dispatch import Locked, Task, dispatch, plan_session
from factory_cli.errors import FactoryError, UsageError
from factory_cli.lockfile import LockedResource, parse_lockfile
from factory_cli.manifest import FactoryCode, Manifest, load_manifest
from factory_cli.prompt import TaskContext, render, unknown_placeholders

FACTORY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = FACTORY_DIR.parent

LOCKED = Locked(
    engineer=LockedResource("agent", "agent_eng", 3),
    environment=LockedResource("environment", "env_1", None),
    vault=LockedResource("vault", "vlt_1", None),
)


@pytest.fixture
def manifest() -> Manifest:
    return load_manifest(FACTORY_DIR)


def _task(**overrides: str) -> Task:
    fields = {"slug": "fix-exit-code", "brief": "Make teardown exit 3.", "requested_by": "seth"}
    return Task(**{**fields, "source": "dev", **overrides})


def _template(manifest: Manifest) -> str:
    return manifest.path(manifest.task_prompt).read_text()


# --- the real factory definition --------------------------------------------


def test_real_factory_definition_is_coherent(manifest: Manifest) -> None:
    assert definition_problems(manifest) == []


def test_real_prompt_uses_only_known_placeholders(manifest: Manifest) -> None:
    assert unknown_placeholders(_template(manifest)) == set()


# --- manifest and lockfile ---------------------------------------------------


def test_manifest_rejects_missing_section(tmp_path: Path) -> None:
    (tmp_path / "factory.yaml").write_text("name: x\nversion: '1'\n")
    with pytest.raises(FactoryError, match="repository"):
        load_manifest(tmp_path)


def test_lockfile_resolves_paths_with_or_without_dot_slash() -> None:
    lock = parse_lockfile(
        {"resources": {"./agents/e.md": {"kind": "agent", "id": "agent_1", "version": "2"}}}
    )
    assert lock.get("agents/e.md", "agent") == LockedResource("agent", "agent_1", 2)
    assert lock.get("./agents/e.md", "agent").version == 2


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
        (".claude/skills/review/SKILL.md", ".claude/**", True),
        ("src/treebox/cli.py", "factory/**", False),
        ("factory/x.md", "factory/*.md", True),
        ("factory/a/x.md", "factory/*.md", False),
    ],
)
def test_glob_matching(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


def test_boundary_blocks_product_code_and_history() -> None:
    rules = FactoryCode(editable=("factory/**", ".claude/**"), never=("factory/retros/**",))
    found = {
        v.path: v.reason
        for v in violations(
            [
                "factory/agents/engineer.md",
                ".claude/skills/review/SKILL.md",
                "factory/retros/2026-01-01.md",
                "src/treebox/cli.py",
            ],
            rules,
        )
    }
    assert set(found) == {"factory/retros/2026-01-01.md", "src/treebox/cli.py"}
    assert "never-edit" in found["factory/retros/2026-01-01.md"]


def test_real_boundary_allows_factory_only(manifest: Manifest) -> None:
    assert boundary_problems(manifest, ["factory/agents/reviewer.md"]) == []
    assert boundary_problems(manifest, ["factory/claude-lock.json"]) != []
    assert boundary_problems(manifest, ["tests/test_units.py"]) != []


# --- prompt and session plan -------------------------------------------------


def test_render_rejects_unknown_placeholder() -> None:
    context = TaskContext(*(["x"] * 10))
    with pytest.raises(FactoryError, match="nope"):
        render("$slug $nope", context)


def test_render_keeps_dollar_signs_in_the_brief(manifest: Manifest) -> None:
    plan = plan_session(manifest, LOCKED, _task(brief="cost is $5 for $slug"), "abc", "$brief")
    assert plan.context_without_session.brief == "cost is $5 for $slug"


@pytest.mark.parametrize("slug", ["Bad Slug", "trailing-", "x" * 49, ""])
def test_task_rejects_bad_slugs(slug: str) -> None:
    with pytest.raises(UsageError):
        _task(slug=slug)


def test_task_rejects_empty_brief() -> None:
    with pytest.raises(UsageError, match="empty"):
        _task(brief="  \n")


def test_plan_pins_agent_and_stamps_factory(manifest: Manifest) -> None:
    plan = plan_session(manifest, LOCKED, _task(), "deadbeef" * 5, _template(manifest))
    body = plan.create
    assert body["agent"] == {"type": "agent", "id": "agent_eng", "version": 3}
    assert body["environment_id"] == "env_1"
    assert body["vault_ids"] == ["vlt_1"]
    assert body["metadata"]["factory_version"] == manifest.version
    assert body["metadata"]["slug"] == "fix-exit-code"
    assert body["metadata"]["branch"] == "factory/fix-exit-code"
    assert body["budget"]["max_list_cost"] == {
        "amount": str(manifest.session.budget_usd * 100),
        "currency": "USD",
    }
    assert "authorization_token" not in body["resources"][0]


class FakeApi:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str]] = []

    def create_session(self, body: dict[str, Any]) -> str:
        self.created.append(body)
        return "sesn_123"

    def send_message(self, session_id: str, text: str) -> None:
        self.sent.append((session_id, text))


def test_dispatch_creates_then_sends_task_with_session_id(manifest: Manifest) -> None:
    api = FakeApi()
    plan = plan_session(manifest, LOCKED, _task(), "abc123", _template(manifest))
    assert dispatch(api, plan, "ghp_secret") == "sesn_123"
    assert api.created[0]["resources"][0]["authorization_token"] == "ghp_secret"
    session_id, text = api.sent[0]
    assert session_id == "sesn_123"
    assert "sesn_123" in text
    assert "Make teardown exit 3." in text
    assert "ghp_secret" not in text


# --- CLI end to end (dry run against a throwaway origin) ---------------------


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
    _sh(work, "git", "-c", "user.email=t@e", "-c", "user.name=t", "add", "-A")
    _sh(work, "git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "init")
    _sh(work, "git", "push", "-q", "origin", "main")
    monkeypatch.chdir(work)
    return work


def test_cli_dry_run_prints_request_without_secrets(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(cli.GITHUB_TOKEN_ENV, "ghp_secret")
    code = cli.main(
        [
            *("run", "--slug", "add-flag", "--requested-by", "po", "--source", "slack"),
            *("--brief", "Add a --quiet flag.", "--dry-run"),
        ]
    )
    out = capsys.readouterr()
    assert code == 0
    data = json.loads(out.out)
    assert data["dry_run"] is True
    assert data["create"]["resources"][0]["authorization_token"] == cli.REDACTED
    assert "Add a --quiet flag." in data["first_message"]
    assert "ghp_secret" not in out.out + out.err


def test_cli_run_refuses_unpushed_factory_changes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".claude" / "skills" / "review" / "SKILL.md").write_text("changed\n")
    code = cli.main(
        ["run", "--slug", "x", "--requested-by", "a", "--source", "dev", "--brief", "b"]
    )
    assert code == 1
    assert "differs from origin/main" in capsys.readouterr().err


def test_cli_check_boundary_flags_product_edits(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("x = 1\n")
    assert cli.main(["check", "--boundary"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert any("src/app.py" in p for p in data["problems"])


def test_cli_check_passes_on_clean_factory(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check", "--boundary"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

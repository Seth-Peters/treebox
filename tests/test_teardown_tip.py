"""Teardown advice must describe the current tip, not a reused branch name."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from treebox import cli, forge


def git_at(path: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.test",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("advance", ["unchanged", "local", "pushed", "no-upstream"])
def test_teardown_chooser_checks_current_tip_after_squash_merge(
    repo: Path, tmp_path: Path, hermetic_config, monkeypatch: pytest.MonkeyPatch, advance: str
) -> None:
    runner = CliRunner()
    root = tmp_path / "wts"
    result = runner.invoke(
        cli.app,
        ["create", "tip-work", "--repo", str(repo), "--root", str(root), "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    worktree = Path(json.loads(result.stdout)["worktree_path"])
    (worktree / "setup.log").unlink()
    (worktree / "first.txt").write_text("already merged\n")
    git_at(worktree, "add", "first.txt")
    git_at(worktree, "commit", "-m", "initial work")
    git_at(worktree, "push", "-u", "origin", "tip-work")
    merged_tip = git_at(worktree, "rev-parse", "HEAD")
    git_at(repo, "merge", "--squash", "tip-work")
    git_at(repo, "commit", "-m", "squash integration")
    git_at(repo, "push", "origin", "main")
    if advance != "unchanged":
        (worktree / "second.txt").write_text("must not be lost\n")
        git_at(worktree, "add", "second.txt")
        git_at(worktree, "commit", "-m", "new work after merge")
        if advance == "pushed":
            git_at(worktree, "push", "origin", "tip-work")
        if advance == "no-upstream":
            git_at(worktree, "branch", "--unset-upstream")

    def pr_response(argv: list[str], **kwargs):
        assert argv[:3] == ["gh", "pr", "view"]
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "state": "MERGED",
                    "number": 7,
                    "headRefOid": merged_tip,
                    "url": "https://example.test/pr/7",
                    "isDraft": False,
                }
            ),
            "",
        )

    monkeypatch.setattr(forge, "_run", pr_response)
    monkeypatch.setattr(forge, "detect", lambda path: forge.GhForge("example.test"))
    monkeypatch.setattr(cli, "_stdin_isatty", lambda: True)
    observed = []

    def inspect_picker(reporter, entries):
        observed.extend(entries)
        return []  # Inspect the actual chooser advice without deleting fixture work.

    monkeypatch.setattr(cli, "_prompt_selection", inspect_picker)
    result = runner.invoke(
        cli.app,
        ["teardown", "--repo", str(repo), "--root", str(root)],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert len(observed) == 1
    advice = observed[0].status
    assert advice.safe is (advance == "unchanged"), advice
    if advance != "unchanged":
        assert "✓ merged" not in advice.label
        assert advice.style != "wt.ok"
    assert worktree.is_dir()
    assert git_at(repo, "rev-parse", "refs/heads/tip-work") == git_at(worktree, "rev-parse", "HEAD")


def test_gitlab_new_open_request_wins_over_old_merged_request(monkeypatch: pytest.MonkeyPatch):
    def response(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {"state": "merged", "iid": 7, "sha": "a" * 40},
                    {"state": "opened", "iid": 8, "sha": "b" * 40},
                ]
            ),
            "",
        )

    monkeypatch.setattr(forge, "_run", response)
    result = forge.GlabForge("example.test").pr_status("/repo", "reused")
    assert result is not None and result.open and result.number == 8


@pytest.mark.parametrize("head", [None, "", "abc", [], 12])
def test_missing_or_invalid_merge_head_never_proves_safety(monkeypatch, head):
    from treebox import status

    monkeypatch.setattr(
        forge,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, json.dumps({"state": "MERGED", "headRefOid": head}), ""
        ),
    )
    pr = forge.GhForge("example.test").pr_status("/repo", "work")
    assert pr is not None and pr.head_oid is None
    advice = status.compute(
        dirty=False,
        placeholder=False,
        has_upstream=True,
        ahead=0,
        merged_ancestor=False,
        pr=pr,
        current_oid="a" * 40,
    )
    assert not advice.safe
    assert "not verified" in advice.label


def test_exact_merged_tip_is_not_safe_when_dirty_or_unreadable():
    from treebox import status

    pr = forge.PRStatus(state="merged", number=7, head_oid="a" * 40)
    for dirty, tip in [(True, "a" * 40), (False, None)]:
        advice = status.compute(
            dirty=dirty,
            placeholder=False,
            has_upstream=True,
            ahead=0,
            merged_ancestor=False,
            pr=pr,
            current_oid=tip,
        )
        assert not advice.safe


@pytest.mark.parametrize(
    "provider, payload",
    [
        (forge.GhForge, {"state": "MERGED", "number": 7, "headRefOid": "a" * 40}),
        (forge.GlabForge, [{"state": "merged", "iid": 7, "sha": "a" * 40}]),
    ],
)
def test_providers_keep_merge_head_identity(monkeypatch, provider, payload):
    def response(argv, **kwargs):
        if provider is forge.GhForge:
            assert "headRefOid" in argv[-1]
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(forge, "_run", response)
    pr = provider("example.test").pr_status("/repo", "work")
    assert pr is not None and pr.head_oid == "a" * 40


def test_latest_gitlab_merged_request_is_not_hidden_by_older_open_request(monkeypatch):
    monkeypatch.setattr(
        forge,
        "_run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {"state": "opened", "iid": 7, "sha": "b" * 40},
                    {"state": "merged", "iid": 8, "sha": "a" * 40},
                ]
            ),
            "",
        ),
    )
    pr = forge.GlabForge("example.test").pr_status("/repo", "work")
    assert pr is not None and pr.merged and pr.number == 8 and pr.head_oid == "a" * 40

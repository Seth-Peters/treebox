"""The e2e harness's own logic: verdicts, the run's exit status, the summary,
stream checks and the docker reaper's name filter. Pure functions only: no
CLI runs, no docker."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

import harness  # noqa: E402
from harness import Result, RunRecord  # noqa: E402


def result(sid: str, verdict: harness.Verdict, group: str = "A") -> Result:
    return Result(sid, group, sid, verdict, "live", f"{verdict} reason", [], [], [], 0.0)


def run(argv: list[str], exit: int, stdout: str = "", stderr: str = "") -> RunRecord:
    return RunRecord(1, "cli", "pipe", ["/x/treebox", *argv], "/", {}, exit, stdout, stderr, 0.0)


def test_a_failed_scenario_fails_the_run() -> None:
    results = [result("A1", "pass"), result("A2", "fail")]
    assert harness.run_exit_status(results, [], False) == 1


def test_skips_and_known_bugs_do_not_fail_the_run() -> None:
    results = [result("A1", "pass"), result("H1", "skip", "H"), result("P1", "known-bug", "P")]
    assert harness.run_exit_status(results, [], False) == 0


def test_a_missing_required_prerequisite_fails_the_run() -> None:
    assert harness.run_exit_status([result("A1", "pass")], ["docker"], False) == 1


def test_an_interrupted_run_fails() -> None:
    assert harness.run_exit_status([result("A1", "pass")], [], True) == 1


def test_summary_shows_a_skipped_group_as_skipped_never_passed() -> None:
    results = [result("A1", "pass"), result("H1", "skip", "H"), result("H2", "skip", "H")]
    text = harness.render_summary(results, {"exit_status": 0, "groups": []})
    row = next(line for line in text.splitlines() if line.startswith("| H"))
    assert row.startswith("| H (skipped) |")
    assert row.split("|")[3].strip() == "0"  # pass column
    assert "| **all** | | **1** | **0** | **2** | **0** |" in text
    assert "### Skipped (not passed)" in text


def test_summary_lists_failures_and_known_bugs() -> None:
    results = [result("B1", "fail", "B"), result("P1", "known-bug", "P")]
    text = harness.render_summary(results, {"exit_status": 1, "groups": []})
    assert "evidence: FAILED" in text
    assert "- **B1** B1: fail reason" in text
    assert "### Known product bugs" in text


def test_stream_check_accepts_clean_json() -> None:
    assert harness.stream_violations(run(["list", "--json"], 0, '{"schemaVersion": 1}')) == []


def test_stream_check_rejects_chatter_on_json_stdout() -> None:
    bad = run(["list", "--json"], 0, 'warning: x\n{"schemaVersion": 1}')
    assert harness.stream_violations(bad)


def test_stream_check_wants_one_error_object_on_stderr() -> None:
    good = run(["enter", "x", "--json"], 3, "", '{"schemaVersion": 1, "error": {"code": "N"}}')
    assert harness.stream_violations(good) == []
    assert harness.stream_violations(run(["enter", "x", "--json"], 3, "", "Traceback"))


def test_stream_check_ignores_agent_args_after_double_dash() -> None:
    launch = run(["enter", "x", "--", "--json"], 0, "agent output")
    assert harness.stream_violations(launch) == []


def test_reaper_matches_the_prefix_only() -> None:
    lines = [
        "treebox-e2e-dockered-0123456789 abc running",
        "treebox-keen-eagle-f8404bdfc2 def exited",
        "treebox-deft-wren-27a7f4f2e1 ghi exited",
        "my-treebox-e2e-x jkl running",
        "treebox-i58-cmds-98ab07282d mno running",
    ]
    assert harness.e2e_owned("containers", lines) == [lines[0]]
    volumes = ["treebox-e2e-vol-e2e-a", "treebox-shellhistory-e2e-a", "treebox-shellhistory-keen"]
    assert harness.e2e_owned("volumes", volumes) == volumes[:2]


def test_no_real_agent_on_path(tmp_path: Path) -> None:
    stub = tmp_path / "stub"
    other = tmp_path / "other"
    stub.mkdir()
    other.mkdir()
    for d in (stub, other):
        for name in harness.AGENT_NAMES:
            (d / name).write_text("#!/bin/sh\n")
            (d / name).chmod(0o755)
    harness.assert_no_real_agent(str(stub), stub)
    try:
        harness.assert_no_real_agent(f"{stub}:{other}", stub)
    except harness.HarnessError as exc:
        assert "real 'claude'" in str(exc)
    else:
        raise AssertionError("a second claude on PATH must be refused")


def test_an_incomplete_teardown_may_print_its_records_before_the_error() -> None:
    error = '{"schemaVersion": 1, "error": {"code": "TEARDOWN_INCOMPLETE", "message": "m"}}'
    records = '{"schemaVersion": 1, "worktrees": [], "warnings": []}'
    td = run(["teardown", "x", "--json"], 1, stdout=records, stderr=error)
    assert harness.stream_violations(td) == []
    assert harness.stream_violations(run(["teardown", "x", "--json"], 1, "garbage", error))
    other = '{"schemaVersion": 1, "error": {"code": "DIRTY_WORKTREE", "message": "m"}}'
    assert harness.stream_violations(run(["teardown", "x", "--json"], 5, records, other))


def test_a_known_bugs_runs_are_not_judged_again_by_the_invariants() -> None:
    crash = run(["create", "x", "--json"], 1, stderr="Traceback")
    ok = run(["version"], 0, stdout="1.0\n")
    results = [result("B26", "known-bug", "B"), result("A1", "pass")]
    judged = harness.runs_for_invariants([("B26", crash), ("A1", ok)], results)
    assert judged == [("A1", ok)]
    x1 = next(r for r in harness.invariant_results(judged) if r.id == "X1")
    assert x1.verdict == "pass"

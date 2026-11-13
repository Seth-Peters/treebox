"""treebox end-to-end evidence: one command, one bundle.

    uv run python tests/e2e/run.py evidence --out DIR [--groups A,B,...]
        [--require docker] [--inject-failure]
    uv run python tests/e2e/run.py fixture     # a fixture for a manual sweep
    uv run python tests/e2e/run.py reap        # remove leftover e2e docker resources

``evidence`` runs every scenario in ``scenarios.py`` (ids and pass criteria:
``.claude/skills/treebox-e2e/references/scenarios.md``) against fresh
disposable fixtures and writes the evidence bundle to DIR, which must be
outside this repository:

    manifest.json   commit, treebox version, platform, prerequisites, skips
    results.json    per scenario: id, verdict, checks, and every run's argv
                    and exit status, with links to its captured output
    summary.md      the pass / fail / skip table (the CI job summary)
    scenarios/<id>/ run-NN.stdout, run-NN.stderr, state.json (git,
                    filesystem and container state after the scenario)

Exit status: 0 when no scenario failed and every ``--require``d prerequisite
was present; 1 otherwise; 2 for a usage error. A skipped scenario never counts
as passed.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness
import scenarios  # noqa: F401  (registers the scenarios)
from harness import (
    GROUPS,
    PROJECT,
    SCENARIOS,
    Bundle,
    Ctx,
    Fixture,
    Result,
    RunRecord,
    SkipScenario,
)


def treebox_bin() -> Path:
    """The console script of this checkout's environment, called directly."""
    path = Path(sys.executable).parent / "treebox"
    if not path.is_file():
        raise harness.HarnessError(
            f"{path} not found: run this through `uv run` from the treebox checkout"
        )
    return path


def _collect(sink: list[tuple[str, RunRecord]], sid: str, run: RunRecord) -> None:
    sink.append((sid, run))


def run_scenario(
    sc: harness.Scenario,
    fx: Fixture,
    prereqs: dict[str, harness.Prerequisite],
    all_runs: list[tuple[str, RunRecord]],
) -> tuple[Result, dict[str, Any] | None]:
    missing = [p for p in sc.requires if not prereqs[p].available]
    if missing:
        reason = "; ".join(f"{p}: {prereqs[p].detail}" for p in missing)
        return _skip(sc, f"missing prerequisite ({reason})"), None
    ctx = Ctx(fx, functools.partial(_collect, all_runs, sc.id))
    started = time.monotonic()
    verdict: harness.Verdict
    try:
        sc.func(ctx)
        failed = [c for c in ctx.checks if not c.ok]
        if failed:
            verdict, reason = "fail", failed[0].message
        elif not ctx.checks:
            verdict, reason = "fail", "the scenario recorded no checks"
        else:
            verdict, reason = "pass", f"{len(ctx.checks)} checks passed"
    except SkipScenario as exc:
        verdict, reason = "skip", str(exc)
    except AssertionError as exc:
        verdict, reason = "fail", f"precondition failed: {exc}"
        ctx.note(harness.format_exception())
    except Exception as exc:  # collect, don't abort: a crash is a finding
        failed = [c for c in ctx.checks if not c.ok]
        crash = f"harness error: {type(exc).__name__}: {exc}"
        # A failed check before the crash is the finding; the crash follows it.
        verdict, reason = "fail", f"{failed[0].message} (then {crash})" if failed else crash
        ctx.note(harness.format_exception())
    output = "\n".join(r.stdout + r.stderr for r in ctx.runs)
    if sc.known_bug and verdict == "fail" and re.search(sc.known_bug_signature, output):
        verdict, reason = "known-bug", f"{sc.known_bug} [{reason}]"
    elif sc.known_bug and verdict == "pass":
        ctx.note(f"known bug did not reproduce in this run: {sc.known_bug}")
    state = harness.capture_state(fx, prereqs["docker"].available)
    result = Result(
        id=sc.id,
        group=sc.group,
        title=sc.title,
        verdict=verdict,
        evidence=sc.evidence,
        reason=reason,
        checks=ctx.checks,
        runs=ctx.runs,
        notes=ctx.notes,
        duration=round(time.monotonic() - started, 2),
    )
    return result, state


def _skip(sc: harness.Scenario, reason: str) -> Result:
    return Result(sc.id, sc.group, sc.title, "skip", sc.evidence, reason, [], [], [], 0.0)


def _fail(sc: harness.Scenario, reason: str) -> Result:
    return Result(sc.id, sc.group, sc.title, "fail", sc.evidence, reason, [], [], [], 0.0)


def evidence(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser().resolve()
    if out == PROJECT or PROJECT in out.parents:
        print(f"--out {out} is inside the repository; bundles are never committed", file=sys.stderr)
        return 2
    if out.exists() and any(out.iterdir()):
        print(f"--out {out} is not empty", file=sys.stderr)
        return 2
    if args.inject_failure:
        import injected  # noqa: F401  (registers Z1, a scenario that must fail)

    selected = set(args.groups.split(",")) if args.groups else None
    only = set(args.only.split(",")) if args.only else None
    if only is not None:
        unknown_ids = sorted(only - {s.id for s in SCENARIOS})
        if unknown_ids:
            print(f"unknown scenario id(s): {', '.join(unknown_ids)}", file=sys.stderr)
            return 2
        selected = {s.group for s in SCENARIOS if s.id in only}
    required = [p for p in (args.require or "").split(",") if p]

    tb_bin = treebox_bin()
    base_env = dict(os.environ)
    prereqs = harness.probe_prerequisites(base_env)
    unknown = [p for p in required if p not in prereqs]
    if unknown:
        print(f"unknown prerequisite(s) for --require: {', '.join(unknown)}", file=sys.stderr)
        return 2

    bundle = Bundle(out)
    real_treebox_home = Path.home() / ".treebox"
    before_home = harness.snapshot_tree(real_treebox_home)
    before_status = harness.git_status(PROJECT)
    version = subprocess.run([str(tb_bin), "version"], capture_output=True, text=True)
    commit = subprocess.run(
        ["git", "-C", str(PROJECT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    manifest: dict[str, Any] = {
        "schemaVersion": harness.BUNDLE_SCHEMA_VERSION,
        "command": [Path(sys.argv[0]).name, *sys.argv[1:]],
        "commit": commit,
        "worktree_dirty": bool(before_status.strip()),
        "treebox_version": version.stdout.strip(),
        "treebox_bin": str(tb_bin),
        "python": sys.version.split()[0],
        "platform": harness.describe_platform(),
        "started": harness.now(),
        "prerequisites": {
            p.name: {"available": p.available, "detail": p.detail} for p in prereqs.values()
        },
        "required": required,
        "groups": [
            *(
                {"letter": g.letter, "title": g.title}
                for g in sorted(GROUPS.values(), key=lambda g: harness._group_order(g.letter))
            ),
            {"letter": "X", "title": "cross-cutting invariants"},
        ],
        "status": "running",
    }
    bundle.write_manifest(manifest)

    all_runs: list[tuple[str, RunRecord]] = []
    fixture_dirs: list[Path] = []
    leftovers: dict[str, dict[str, list[str]]] = {}
    setups: list[dict[str, Any]] = []
    interrupted = False
    try:
        for letter in sorted(GROUPS, key=harness._group_order):
            if selected is not None and letter not in selected:
                continue
            group = GROUPS[letter]
            members = [s for s in SCENARIOS if s.group == letter and (only is None or s.id in only)]
            print(f"== {letter}: {group.title} ({len(members)} scenarios)", file=sys.stderr)
            missing = [p for p in group.requires if not prereqs[p].available]
            if missing:
                reason = "; ".join(f"{p}: {prereqs[p].detail}" for p in missing)
                for sc in members:
                    bundle.add(_skip(sc, f"missing prerequisite ({reason})"), None)
                continue
            for sc in members:
                fx = Fixture.create(tb_bin=tb_bin, base_env=base_env)
                fixture_dirs.append(fx.sbx)
                try:
                    setup_error = ""
                    if group.setup is not None:
                        setup_id = f"{sc.id}-setup"
                        ctx = Ctx(fx, functools.partial(_collect, all_runs, setup_id))
                        try:
                            group.setup(ctx)
                        except Exception as exc:
                            setup_error = f"group setup failed: {type(exc).__name__}: {exc}"
                            ctx.note(harness.format_exception())
                        setups.append(bundle.add_setup(setup_id, ctx, setup_error))
                    if setup_error:
                        bundle.add(_fail(sc, setup_error), None)
                        continue
                    result, state = run_scenario(sc, fx, prereqs, all_runs)
                    mark = result.verdict.upper()
                    print(f"   {mark} {sc.id} {sc.title}: {result.reason}", file=sys.stderr)
                    bundle.add(result, state)
                finally:
                    if prereqs["docker"].available:
                        found = harness.reap_docker(fx.env)
                        if any(found.values()):
                            leftovers[sc.id] = found
                    fx.remove()
    except KeyboardInterrupt:
        interrupted = True

    # Cross-cutting invariants over every CLI run of the sweep.
    if selected is None or "X" in selected:
        for result in harness.invariant_results(all_runs):
            bundle.add(result, None)
        bundle.add(_pollution(real_treebox_home, before_home, before_status, fixture_dirs), None)

    failed = [r for r in bundle.results if r.verdict == "fail"]
    required_missing = [p for p in required if not prereqs[p].available]
    exit_status = harness.run_exit_status(bundle.results, required_missing, interrupted)
    manifest.update(
        {
            "finished": harness.now(),
            "status": "interrupted" if interrupted else "complete",
            "counts": bundle.counts(),
            "skipped": [
                {"id": r.id, "reason": r.reason} for r in bundle.results if r.verdict == "skip"
            ],
            "failed": [r.id for r in failed],
            "known_bugs": [
                {"id": r.id, "reason": r.reason} for r in bundle.results if r.verdict == "known-bug"
            ],
            "required_missing": required_missing,
            "docker_leftovers_reaped": leftovers,
            "group_setups": setups,
            "exit_status": exit_status,
        }
    )
    bundle.write_manifest(manifest)
    summary = bundle.write_summary(manifest)
    print(summary, file=sys.stderr)
    print(f"evidence bundle: {out}", file=sys.stderr)
    return exit_status


def _pollution(
    home: Path, before_home: dict[str, tuple[int, int]], before_status: str, fixtures: list[Path]
) -> Result:
    """X5: the run left no trace on the host outside its bundle."""
    checks = [
        harness.Check(
            harness.snapshot_tree(home) == before_home, f"nothing changed under real {home}"
        ),
        harness.Check(
            harness.git_status(PROJECT) == before_status,
            "`git status` of the treebox checkout is unchanged",
        ),
        harness.Check(
            not [f for f in fixtures if f.exists()], f"all {len(fixtures)} fixture dirs removed"
        ),
    ]
    remaining = harness.docker_resources(os.environ) if _docker_up() else None
    if remaining is not None:
        checks.append(
            harness.Check(
                not any(remaining.values()),
                f"no {harness.DOCKER_PREFIX}* containers, images or volumes remain ({remaining})",
            )
        )
    failed = [c for c in checks if not c.ok]
    return Result(
        id="X5",
        group="X",
        title="no host pollution",
        verdict="fail" if failed else "pass",
        evidence="live",
        reason=failed[0].message if failed else f"{len(checks)} checks passed",
        checks=checks,
        runs=[],
        notes=[],
        duration=0.0,
    )


def _docker_up() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except OSError:
        return False


def fixture(args: argparse.Namespace) -> int:
    """Build one fixture for a manual sweep and print shell assignments."""
    fx = Fixture.create(tb_bin=treebox_bin(), base_env=dict(os.environ))
    fx.install_offline_template()
    env_args = " ".join(shlex.quote(f"{k}={v}") for k, v in sorted(fx.env.items()))
    launcher = fx.sbx / "tb"
    launcher.write_text(
        "#!/bin/sh\n"
        "# The fixture's treebox: the hermetic env, the stub agent first on PATH.\n"
        f'exec env -i {env_args} {shlex.quote(str(fx.tb_bin))} "$@"\n'
    )
    launcher.chmod(0o755)
    values = {
        "SBX": fx.sbx,
        "REPO": fx.repo,
        "ROOT": fx.root,
        "E2E_HOME": fx.home,
        "TREEBOX_HOME": fx.treebox_home,
        "TREEBOX_CONFIG": fx.config,
        "E2E_TEMPLATE": harness.OFFLINE_TEMPLATE,
        "E2E_MARK": "e2e",
    }
    for key, value in values.items():
        print(f"export {key}={shlex.quote(str(value))}")
    return 0


def reap(args: argparse.Namespace) -> int:
    found = harness.reap_docker(os.environ)
    for kind, names in found.items():
        for name in names:
            print(f"removed {kind[:-1]}: {name}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("evidence", help="run the scenarios and write an evidence bundle")
    ev.add_argument("--out", required=True, help="bundle directory, outside the repository")
    ev.add_argument("--groups", help="comma-separated group letters (default: all)")
    ev.add_argument(
        "--only",
        help="scenario ids, each with a fresh fixture and its group setup (default: all)",
    )
    ev.add_argument(
        "--require",
        help="comma-separated prerequisites whose absence fails the run (e.g. docker)",
    )
    ev.add_argument(
        "--inject-failure",
        action="store_true",
        help="add scenario Z1, which always fails: proves a failed scenario fails the run",
    )
    ev.set_defaults(func=evidence)
    sub.add_parser("fixture", help="build a fixture for a manual sweep").set_defaults(func=fixture)
    sub.add_parser("reap", help="remove leftover e2e docker resources").set_defaults(func=reap)
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
    except harness.HarnessError as exc:
        print(f"treebox e2e: {exc}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())

---
name: treebox-e2e
description: Test treebox end-to-end — run the deterministic gate (lint, types, pytest, golden snapshots) plus a scenario-driven usability sweep of every CLI command and flag against an isolated fixture, and report breaking changes. Use when asked to e2e-test treebox, verify a branch didn't break the CLI, run the full test matrix, or usability-test treebox commands.
---

# Treebox end-to-end testing

Two complementary halves, in order:

1. **Deterministic gate** — the repo's scripted checks (`scripts/validate.sh`).
2. **Usability sweep** — actually run each CLI command with its flags against a
   throwaway fixture, inspect real output, and judge it against per-scenario
   pass criteria (`references/scenarios.md`).

The deterministic gate catches what tests already encode; the usability sweep
catches what they don't — regressions in flag semantics, exit codes, stream
discipline, JSON shape, filesystem effects, and drift between behavior and docs.

## Hard rules

- **Never launch a real agent.** Always end scenarios with `--print`, `--json`,
  or `--dry-run`. A bare `create`/`enter` would exec `claude
  --dangerously-skip-permissions` for real.
- **Never test against a real repo or the real `~/.treebox`.** Everything runs
  inside the fixture sandbox (`fixture.sh` scopes `TREEBOX_HOME`,
  `TREEBOX_CONFIG`, repo, and worktree root under one `mktemp -d`).
- **Collect, don't abort.** A failed scenario is a finding to record; keep
  going so one regression doesn't hide the rest. Only abort if the fixture
  itself is broken.
- **Judge behavior, not prose.** Exit codes, JSON fields, files on disk,
  stdout/stderr separation. Exact human-output text is `tests/golden/`'s job —
  don't duplicate it, and don't fail a scenario over cosmetic wording.
- Run scenarios **sequentially** — worktree names collide and the per-name lock
  plus shared fixture state make parallel sweeps flaky by construction.
- Run the sweep from a **bash** shell — the fixture is bash and is `source`d. The
  session shell may be zsh, where unquoted no-match globs (`/tmp/treebox-e2e.*`)
  error instead of returning empty, tripping cleanup checks.

## Phase 0 — deterministic gate

```bash
./scripts/validate.sh
```

Ruff, mypy, shell-asset checks, the full pytest suite, golden CLI snapshots,
and a live host-runner smoke test. If it fails, record the failure as a finding
and still continue to the usability sweep (unless the package doesn't even
import — then stop; nothing else can run). For a quick pre-sweep sanity check
on small diffs, `uv run --extra dev python -m pytest` alone is acceptable, but
a full e2e report requires `validate.sh`.

## Phase 1 — fixture

```bash
source .claude/skills/treebox-e2e/fixture.sh   # from the treebox repo root, in bash
```

This creates a throwaway bare origin + clone (real uv project, `.env`, `main`
and `dev` branches), a scratch `TREEBOX_HOME`/`TREEBOX_CONFIG`, and defines:

- `$SBX` `$REPO` `$ROOT` — sandbox root, fixture clone, worktree root
- `tb <args…>` — runs this working tree's CLI (`uv run --project … treebox`)
- `e2e_cleanup` — reaps `treebox-e2e-*` docker artifacts, then removes the sandbox
- `e2e_reap_docker` — docker-only sweep, safe anytime (even after a crashed run)
- `$E2E_MARK` (`"e2e"`) — the reserved name prefix docker scenarios must use

Always pass `--repo "$REPO" --root "$ROOT"` on worktree commands. Sourcing the
script twice gives a fresh sandbox (new `$SBX`) — use that to reset state
mid-sweep if a scenario corrupts the fixture.

## Phase 2 — scenario sweep

Work through **`references/scenarios.md`** top to bottom: groups A (doctor/
version), B (create), C (enter), D (list), E (teardown), F (template),
G (config), H (docker), plus the cross-cutting invariants X1–X5. For each
scenario record: ID, the exact command run, exit code, and PASS / FAIL / SKIP
with a one-line reason. On FAIL capture the actual stdout+stderr.

Scope by request: a targeted ask ("did my teardown change break anything?")
can run only the affected groups plus X-invariants, but say so in the report.
A full e2e run means every group.

## Phase 3 — docker (conditional)

Run group H only if `tb doctor --isolation docker --json` reports `ok: true`
(scenario A4). No daemon → mark all of H **SKIP (no docker daemon)** — skipped
is not passed; the report must say docker went unexercised. H builds a real
image; expect it to be the slowest phase.

Name every group-H worktree with the `e2e-` prefix (e.g. `e2e-dockered`). Docker
containers/images escape the sandbox dir into the global daemon namespace as
`treebox-<name>-<hash>`; the prefix makes them `treebox-e2e-*` so Phase 4 can reap
them without touching a user's real worktrees. Keep the prefix on any docker
scenario you add.

## Phase 4 — cleanup & pollution check

```bash
e2e_cleanup   # reaps treebox-e2e-* docker containers/images, then removes $SBX
```

`e2e_cleanup` is one pass: it reaps every `treebox-e2e-*` docker artifact — so H5's
intentional `--skip-container` leftover and any containers from a crashed run go
too — and then removes the sandbox dir. If a run died before you could call it,
`e2e_reap_docker` alone clears the docker side; it only ever touches
`treebox-e2e-*`, never a real worktree.

Then verify invariant X5: no new files under the real `~/.treebox`, no stray
`treebox-e2e.*` dirs in `$TMPDIR`, `git status` of the treebox repo unchanged, and
— if group H ran — no `treebox-e2e-*` containers or images left in the daemon
(`docker ps -a --filter name=treebox-e2e-`, `docker images 'treebox-e2e-*'`).

## Report

End with a single markdown report:

1. **Verdict first** — "no breaking changes found" or "N breaking changes",
   one line each.
2. **Counts** — passed / failed / skipped per group, plus the Phase 0 result.
3. **Failures** — per finding: scenario ID, command, expected vs. actual
   (with the captured output), and a classification:
   - **regression** — behavior contradicts documented contract (exit-code
     table, JSON schema, invariants in `CLAUDE.md`);
   - **doc drift** — behavior is coherent but `docs/usage.md`,
     `skills/treebox/SKILL.md`, or `README.md` say otherwise (name the file
     and line);
   - **environment** — missing daemon/login/network, not treebox's fault.
4. **Skips** — what was not exercised and why (docker, codex login, …).

Do not fix regressions as part of the sweep — report them. If asked to fix,
re-run the affected scenario group afterwards to confirm.

## Extending the matrix

When a new command or flag lands, add a scenario to `references/scenarios.md`
in the matching group: state the command, what it should do, and a
*behavioral* pass criterion (exit code / JSON field / file effect). If the new
behavior also changes human output, update `tests/golden/` via
`scripts/golden-diff.sh --update` separately — golden owns exact text, this
skill owns semantics.

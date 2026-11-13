---
name: treebox-e2e
description: Test treebox end-to-end - run the deterministic gate (lint, types, pytest, golden snapshots), the scripted scenario sweep that writes a durable evidence bundle (real CLI, real git, real Docker engine with an offline template and a stub agent), and a hands-on usability review of every CLI route against an isolated fixture; keep the feature map aligned with the source; report breaking changes. Use when asked to e2e-test treebox, verify a branch didn't break the CLI, run the full test matrix, produce verification evidence, or usability-test treebox commands.
---

# Treebox end-to-end testing

Three complementary halves, in order:

1. **Deterministic gate** - the repo's scripted checks (`scripts/validate.sh`).
2. **Evidence run** - one command drives every scenario in
   `references/scenarios.md` through the real CLI against disposable fixtures
   and writes an **evidence bundle** (`tests/e2e/run.py`).
3. **Usability review** - run the routes yourself in a fixture and judge what
   code cannot: human output, wording, alignment, and behavior no scenario
   covers yet.

`references/feature-map.md` indexes every user-visible route to its source and
its scenarios. `references/scenarios.md` holds each scenario's command and pass
criterion; `tests/e2e/scenarios.py` holds the checks, by the same id.

## Hard rules

- **Never launch a real agent.** The fixture's `tb` and the evidence run put
  the controlled stub agent (`tests/e2e/stub-agent`) first on `PATH`, and the
  harness refuses to run when any other `claude` or `codex` is reachable. In
  docker scenarios the offline template's agent is the same stub. So a bare
  `tb create` / `tb enter` (a real launch) is safe in the fixture. A printed
  host entry command (`sh -c 'cd … && exec claude …'`) is not: never run it in
  your own shell, where `claude` is the real agent. Replay it only with the
  stub first: `PATH="$SBX/stub-bin:$PATH" sh -c '…'`.
- **Never test against a real repo, the real `~/.treebox`, `~/.claude` or
  `~/.codex`.** The fixture scopes `HOME`, `TREEBOX_HOME`, `TREEBOX_CONFIG`,
  the repo and the worktree root under one temp dir. Its `~/.claude` and
  `~/.codex` hold fake login files.
- **Collect, don't abort.** A failed scenario is a finding to record; keep
  going so one regression doesn't hide the rest.
- **Judge behavior, not prose.** Exit codes, JSON fields, files on disk,
  stdout/stderr separation, git and container state. Exact human-output text is
  `tests/golden/`'s job.
- **A skip is never a pass.** A missing prerequisite (no docker engine, no
  base image, root user, no shell detection) makes the scenario SKIP with the
  reason, and the report must say what went unexercised.
- Each scenario uses a fresh fixture and its group setup. A selected id owns
  its required state. Run manual sweeps from **bash** and start a fresh fixture
  for each scenario (the fixture is bash and is `source`d).
- Docker names: name every docker worktree `e2e-…`, so its container, image and
  volume are `treebox-e2e-*` and the reaper finds them. The reaper removes
  those prefixes only, never by substring, never anything else.

## Phase 0 - deterministic gate

```bash
./scripts/validate.sh
```

Ruff, mypy, shell-asset checks, the full pytest suite (including
`tests/test_e2e_map.py`, which keeps the feature map, the scenario ids and the
source in step), golden CLI snapshots, and a live host-runner smoke test. It
never needs Docker. If it fails, record the failure as a finding and still
continue (unless the package does not import).

## Phase 1 - evidence run

```bash
uv run python tests/e2e/run.py evidence --out "$TMPDIR/treebox-evidence-$(date +%s)"
#   --groups B,E        only these groups (X invariants run only in a full run)
#   --only R44,H7       only these scenarios (each gets its group setup)
#   --require docker    a missing prerequisite fails the run instead of skipping
#   --inject-failure    adds Z1, which always fails (proves the failure path)
```

Each scenario runs on a fresh fixture, so `--only` needs no earlier scenario.
The fixture shares host tool binaries and the pinned base image; mutable repo,
worktree and template state is private to that scenario. `--out` must be outside
the repository and empty; bundles are never committed. The bundle survives a
failed scenario and a crash (it is written after every scenario):

- `manifest.json` - commit, treebox version, platform, prerequisites with
  details, what was skipped and why, known bugs, exit status. Group setup runs
  have a separate `<scenario-id>-setup` evidence entry.
- `results.json` - per scenario: id, verdict, evidence kind, every check, and
  every run's argv, exit status and output files.
- `summary.md` - the pass / fail / skip / known-bug table per group.
- `scenarios/<id>/run-NN.stdout|.stderr` and `state.json` (git worktrees,
  branches and status, files under the worktree root and treebox home, and
  `treebox-e2e-*` containers, images and volumes after the scenario).

Verdicts: **pass** (every check held), **fail**, **skip** (a prerequisite is
missing; never counted as passed), **known-bug** (the scenario failed exactly
as a documented, unfixed product bug predicts, matched by its output
signature; any other failure is a real fail). The command exits 1 on any fail,
a missing `--require`d prerequisite, or an interrupt.

Interactive routes (the teardown confirmation and chooser) run on a
pseudo-terminal through Python's `pty`. Parallel dispatch starts several CLI
processes at the same moment.

CI: `.github/workflows/e2e.yml` runs this on pull requests that touch the
runners, the sandbox assets or the e2e suite, and nightly: on `ubuntu-latest`
with `--require docker` (the real engine), and on `macos-latest`, where the
docker group shows as skipped. It uploads the bundle as the
`e2e-evidence-<os>` artifact (`gh run download <run-id>`) and writes the table
to the job summary.

## The docker group: the offline path

The docker group never needs the bundled 5.6 GB image or a network build:

- **Operator template** `tests/e2e/offline-template/` (installed as
  `$TREEBOX_HOME/templates/e2e-offline`; pass `--template e2e-offline`). Its
  Dockerfile starts from a digest-pinned `alpine/git` and only edits local
  files, so once that base image is present the build needs no network, and
  it works for any host uid, root included.
- **Controlled launch executable** `tests/e2e/stub-agent`. On the host the
  fixture links it as `claude` and `codex` in `$SBX/stub-bin`, first on the
  fixture's `PATH`. In the offline template it is `post-create.sh` (a symlink,
  copied as a file into the build context) and the Dockerfile links it as
  `/usr/local/bin/claude` and `/usr/local/bin/codex`. Invoked as an agent, it
  prints a record (its argv, cwd, uid, whether it runs in a container, whether
  `ANTHROPIC_API_KEY` is set) and exits with `--stub-exit=N` (default 0).
  Invoked as `post-create.sh`, it writes `/tmp/treebox-e2e-post-create`.

Prerequisites, probed once per run: `docker` (a reachable engine) and
`offline-base-image` (the pinned base image is present, or can be pulled).
Without either, every docker scenario is SKIP with the reason.

## Live and boundary evidence

Scenarios are **live** by default: real CLI, real git, real filesystem, real
engine. A **boundary** scenario uses a fake at one edge to inject a failure
the real world cannot produce on demand, and is labeled so in the bundle:

- `tests/e2e/fakes/gh` - a logged-in GitHub CLI with canned pull requests
  (R46: the chooser's merged badge).
- `tests/e2e/fakes/docker` - passes through to the real docker CLI but fails
  one subcommand (H10: a failed container removal).

The pytest suite's injected engine (`DockerRunner(docker=…)`) and
`scripts/golden-diff.sh`'s docker shim are boundary evidence too. Keep them
for failure injection; they never replace a live docker run.

## Phase 2 - usability review (by hand)

```bash
source .claude/skills/treebox-e2e/fixture.sh   # from the treebox repo root, in bash
```

The fixture is built by the same code as the evidence run (`run.py fixture`):
a throwaway bare origin + clone (real uv project, an untracked `.env`, `main`
and `dev` branches), a scratch `HOME`, `TREEBOX_HOME` and `TREEBOX_CONFIG`, the
offline template installed, and:

- `$SBX` `$REPO` `$ROOT` - sandbox root, fixture clone, worktree root
- `tb <args…>` - this working tree's CLI with the fixture's environment; it
  adds no output of its own, so stream checks see only treebox
- `e2e_cleanup` - reaps `treebox-e2e-*` docker resources, then removes `$SBX`
- `e2e_reap_docker` - the docker-only sweep, safe anytime (even after a crash)

Always pass `--repo "$REPO" --root "$ROOT"`. Sourcing again gives a fresh
sandbox. Read the human output of each route in a terminal, piped, and with
`NO_COLOR=1`: alignment, wrapping, truncation, colors, hint wording. Be picky.
Run a scenario by hand when its evidence is surprising.

## Phase 3 - cleanup & pollution check

The evidence run cleans up after itself and checks invariant X5 (nothing
changed under the real `~/.treebox`, `git status` of this checkout unchanged,
every fixture removed, no `treebox-e2e-*` docker resource left). After a manual
sweep run `e2e_cleanup` and make the same checks
(`docker ps -a --filter name=treebox-e2e-`, `docker images 'treebox-e2e-*'`,
`docker volume ls --filter name=treebox-e2e-`).

## Report

End with a single markdown report:

1. **Verdict first** - "no breaking changes found" or "N breaking changes",
   one line each.
2. **Counts** - the bundle's summary table, the bundle path, and the Phase 0
   result.
3. **Failures** - per finding: scenario id, command, expected vs. actual (from
   the bundle), and a classification (see the maintenance procedure):
   **product bug**, **doc drift**, **driver gap**, or **environment**.
4. **Skips and known bugs** - what was not exercised and why; which known bugs
   still reproduce.

Do not fix regressions as part of the sweep - report them. If asked to fix,
re-run the affected scenarios afterwards (`--only`).

## Maintenance procedure

Run this when routes change (a new flag, error code, config key, environment
variable or command) and before trusting a sweep on a changed branch.

1. **Derive the routes from the source.** Run
   `uv run --extra dev python -m pytest tests/test_e2e_map.py`. It lists every
   route the source defines that `references/feature-map.md` lacks, every row
   that names a route or source symbol that no longer exists, and every
   scenario id that the prose, the code and the map do not agree on.
2. **Read each mapped feature from source.** For each row (all rows in a full
   pass, the changed ones otherwise) read the referenced symbol and the docs
   that describe it (`docs/usage.md`, `docs/configuration.md`,
   `skills/treebox/SKILL.md`, `README.md`, `--help`).
3. **Drive it live.** Run its scenarios
   (`run.py evidence --only <ids> --out …`), then the route by hand in the
   fixture, including its error paths.
4. **Classify every difference**, exactly one class each:
   - **product bug** - the behavior contradicts the documented contract (exit
     codes, JSON schema, the invariants in `CLAUDE.md`). Report it with the
     bundle. Keep the scenario's checks as they are; until the fix lands, mark
     the scenario `known_bug=` with a `known_bug_signature=` regex that
     matches only that failure, and remove both when it is fixed.
   - **doc drift** - the behavior is coherent but a doc says otherwise. Name
     the file and line; fix the doc (or report it).
   - **driver gap** - the product is right and the docs agree, but a
     scenario, the harness, the fixture or the map is wrong or missing. Fix
     the suite.
   - **environment** - a missing engine, login or network; not treebox's fault.
5. **Add a route** with one row in `feature-map.md` plus one scenario: the
   prose (command, intent, a *behavioral* pass criterion) in `scenarios.md`
   under the matching group, the checks under the same id in
   `tests/e2e/scenarios.py`. Name docker worktrees `e2e-…`. If the route also
   changes human output, update `tests/golden/` separately
   (`scripts/golden-diff.sh --update`): golden owns exact text, this skill
   owns semantics.

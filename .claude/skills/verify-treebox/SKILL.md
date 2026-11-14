---
name: verify-treebox
description: Verify the treebox CLI (it creates git worktrees for AI agents, on the host or in docker sandboxes) end to end, the way a user drives it, and record the evidence. Use when you must prove a change works and breaks nothing, e2e-test treebox, produce verification evidence for a PR, run the full scenario sweep, or find out how to drive one treebox feature by hand. The factory engineer uses it after every implementation and fix round.
---

# Verify treebox

A change is not done because the code looks right. It is done when the real CLI was driven against a throwaway fixture and the evidence says so. Never claim a check passed that you did not run and see pass.

treebox is a short-lived CLI with `--help`, `--json`, `--dry-run` and classified errors. There is no server to keep alive and no control tool: each drive is one real `treebox` command against the fixture.

- `features/README.md` is the feature map: one file per user-facing feature, with the exact commands and the observable results.
- `references/routes.md` maps every route (command, flag, error code, config key, environment variable, behavior) to its feature, its source and its scenarios.
- `references/scenarios.md` holds each scenario's command and pass criterion; `tests/e2e/scenarios.py` holds the checks, by the same id. `tests/e2e/run.py evidence` runs them all.

## Hard rules

- **Never launch a real agent.** The fixture's `treebox` and the evidence run put the controlled stub agent (`tests/e2e/stub-agent`) first on treebox's `PATH`, and refuse to run when any other `claude` or `codex` is reachable there. In docker the offline template's agent is the same stub. So a bare `treebox create` / `treebox enter` (a real launch) is safe in the fixture. A printed host entry command (`sh -c 'cd … && exec claude …'`) is not: never run it in your own shell, where `claude` is the real agent. Replay it only with the stub first: `PATH="$SBX/stub-bin:$PATH" sh -c '…'`.
- **Never use the real repo, the real `~/.treebox`, `~/.claude`, `~/.codex` or the macOS Keychain.** The fixture scopes `HOME`, `TREEBOX_HOME`, `TREEBOX_CONFIG`, the repo and the worktree root under one temp dir. Its `~/.claude` and `~/.codex` hold fake login files, and its stub dir holds a fake `security` that finds no Keychain item.
- **Collect, do not abort.** A failed drive or scenario is a finding to record; keep going so one regression does not hide the rest.
- **Judge behavior, not prose.** Exit codes, JSON fields, files on disk, stdout/stderr separation, git and container state. Exact human text is `tests/golden/`'s job.
- **A skip is never a pass.** A missing prerequisite (no docker engine, no base image, root or non-root user, not macOS, no shell detection, the bundled image not opted in) is SKIP with the reason, and the report says what went unexercised. With no reachable engine, the docker work is SKIP, never PASS; with an engine, it runs.
- Docker names: name every docker worktree `e2e-…`, so its container, image and volume are `treebox-e2e-*` and the reaper finds them. The reaper removes those prefixes only.

## Launch

From the treebox checkout, in **bash**:

```bash
source .claude/skills/verify-treebox/fixture.sh
```

It builds the fixture with the same code as the evidence run (`tests/e2e/run.py fixture`) and prints `fixture ready: SBX=… REPO=… ROOT=… TREEBOX_HOME=…` on stderr. Ready means that line was printed and `e2e_doctor` is all `ok`. After it:

- `treebox` is `$SBX/bin/treebox`, first on `PATH`: this checkout's console script with the fixture's environment. It adds no output of its own, so stream checks see only treebox.
- `$SBX`, `$REPO`, `$ROOT`: sandbox root, fixture clone, worktree root. `$TREEBOX_HOME`, `$TREEBOX_CONFIG`, `$E2E_HOME`: the scratch treebox home, config and `HOME`. `$E2E_TEMPLATE` is `e2e-offline`.
- Every drive is one command, for example `treebox create fix-auth --repo "$REPO" --root "$ROOT" --json`.

Shell state does not survive between separate tool calls. In a new shell, attach to the same fixture instead of building another: `source .claude/skills/verify-treebox/fixture.sh --attach "$SBX"` (the path is in the ready line). Sourcing without `--attach` gives a fresh fixture.

## Doctor

One read-only check that answers "is this fixture worth driving?". Run it before the first drive, after any drive that failed or surprised you, and in every new shell:

```bash
e2e_doctor
```

Each line is `ok`, `FAIL` or `note`; it returns 1 on any `FAIL`. It proves:

- `treebox` on `PATH` is `$SBX/bin/treebox`, it runs this checkout's console script (`$E2E_TREEBOX_BIN`, under the checkout's `.venv`), the package imports from this checkout's `src/`, and `treebox version` is that build's version. An older release on `PATH` (for example `~/.local/bin/treebox`) is listed as `shadowed`.
- `TREEBOX_CONFIG`, `HOME` and `TREEBOX_HOME` are scratch paths under `$SBX`.
- `treebox doctor` passes, its `agent: claude` row is `$SBX/stub-bin/claude` (the stub is first on treebox's `PATH`) and its `login: claude` row is the fake login under `$SBX/home`.
- `engine: reachable`, or a `note` that the engine is not reachable and docker work is SKIP.
- A `note` for each pass-through variable set in your shell (`NO_COLOR`, `TZ`, `TREEBOX_NON_INTERACTIVE`, `TREEBOX_TEMPLATE_DIR`, `XDG_CACHE_HOME`, `DOCKER_HOST`).

## Drive

1. Read `features/README.md`: the baseline, the conventions and the index.
2. Pick the feature file(s) the change touches. `references/routes.md` names the feature of every route; `git diff --stat` against the routes' source column finds them.
3. Run its `Driving it with treebox` bullets, exactly as written, in a fresh fixture. Use `--json` for assertions and read the field; read the human output too, piped and with `NO_COLOR=1`, and be picky about alignment, wrapping and wording.
4. A behavior no bullet covers: drive it by hand the same way, record the command and the result, and add the bullet (see `/maintain-verify-treebox`).

To run a scenario's executable checks instead: `uv run python tests/e2e/run.py evidence --only H12,C18 --out "$TMPDIR/treebox-evidence-$(date +%s)"`.

### The docker features: the offline path

The docker features never need the bundled 5.6 GB image or a network build:

- **Operator template** `tests/e2e/offline-template/` (installed as `$TREEBOX_HOME/templates/e2e-offline`; pass `--template e2e-offline`). Its Dockerfile starts from a digest-pinned `alpine/git` and only edits local files, so once that base image is present the build needs no network, and it works for any host uid.
- **Controlled launch executable** `tests/e2e/stub-agent`. On the host the fixture links it as `claude` and `codex` in `$SBX/stub-bin`. In the offline template it is `post-create.sh` and the Dockerfile links it as `/usr/local/bin/claude` and `/usr/local/bin/codex`. As an agent it prints a record (its argv, cwd, uid, whether it runs in a container, whether `ANTHROPIC_API_KEY` is set) and obeys `--stub-exit=N`, `--stub-kill=SIG` and `--stub-sleep=N`. As `post-create.sh` it writes `/tmp/treebox-e2e-post-create`.

The firewall test template (`features/firewall.md`) proves when treebox runs the firewall, not what a real firewall blocks. The real egress lockdown, the bundled agents and Chromium need the bundled image: H18 builds it only with `TREEBOX_E2E_BUNDLED_IMAGE=1` (about 5.6 GB, needs the network) and is SKIP otherwise.

### Live and boundary evidence

Drives and scenarios are **live** by default: real CLI, real git, real filesystem, real engine. A **boundary** scenario uses a fake at one edge to inject a failure the real world cannot produce on demand, and is labeled so in the bundle:

- `tests/e2e/fakes/gh`: a logged-in GitHub CLI with canned pull requests (R46: the chooser's merged badge).
- `tests/e2e/fakes/docker`: passes through to the real docker CLI but fails one subcommand (H10: a failed container removal, `TEARDOWN_INCOMPLETE`).
- A fake macOS `security` that prints a login (A10, H13: the Keychain login source). The real Keychain is never read.

The pytest suite's injected engine (`DockerRunner(docker=…)`) and `scripts/golden-diff.sh`'s docker shim are boundary evidence too. They never replace a live docker run.

## Evidence

**For one feature**, keep per bullet: the command, stdout, stderr, the exit code, and the resulting state (the read-only second view: `treebox list --json`, the record, `git -C "$REPO" worktree list`, `docker inspect`). Save what the PR needs outside `$SBX` before cleanup, for example `treebox … >"$OUT/create-json.stdout" 2>"$OUT/create-json.stderr"; echo $? >"$OUT/create-json.exit"` with `OUT="$TMPDIR/treebox-drive-$(date +%s)"`.

**For the full sweep**, one command drives every scenario against fresh fixtures and writes an evidence bundle:

```bash
uv run python tests/e2e/run.py evidence --out "$TMPDIR/treebox-evidence-$(date +%s)"
#   --groups B,E        only these groups (X invariants run only in a full run)
#   --only R44,H7       only these scenarios (each gets its group setup)
#   --require docker    a missing prerequisite fails the run instead of skipping
#   --inject-failure    adds Z1, which always fails (proves the failure path)
```

`--out` must be outside the repository and empty; bundles are never committed. The bundle is written after every scenario, so it survives a failed scenario and a crash:

- `manifest.json`: commit, treebox version and binary, platform, prerequisites with details, what was skipped and why, known bugs, exit status. Group setup runs have a separate `<scenario-id>-setup` entry.
- `results.json`: per scenario, the id, verdict, evidence kind (live or boundary), every check, and every run's argv, exit status and output files.
- `summary.md`: the pass / fail / skip / known-bug table per group.
- `scenarios/<id>/run-NN.stdout|.stderr` and `state.json` (git worktrees, branches and status, files under the worktree root and treebox home, and `treebox-e2e-*` containers, images and volumes after the scenario).

Verdicts: **pass** (every check held), **fail**, **skip** (a prerequisite is missing; never counted as passed), **known-bug** (the scenario failed exactly as a documented, unfixed product bug predicts, matched by its output signature; any other failure is a real fail). The command exits 1 on any fail, a missing `--require`d prerequisite, or an interrupt. Interactive routes run on a pseudo-terminal through Python's `pty`; parallel dispatch starts several CLI processes at once.

CI: `.github/workflows/e2e.yml` runs the sweep on pull requests that touch the runners, the sandbox assets, the e2e suite or this skill, and nightly: on `ubuntu-latest` with `--require docker` (the real engine), and on `macos-latest`, where the docker group is SKIP. It uploads the bundle as `e2e-evidence-<os>` (`gh run download <run-id>`) and writes the table to the job summary.

**Proof standards.** Drive the real user path, never internal setters or test-only entry points. Capture the action and the resulting state. Verify side effects (worktree, branch, record, ledger, container) as well as the output. Fakes only at a boundary that already isolates an external system, labeled as boundary evidence. When the safe path is `--dry-run`, check what it skips by observing (no worktree, no branch, no ledger entry), not by trusting its name.

**For the PR body**, in this shape:

```markdown
## Verification
- `./scripts/validate.sh`: pass (N tests)
- e2e evidence (`run.py evidence --groups B,E`): N pass / 0 fail / N skip · H skipped (no docker engine)
- Features driven (verify-treebox): create, teardown
- Changed behavior, exercised:
  - `treebox teardown ghost --json` → exit 3, `error.code: NOT_FOUND` (was exit 1)
```

When you report a sweep: verdict first ("no breaking changes found" or "N breaking changes", one line each); then the counts (the bundle's summary table, the bundle path, the `validate.sh` result); then each failure with the scenario id or feature bullet, the command, expected and actual, and one class (**product bug**, **doc drift**, **harness gap**, **environment**); then the skips and known bugs (what was not exercised and why). Do not fix a regression as part of a sweep: report it. If asked to fix it, re-run the affected scenarios afterwards (`--only`).

If anything fails and you cannot fix it, stop after a genuine attempt and put the failure verbatim in the PR body. A failed check reported honestly is useful; a hidden one is not.

## Cleanup

Tear down what you created, then the fixture:

```bash
treebox teardown e2e-a e2e-b --repo "$REPO" --root "$ROOT" --force --remove-volumes --json
e2e_cleanup
```

`e2e_cleanup` reaps `treebox-e2e-*` docker resources, removes `$SBX`, and takes `$SBX/bin` off `PATH`, so a later `treebox` is never a dead fixture's. Evidence you saved outside `$SBX` (and every evidence bundle) survives it. Never kill processes by name: stop the ones you started. Then check that nothing is left: `docker ps -a --filter name=treebox-e2e-`, `docker images 'treebox-e2e-*'`, `docker volume ls --filter name=treebox-e2e-` are empty, and `~/.treebox` is unchanged (it does not exist on a machine without a real treebox setup). The evidence run cleans up after itself and checks this as invariant X5.

## Helpers

| Helper | Invocation | What it does |
| --- | --- | --- |
| `fixture.sh` | `source .claude/skills/verify-treebox/fixture.sh` | Build a fresh fixture and put its `treebox` first on `PATH`. |
| `fixture.sh --attach` | `source .claude/skills/verify-treebox/fixture.sh --attach "$SBX"` | Use an existing fixture in a new shell. |
| `e2e_doctor` | `e2e_doctor` | The read-only health check (see Doctor). |
| `e2e_reap_docker` | `e2e_reap_docker` | Remove `treebox-e2e-*` containers, images and volumes; safe anytime, also after a crash. |
| `e2e_cleanup` | `e2e_cleanup` | Reap, remove `$SBX`, restore `PATH`. |
| `$SBX/bin/treebox` | `treebox …`; `E2E_PATH=… treebox …` | This checkout's CLI with the hermetic environment. Passes through `NO_COLOR`, `TERM`, `COLUMNS`, `TZ`, `TREEBOX_NON_INTERACTIVE`, `TREEBOX_TEMPLATE_DIR`, `TREEBOX_CONFIG`, `XDG_CACHE_HOME`, `DOCKER_HOST` and the completion variables; `E2E_PATH` replaces its `PATH` (exit 125 when a real agent is on it). |
| `run.py evidence` | `uv run python tests/e2e/run.py evidence --out DIR [--groups …] [--only …] [--require docker] [--inject-failure]` | The full scenario sweep and its bundle. |
| `run.py fixture` | `uv run python tests/e2e/run.py fixture` | What `fixture.sh` calls: builds a fixture, writes `$SBX/fixture.env`, prints the exports. |
| `run.py reap` | `uv run python tests/e2e/run.py reap` | What `e2e_reap_docker` calls. |

## When to run what

- Always: the deterministic gate, `./scripts/validate.sh` (ruff, mypy, shell assets, the full pytest suite including `tests/test_e2e_map.py`, golden snapshots, a live host smoke test). It needs no engine. If a golden snapshot changed because the task intentionally changes CLI output, update it with `scripts/golden-diff.sh --update` and say so in the PR; never update snapshots to make an unintended change pass.
- Then scope by feature: drive the feature files the change touches, and run their scenarios (`--only`, from the file's last Gotchas bullet), plus the cross-cutting invariants X1 to X5 (a full `--groups` run includes them).
- Shared code (`cli.py`, `provision.py`, `output.py`, `record.py`, the runners): the full sweep, `uv run python tests/e2e/run.py evidence --out …`, with `--require docker` where an engine exists.
- A route that is new or changed (a flag, an error code, a config key, an environment variable): `tests/test_e2e_map.py` fails until `references/routes.md` has its row (with its feature) and a scenario covers it.

To keep the feature map honest as treebox changes, run `/maintain-verify-treebox`.

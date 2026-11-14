---
name: maintain-verify-treebox
description: Periodic pass that keeps the verify-treebox skill and its feature map honest as the treebox CLI changes - the route table checked mechanically, one read-only source reader per feature file, a live drive of every feature in a fresh fixture, and at most one PR of proven corrections. Use for /maintain-verify-treebox, "audit the verify skill", "is the feature map still right", or after a release or a batch of user-facing changes.
disable-model-invocation: true
---

# Maintain verify-treebox

A feature map rots the moment treebox changes. This is the upkeep loop for `.claude/skills/verify-treebox/`. The unit of rigor is the feature, not every sentence: cover every feature file from source and drive every feature live, without terminalising every bullet.

## Outcomes

Pick one, and say which:

- **clean**: every feature got source and live coverage; nothing worth shipping. No branch, no PR.
- **changed**: one PR ships proven doc, harness or map corrections.
- **blocked**: coverage could not finish, or a proven fix could not ship safely. Say exactly what blocked it.

## Edit scope

Edit only `.claude/skills/verify-treebox/` (its `SKILL.md`, `fixture.sh`, `features/`, `references/`), this skill, `tests/e2e/` (the harness, the scenarios, the fakes, the offline template) and `tests/test_e2e_map.py`. Never edit product code (`src/`, `docs/`, `skills/treebox/`) during a pass: a behavior the map describes that treebox no longer does is either doc drift (fix the map) or a product regression (report it, do not paper over it in the map). A changed route keeps its scenario's checks as they are; until a product fix lands, mark the scenario `known_bug=` with a `known_bug_signature=` regex that matches only that failure, and remove both when it is fixed.

## Pass

0. **Locate the target.** The target is `.claude/skills/verify-treebox/` (`.agents/skills/verify-treebox/` is the same directory through a symlink). If it is missing or has no `features/README.md`, stop and report it: do not invent a new map.

1. **Index hygiene.** Run the drift test first; it finds route drift mechanically:

   ```bash
   uv run --extra dev python -m pytest tests/test_e2e_map.py -q
   ```

   It fails on: a route the source defines (a command, option, `--json` error code, config key, environment variable) with no row in `references/routes.md`, or a row for one that no longer exists; a source reference to a missing file or symbol; scenario ids that `references/scenarios.md` and `tests/e2e/scenarios.py` do not agree on, or that no row names; a row with no feature or with a feature that has no file; a feature file that `features/README.md` does not link or that no row names; a link in the map that does not resolve. Fix each failure. Then read `features/README.md` against `ls features/`: every file has one index bullet with a true one-line summary, and every file follows the entry contract (the H1, one paragraph, `Used with:`, the four H2s in order, the scenario ids as the last Gotchas bullet). Lightweight; no generated inventory.

2. **Source wave.** One read-only subagent per feature file, launched concurrently (13 today). Brief each with its feature file, `references/routes.md` (its rows are the feature's source entry points) and `features/README.md`. Each explains "how does this user-facing feature work?" from `src/treebox/`, `treebox <command> --help` and `docs/`, flags likely drift between the file and the source with citations (`path:line`), and returns one concise live recipe. Children never drive treebox, never build a fixture and never edit files. Return shape: feature summary / source entry points / likely drift or none / one recipe. If you cannot spawn subagents, do this reading yourself, per feature, and say so in the report.

3. **Reconcile.** Every feature file has a returned summary. Merge overlapping recipes into as few fixtures as practical. Spot-check cited drift in the source; do not re-prove clean claims. Sweep recent commits for user-facing surface the map misses: `git log --oneline -30 -- src/treebox docs skills/treebox` and the routes the drift test derived. Require a concrete source path (a Typer option, an emitted error code, a config key, a new behavior with a `path::symbol`) before you call one missing. A new route is one row in `routes.md` (with its feature) plus one scenario (prose in `scenarios.md`, checks with the same id in `tests/e2e/scenarios.py`; docker worktrees `e2e-…`), and a bullet in the feature file when a user drives it differently from what the file already shows. If it also changes human output, `tests/golden/` is updated separately: golden owns exact text, this map owns semantics.

4. **Live pass.** Required even when the source looks clean. You own all driving, by the verify-treebox Launch model: treebox is a short-lived CLI, so use a **fresh fixture per feature** (`source .claude/skills/verify-treebox/fixture.sh`, `--attach "$SBX"` in a new shell) and run each `Driving it with treebox` bullet as one command. Drive every feature at least once, and hold three invariants the whole pass, whatever the failure:
   1. Never drive a fixture you have not health-checked since it last did something surprising: `e2e_doctor` before the first drive of each fixture and again after any failed drive. Where `e2e_doctor` cannot see the failure (a worktree left half-made, a config left written), reset (`: > "$TREEBOX_CONFIG"`) or start a new fixture rather than hoping.
   2. Evidence captured so far survives every cleanup: save it outside `$SBX` (and keep evidence bundles outside the repo), and check it at its named location after `e2e_cleanup`, do not assume.
   3. Nothing a drive started outlives the drive: tear down its worktrees, stop its background `treebox` processes by pid, and run `e2e_cleanup`, also after a failed or stuck drive. On a shared engine, remove only your own `treebox-e2e-*` resources.

   An `e2e_doctor` failure caused by skill drift is drift: fix it under the edit scope and retry once (restart only what the fix invalidated) before you call the pass `blocked`. A bullet that cannot be reached is `verified-unreachable` only with the concrete prerequisite (uid 0, a reachable engine, macOS for the Keychain, a terminal for the chooser, an SSH worker) and the command attempted; if the feature file omits that prerequisite, that is drift. Any harness fix from triage is driven live again before it ships. Also run the scenario sweep once, `uv run python tests/e2e/run.py evidence --out "$TMPDIR/treebox-evidence-$(date +%s)"` (with `--require docker` where an engine exists), and compare its counts with the last known run. The final cleanup happens after the last drive, re-proofs included.

5. **Triage.** Exactly one class per difference:
   - **doc drift**: the behavior is coherent but the map, a feature file or the skill says otherwise. Fix it.
   - **harness gap**: treebox is right and its docs agree, but the fixture, the launcher, `run.py`, a scenario or the drift test cannot drive or check it. Fix it; a harness fix follows the helpers rule (scripts executable, invocation shown in the verify-treebox Helpers table) and gets a live re-drive.
   - **product gap**: treebox contradicts its documented contract (exit codes, `--json` schema, the invariants in `CLAUDE.md`). Record it with the command and the output for the user, and keep it out of this PR.
   - **environment**: a missing engine, image, login or network. Not treebox's fault; it makes the bullet SKIP.

6. **Ship or stop.** For **changed**: one PR of proven corrections. Re-read every changed file first, then run `./scripts/validate.sh` and `uv run --extra dev python -m pytest tests/test_e2e_map.py`, and, when `tests/e2e/` changed, the full sweep. For **clean** or **blocked**: no PR; report the outcome and the coverage honestly.

Keep short run notes (features covered, unreachable prerequisites, confirmed drift, product gaps, outcome) in a scratch location outside the repo; do not commit them.

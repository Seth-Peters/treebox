# Treebox end-to-end scenario matrix

Run every scenario against the sandbox fixture (`source fixture.sh`), never
against a real repo or the user's `~/.treebox`. `tb` is the fixture's wrapper
for `uv run treebox`; always pass `--repo "$REPO" --root "$ROOT"` on commands
that touch worktrees. Judge **behavior** (exit code, JSON fields, filesystem
effects, stream separation) — exact human-output strings are locked by
`tests/golden/` and are not this checklist's job.

Conventions used below:

- "exit N" — the command's exit status must be exactly N (`0` ok · `1` runtime ·
  `2` usage · `3` not-found · `4` auth/fetch · `5` conflict).
- "JSON on stdout" — stdout alone must parse (`python3 -m json.tool`), carry an
  integer `schemaVersion` (currently `1`), and diagnostics must all be on stderr.
- "JSON error" — in `--json` mode a failure prints one object on **stderr** with
  `error.code` / `error.message` (usually `error.hint`).
- Reset state between groups when noted; scenarios within a group may build on
  each other in order. **Watch for hidden preconditions**: a scenario that needs a
  *clean* worktree or *≥2 matching* worktrees will silently exercise the wrong
  branch if earlier scenarios changed that state (e.g. C3 dirties `fix-auth`
  before E1 wants it clean). Where a precondition matters it is called out inline;
  set it up rather than reusing leftover state.
- Assert JSON fields with a field extractor, not by eyeballing — `list --json` and
  `doctor --json` payloads are long and easy to truncate. Pipe to
  `python3 -c 'import sys,json; d=json.load(sys.stdin); ...'` and check the field.
- Run the sweep from a **bash** shell (the fixture is bash and must be `source`d);
  the session shell may be zsh, where unquoted globs like `/tmp/treebox-e2e.*`
  error on no-match instead of returning empty.
- **Docker naming marker**: every group-H (docker) scenario names its worktree with
  the `e2e-` prefix so the container/image become `treebox-e2e-*` and
  `e2e_cleanup`/`e2e_reap_docker` can sweep them. Host/git artifacts already live
  under `$SBX`, so groups A–G don't need the prefix (and name-shape scenarios like
  B3/B8 must not carry it).

## A. version & doctor

- [ ] **A1 version** — `tb version`, `tb --version`, `tb -V`
      Expect: exit 0 each; all three print the *same* version string on stdout,
      equal to `tb version`. (Don't compare against `project.version` in
      `pyproject.toml` — it is `dynamic` here, derived from git tags, so there is
      no literal to match; a `0.7.1.dev12`-style value is normal off a tag.)
- [ ] **A2 doctor human** — `tb doctor --repo "$REPO"`
      Expect: exit 0 when git + at least one harness login are healthy; check
      rows on stdout are readable off-TTY (no raw ANSI garbage when piped).
      Against a repo with no `.env`, the `.env` row is a muted `·` note ending
      in `· optional` (still showing the configured path), never a red `✗`,
      and the exit code stays 0.
- [ ] **A3 doctor json** — `tb doctor --repo "$REPO" --json`
      Expect: exit 0; JSON on stdout with `ok`, `isolation`, `checks[]`,
      `advisories`; `ok` true iff every hard check passed.
- [ ] **A4 doctor docker** — `tb doctor --repo "$REPO" --isolation docker --json`
      Expect: exit 0 with `ok: true` when a docker daemon is available — this
      result **gates phase H**; otherwise a docker hard-check failure and exit 1
      (then mark H skipped, not failed).
- [ ] **A5 bad isolation** — `tb doctor --isolation qemu`
      Expect: exit 2; message names the valid values.

## B. create

- [ ] **B1 named create** — `tb create fix-auth --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; JSON on stdout with `name: "fix-auth"`,
      `branch: "fix-auth"`, `base: "main"`, `created: true`, `worktree_path`
      under `$ROOT`, `entry_command` that `cd`s into the worktree and execs the
      harness. Filesystem: `$ROOT/fix-auth/.venv` exists; `$ROOT/fix-auth/.env`
      content equals the repo's `.env` (`SECRET=canonical`); branch of the
      worktree is `fix-auth`.
- [ ] **B2 nameless create (petname placeholder)** — `tb create --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; generated `name` (petname), `branch` = `treebox/<name>`;
      `tb list --json` shows it with `"unnamed": true`.
- [ ] **B3 slash name flattening** — `tb create feature/auth --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; `branch: "feature/auth"`; directory leaf is
      `feature--auth`; `name` is the flattened form.
- [ ] **B4 --base** — `tb create from-dev --base dev --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; `base: "dev"`; the new branch's head equals
      `origin/dev`'s head.
- [ ] **B5 --checkout existing branch** — `tb create review-dev --checkout dev --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; worktree is on branch `dev` (no new branch created).
- [ ] **B6 BRANCH_EXISTS conflict** — `tb create fix-auth --repo "$REPO" --root "$SBX/wts2" --json`
      Expect: exit 5; JSON error with a branch-exists conflict code; hint points
      at `--checkout` as the resume path; `$SBX/wts2/fix-auth` not created.
- [ ] **B7 SLUG_CONFLICT on re-create** — `tb create fix-auth --repo "$REPO" --root "$ROOT" --json` (again, same root)
      Expect: exit 5 (re-`create` of a fully provisioned name is never an
      idempotent enter); hint points at `enter`.
- [ ] **B8 invalid name** — `tb create "Bad Name" --repo "$REPO" --root "$ROOT"`
      Expect: exit 2; usage error explaining the slug rule.
- [ ] **B9 fetch required / --no-fetch escape** —
      `git -C "$REPO" remote set-url origin /nonexistent`, then
      `tb create off-line --repo "$REPO" --root "$ROOT"`
      Expect: exit 4, no worktree created, no silent fallback to stale refs.
      Then `tb create off-line --repo "$REPO" --root "$ROOT" --no-fetch --print`
      Expect: exit 0. Restore the URL afterwards:
      `git -C "$REPO" remote set-url origin "$SBX/origin.git"`.
- [ ] **B10 --dry-run changes nothing** —
      `tb create plan-x --repo "$REPO" --root "$ROOT" --dry-run` then with `--json`
      Expect: human mode: plan (worktree/branch/isolation + `$ git …` commands)
      on **stderr**, stdout empty. JSON mode: object on **stdout** with
      `"dry_run": true` and a `commands` array. Both: exit 0 and
      `$ROOT/plan-x` does not exist.
- [ ] **B10b --dry-run preflights like a real create** -
      `tb create fix-auth --repo "$REPO" --root "$SBX/wts2" --dry-run --json`
      (branch `fix-auth` exists from B1)
      Expect: exit 5; JSON error `BRANCH_EXISTS` on stderr, stdout empty;
      `$SBX/wts2/fix-auth` not created and `git -C "$REPO" worktree list`
      unchanged - a dry run fails with the same error a real create would,
      never printing a plan a real run refuses. Same parity for
      `SLUG_CONFLICT` (existing dir), `NOT_FOUND` (missing `--checkout`/base
      branch, exit 3), and `BRANCH_IN_USE` (`--checkout` of a checked-out
      branch).
- [ ] **B11 --print** — `tb create print-x --repo "$REPO" --root "$ROOT" --print`
      Expect: exit 0; stdout is exactly one runnable launch command that carries
      the worktree dir (self-contained — no reliance on cwd) and the harness
      binary; nothing else on stdout.
- [ ] **B12 --harness codex** — `tb create cdx --repo "$REPO" --root "$ROOT" --harness codex --print`
      Expect: exit 0; launch command execs `codex` (not `claude`). No codex
      login is needed for `--print`.
- [ ] **B13 pre-push guard** — in `$ROOT/fix-auth`:
      `git branch treebox/scratch && git push origin treebox/scratch`
      Expect: push rejected (non-zero), stderr mentions the placeholder branch;
      then `git push origin fix-auth` succeeds — the guard only blocks
      `treebox/*` refs.
- [ ] **B14 progress & color degradation** — any create with stderr piped, and
      once with `NO_COLOR=1` and once with `--quiet`
      Expect: piped/NO_COLOR stderr has no ANSI escape sequences or spinner
      frames; `--quiet` create produces no progress chatter; `--verbose`
      streams raw underlying command output.

## C. enter

Prereq: `fix-auth` worktree from B1 exists.

- [ ] **C1 re-launch --print** — `tb enter fix-auth --repo "$REPO" --root "$ROOT" --print`
      Expect: exit 0; same shape of launch command as B11; no agent launched.
- [ ] **C2 .env refresh** — change `$REPO/.env` to `SECRET=rotated`, run C1 again
      Expect: `$ROOT/fix-auth/.env` now reads `SECRET=rotated`.
- [ ] **C3 deps re-sync only when manifest changed** — in `$ROOT/fix-auth`:
      bump `version` in `pyproject.toml` and run `uv lock -q`
      Expect: `tb list --json` now shows `"deps": "stale"` for fix-auth;
      after `tb enter fix-auth … --print` it shows `"deps": "fresh"` again.
- [ ] **C4 ref resolution & rename survival** —
      `git -C "$ROOT/fix-auth" branch -m fix/auth-renamed`, then enter by the
      original name `fix-auth`, by the new branch `fix/auth-renamed`, and by a
      unique substring (e.g. `renamed`)
      Expect: all three resolve to the same worktree, exit 0 (name is
      permanent identity; branch is read live).
- [ ] **C5 ambiguous & missing refs** — precondition: at least two worktrees must
      share the substring you test (create e.g. `amb-one` and `amb-two` first —
      after C4, `fix` alone matches only `fix-auth` and would resolve, not conflict).
      Enter the shared substring (`amb`):
      Expect: exit 2 listing the candidates. Enter `ghost-x`:
      Expect: exit 3; with `--json`, a JSON error on stderr with
      `"code": "NOT_FOUND"`.
- [ ] **C6 agent args passthrough** — `tb enter fix-auth … --print -- "continue the refactor"`
      Expect: exit 0; the printed command carries the extra arg verbatim after
      the harness argv.
- [ ] **C7 recorded isolation is sticky** — `tb enter fix-auth … --isolation docker --print`
      (worktree was created with host isolation)
      Expect: exit 5 conflict — an explicit mismatched `--isolation` never
      silently re-provisions; the recorded choice wins over config drift too.

## D. list

- [ ] **D1 human table** — `tb list --repo "$REPO" --root "$ROOT"`
      Expect: exit 0; one row per worktree with name, live branch, isolation,
      deps freshness, `.env` presence; renamed branches (C4) show the new
      branch under the stable name.
- [ ] **D2 json shape** — `tb list --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; `worktrees[]` where each entry has `name`, `branch`,
      `unnamed`, `missing`, `path`, `base`, `isolation`, `harness`, `deps`
      (`fresh|stale`), `env`.
- [ ] **D3 empty root** — `tb list --repo "$REPO" --root "$SBX/empty" --json`
      Expect: exit 0; `"worktrees": []` (not an error).

## E. teardown

- [ ] **E1 non-TTY refusal** — precondition: a **clean** worktree (create a
      throwaway `e2e-clean-td`; do *not* reuse `fix-auth`, which C3 left dirty — a
      dirty tree takes the uncommitted-changes branch below and hides this path).
      `tb teardown e2e-clean-td --repo "$REPO" --root "$ROOT"` (no `--force`, stdin
      not a TTY)
      Expect: exit 5; worktree untouched; message says confirmation/`--force` is
      required for non-interactive teardown.
- [ ] **E2 dirty worktree needs --force** — `touch "$ROOT/fix-auth/junk.txt"`,
      teardown without `--force`
      Expect: exit 5 (dirty), worktree intact; then with `--force`: removed.
- [ ] **E3 force removes, branch survives** — after E2's forced removal
      Expect: exit 0; directory gone; `git -C "$REPO" worktree list` no longer
      mentions it; the branch (`fix/auth-renamed`) still exists — branches are
      kept by default.
- [ ] **E4 --delete-branch** — create a throwaway `del-me`, then
      `tb teardown del-me … --force --delete-branch --json`
      Expect: exit 0; JSON record of what was removed on stdout; local branch
      `del-me` gone.
- [ ] **E5 multiple refs** — create two throwaways, tear both down in one call
      Expect: exit 0; both directories removed; JSON lists both.
- [ ] **E6 teardown resolves original name after rename** — covered by tearing
      down `fix-auth` (E1–E3) *after* C4 renamed its branch: the original name
      must still resolve.
- [ ] **E7 corrupt worktree is not "dirty"** - create a throwaway `e2e-corrupt`,
      remove its pointer file (`rm "$ROOT/e2e-corrupt/.git"`), dirty the main
      checkout (`touch "$REPO/main-dirt.txt"`), then teardown without `--force`
      (`--json`)
      Expect: exit 5 with `NEEDS_CONFIRMATION`, *not* `DIRTY_WORKTREE` (the main
      checkout's dirt must not be attributed to the corrupt tree); then with
      `--force --json`: exit 0, directory gone, registration pruned from
      `git -C "$REPO" worktree list`, and `$REPO/main-dirt.txt` untouched
      (clean it up afterwards).

## F. template

Uses the scratch `$TREEBOX_HOME` — must never write to the real `~/.treebox`.

- [ ] **F1 list bundled default** — `tb template list` (and `--json`)
      Expect: exit 0; the bundled `default` appears with its source and
      required-file status, marked as the configured default.
- [ ] **F2 path resolution** — `tb template path` / `tb template path default`
      Expect: exit 0; prints an existing directory on stdout (scriptable:
      `cd "$(tb template path)"` works). `tb template path ghost`: exit 3.
- [ ] **F3 init scaffolds full set** — `tb template init mytpl --json`
      Expect: exit 0; `$TREEBOX_HOME/templates/mytpl/` contains the full
      required file set (`Dockerfile`, `container.json`, `post-create.sh`,
      firewall assets); JSON says `valid: true`, `missing: []`; `template list`
      now shows `mytpl`.
- [ ] **F4 init conflict & --force** — `tb template init mytpl` again
      Expect: exit 5 (existing template untouched). With `--force`: exit 0,
      re-scaffolded.
- [ ] **F5 --from** — `tb template init copytpl --from mytpl --json`
      Expect: exit 0; contents copied from `mytpl`, not the bundled default
      (verify by first marking `mytpl`'s Dockerfile with a comment).
- [ ] **F6 unknown --from** — `tb template init x --from ghost`
      Expect: loud not-found (exit 3), nothing scaffolded.

## G. config (user-level TOML)

Each writes `$TREEBOX_CONFIG`, runs, then restores the empty file (`: > "$TREEBOX_CONFIG"`).

- [ ] **G1 missing config file is loud** — point `TREEBOX_CONFIG` at a
      nonexistent path for one command
      Expect: exit 2 with a hint to fix or unset it (no silent defaulting).
- [ ] **G2 defaults apply** — write `base = "dev"`, then `tb create cfg-base … --json`
      Expect: `base: "dev"` without any `--base` flag.
- [ ] **G3 invalid values rejected** — write `isolation = "qemu"`
      Expect: any worktree command exits 2 naming the valid isolation values.
- [ ] **G4 explicit flag beats config** — with `base = "dev"` still set,
      `tb create cfg-override --base main … --json`
      Expect: `base: "main"`.

## H. docker isolation (run only if A4 passed; expensive — image build)

**Name every worktree here with the `e2e-` prefix** (see the docker naming marker
in the preamble): the container/image become `treebox-e2e-*`, so `e2e_cleanup` /
`e2e_reap_docker` remove them even when a scenario (H5's `--skip-container`) leaves
one behind on purpose.

- [ ] **H1 docker create** — `tb create e2e-dockered --repo "$REPO" --root "$ROOT" --isolation docker --print`
      Expect: exit 0; provisioning identical to host (worktree, `.env`,
      `.venv`); launch command targets the container (`docker exec …`), not a
      bare host shell.
- [ ] **H2 sandbox config outside the mount** — after H1
      Expect: the rendered container config/build context (and staged credential
      copies) live under `$ROOT/.containers/<branch>/`, **not** inside the worktree
      — the boxed agent must not be able to edit its own sandbox definition.
- [ ] **H3 unknown template is loud** — `tb create e2e-tpl-ghost … --isolation docker --template ghost`
      Expect: a loud non-zero exit (no fallback to the bundled default) whose
      message names the missing template. Note the exit code is `1` here (the
      failure surfaces mid-provision, after the worktree is added host-side), not
      the `3` that `template path ghost` / `template init --from ghost` return — an
      inconsistency worth flagging, but not itself a failure of this scenario.
      Because provisioning is host-side and completes `worktree add` before the
      docker step fails, `$ROOT/e2e-tpl-ghost/` is **left on disk** (a re-run would
      then hit SLUG_CONFLICT); the sandbox files under `.containers/` are not
      written. That leftover worktree is expected given the provision-then-run
      architecture — record it, don't fail on it. Note: under *host* isolation an
      unknown `--template` is currently accepted silently (templates are
      docker-only) — also expected.
- [ ] **H4 recorded isolation reused** — `tb enter e2e-dockered … --print` with no
      `--isolation` flag
      Expect: exit 0; still docker (recorded at create), even if the config
      default says host.
- [ ] **H5 docker teardown** — `tb teardown e2e-dockered … --force --json`; then
      `--remove-volumes` on a fresh `e2e-dockered2`; then `--skip-container` on a
      fresh `e2e-dockered3`
      Expect: exit 0 each; `--remove-volumes` reports `volumes_removed: true`;
      `--skip-container` reports `container: "skipped"` and leaves the container
      running (the `e2e-` name means `e2e_cleanup` still reaps it later); the
      worktree dir is always removed. Container/image cleanup is best-effort —
      `container: "failed"` is a warning, not a teardown failure.

## Cross-cutting invariants (assert continuously, fail the run on any hit)

- [ ] **X1 stream discipline** — in every `--json`/`--print` scenario stdout
      parsed cleanly with zero diagnostic lines mixed in.
- [ ] **X2 exit-code table** — no scenario produced a code outside its
      documented meaning (`0/1/2/3/4/5`).
- [ ] **X3 schemaVersion** — every JSON payload (success *and* error) carried
      `schemaVersion`; the number matches `tests/golden/` (currently `1`).
      If code and `tests/golden/` disagree with `docs/usage.md` or
      `skills/treebox/SKILL.md`, report **doc drift** as a finding.
- [ ] **X4 per-name lock** — if any command reported a lock conflict (exit 5)
      during sequential runs, that's a bug — the lock must be released on exit.
- [ ] **X5 no host pollution** — after `e2e_cleanup`: nothing new under the real
      `~/.treebox`, no stray `treebox-e2e.*` dirs left in `$TMPDIR`,
      `git -C <treebox repo> status` unchanged by the run, and — if group H ran —
      **no leftover docker artifacts**: `docker ps -a --filter name=treebox-e2e-`
      and `docker images 'treebox-e2e-*'` both empty. `e2e_cleanup` reaps these via
      `e2e_reap_docker`; if a run crashed before cleanup, call `e2e_reap_docker`
      directly (it only removes `treebox-e2e-*`, never real worktrees). This check
      only works because every group-H worktree carries the `e2e-` name prefix.

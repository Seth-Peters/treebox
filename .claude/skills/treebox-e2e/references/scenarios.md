# Treebox end-to-end scenario matrix

Run every scenario against the sandbox fixture (`source fixture.sh`), never
against a real repo or the user's `~/.treebox`. `tb` is the fixture's
treebox, with a scratch `HOME` and the stub agent first on `PATH`; always pass
`--repo "$REPO" --root "$ROOT"` on commands that touch worktrees. Judge **behavior** (exit code, JSON fields, filesystem
effects, stream separation) — exact human-output strings are locked by
`tests/golden/` and are not this checklist's job.

Every scenario here has an executable twin with the same id in
`tests/e2e/scenarios.py`; `uv run python tests/e2e/run.py evidence --out DIR`
runs them all and writes the evidence bundle (see `SKILL.md`). This file is the
prose: the command, the intent, the pass criterion. The code is the checks.
Where a route lives in the source, and which scenario covers it, is in
`feature-map.md`.

Conventions used below:

- "exit N" — the command's exit status must be exactly N (`0` ok · `1` runtime ·
  `2` usage · `3` not-found · `4` auth/fetch · `5` conflict).
- "JSON on stdout" — stdout alone must parse (`python3 -m json.tool`), carry an
  integer `schemaVersion` (currently `1`), and diagnostics must all be on stderr.
- "JSON error" — in `--json` mode a failure prints one object on **stderr** with
  `error.code` / `error.message` (usually `error.hint`).
- Each scenario starts on a fresh fixture with the setup under its group
  heading. Create all additional state within that scenario. Selecting an id
  with `--only` must produce the same result as selecting the whole group.
  Start a new fixture for each scenario in a manual sweep too.
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
      Expect: exit 0 when git, the selected harness binary (`agent: claude`) and
      its login (`login: claude`: a real login file, or the macOS Keychain on
      host) are healthy; exit 1 otherwise (other harnesses' logins are
      optional rows). Check
      rows on stdout are readable off-TTY (no raw ANSI garbage when piped).
      Against a repo with no `.env`, the `.env` row is a muted `·` note ending
      in `· optional` (still showing the configured path), never a red `✗`,
      and the exit code stays 0.
- [ ] **A3 doctor json** — `tb doctor --repo "$REPO" --json`
      Expect: exit 0; JSON on stdout with `ok`, `isolation`, `checks[]`,
      `advisories`; `ok` true iff every hard check passed.
- [ ] **A4 doctor docker** — `tb doctor --repo "$REPO" --isolation docker --json`
      Expect: exit 0 with `ok: true` when the
      [Docker requirements](../../../../docs/install.md#what-each-isolation-mode-needs)
      are met. This result **gates phase H**. If the Docker check fails, expect
      exit 1 and mark H skipped, not failed.
- [ ] **A5 bad isolation** — `tb doctor --isolation qemu`
      Expect: exit 2; message names the valid values.

- [ ] **A6 completion protocol** - `_TREEBOX_COMPLETE=complete_bash COMP_WORDS="treebox cre" COMP_CWORD=1 tb`
      (what the installed bash completion calls), then the same for
      `COMP_WORDS="treebox template " COMP_CWORD=2`
      Expect: exit 0; completions `create`, then exactly `init list path`
      (the hidden `ls` alias is not offered).
- [ ] **A7 --show-completion / --install-completion** - run each from a real
      `bash -c` parent (they detect the parent shell)
      Expect: exit 0; the shown script wires `_TREEBOX_COMPLETE=complete_bash`;
      install writes into the scratch `~/.bashrc`. SKIP where the shell cannot
      be detected (some sandboxes hide the process table).
- [ ] **A8 help** - `tb --help`, bare `tb`, `tb create --help`, `tb template init -h`
      Expect: `--help` exits 0 and lists every visible command but not the
      `ls`/`rm` aliases; bare `tb` exits 2 with usage; subcommand help exits 0.
- [ ] **A9 doctor --harness** - `tb doctor -H codex --json`; again with a `PATH`
      that has the stub `claude` but no `codex`; again after
      `rm "$HOME/.codex/auth.json"`; then plain `tb doctor --json`
      Expect: exit 0 with `agent: codex` and `login: codex` rows ok; exit 1
      with the `agent: codex` row failed; exit 1 with the `login: codex` row
      failed; plain doctor (claude) exits 0 (the other harness's login is
      optional).
- [ ] **A10 macOS Keychain login** (macOS; boundary: a fake `security` first on
      `PATH`, never the real Keychain) - `rm "$HOME/.claude/.credentials.json"`;
      `tb doctor --json` with a `security` that prints a JSON login, then with
      one that exits 44 (item not found)
      Expect: exit 0, the `login: claude` row ok and its detail names the
      Keychain, and treebox asked for the `Claude Code-credentials` item; then
      exit 1 with the row failed. SKIP on other platforms.

## B. create

- [ ] **B1 named create** — `tb create fix-auth --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; JSON on stdout with `name: "fix-auth"`,
      `branch: "fix-auth"`, `base: "main"`, `created: true`, `worktree_path`
      under `$ROOT`, `entry_command` that `cd`s into the worktree and execs the
      harness, `id` (10 lowercase hex), `port_base: 20000` and `port_count: 10`
      (the first block of the default range), `warnings: []`; `list --json`
      shows the same `id` and `port_base`. Filesystem: `$ROOT/fix-auth/.venv`
      exists; `$ROOT/fix-auth/.env` content equals the repo's `.env`
      (`SECRET=canonical`); branch of the worktree is `fix-auth`.
- [ ] **B2 nameless create (petname placeholder)** — `tb create --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; generated `name` (petname), `branch` = `treebox/<name>`;
      `tb list --json` shows it with `"unnamed": true`.
- [ ] **B3 slash name flattening** — `tb create feature/auth --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; `branch: "feature/auth"`; directory leaf is
      `feature--auth`; `name` is the flattened form. Then
      `tb create --checkout treebox/a/b … --json` (a placeholder-shaped
      branch): `name: "a--b"` (the `treebox/` prefix drops, the rest
      flattens), and no nested `treebox/` directory exists.
- [ ] **B4 --base** — `tb create from-dev --base dev --repo "$REPO" --root "$ROOT" --json`
      (first push a commit to `dev`, so `origin/dev` differs from `main`)
      Expect: exit 0; `base: "dev"`; the new branch's head equals
      `origin/dev`'s head.
- [ ] **B5 --checkout existing branch** — `tb create review-dev --checkout dev --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; worktree is on branch `dev` (no new branch created).

First create `fix-auth` within B6, B7, B10b, B13 and B17. Each scenario
owns this setup; none needs B1 to run first.

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
      (first create the `fix-auth` worktree)
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

- [ ] **B15 real launch (host)** - `tb create launch-host --repo "$REPO" --root "$ROOT"`
      (no `--print`: this launches the agent, which in the fixture is the stub)
      Expect: exit 0; the stub's record on stdout shows `name=claude`, `cwd` =
      the worktree, args exactly `--dangerously-skip-permissions`,
      `in_container=no`, `api_key=unset`, and `TREEBOX_NAME=launch-host`,
      `TREEBOX_ID` / `TREEBOX_PORT_BASE` equal to `list --json`'s `id` /
      `port_base`, `TREEBOX_PORT_COUNT=10`.
- [ ] **B16 launch without the agent binary** - B15's command with the stub dir
      removed from `PATH` (and no other `claude` reachable)
      Expect: exit 1 (`HARNESS_MISSING`); stderr names `'claude' not found on
      PATH`; no traceback; nothing provisioned (no worktree, no branch): the
      agent is checked before provisioning.
- [ ] **B17 BRANCH_IN_USE** - `tb create dup --checkout fix-auth … --json` (fix-auth is checked out)
      Expect: exit 5, `BRANCH_IN_USE`; nothing created.
- [ ] **B18 INVALID_BRANCH** - `tb create --checkout bad..ref … --json`
      Expect: exit 2, `INVALID_BRANCH`.
- [ ] **B19 NOT_A_REPO** - `tb create x --repo "$SBX/not-a-repo" --json` (a plain dir)
      Expect: exit 2, `NOT_A_REPO`; the hint names `--repo`.
- [ ] **B20 GIT_MISSING** - `tb list … --json` with a `PATH` that has no `git`
      Expect: exit 1, `GIT_MISSING`.
- [ ] **B21 TEMPLATE_INVALID** - a template `broken` whose `container.json` is
      malformed; `tb create e2e-broken … --isolation docker --template broken --json`
      Expect: exit 1, `TEMPLATE_INVALID`, before anything is provisioned: no
      worktree, no branch. Needs no docker engine.
- [ ] **B22 MISSING_DEPENDENCY** - `tb create e2e-nodocker … --isolation docker --json`
      with a `PATH` that has no `docker`
      Expect: exit 1, `MISSING_DEPENDENCY`; no worktree (preflight runs first).
- [ ] **B23 DOCKER_UNAVAILABLE** - the same with
      `DOCKER_HOST=unix://$SBX/no-daemon.sock` (a real CLI, no daemon); also
      `tb doctor --isolation docker`
      Expect: create exits 1, `DOCKER_UNAVAILABLE`, no worktree; doctor exits 1.
      SKIP without a docker CLI.
- [ ] **B24 --cold** - `tb create cold-x … --cold --print`
      Expect: exit 0; stderr reports the setup as `from-source (cold)`; `.venv`
      exists; no `treebox-cold-*` dir is left in `$TMPDIR`.
- [ ] **B25 LOGIN_MISSING** - `rm "$HOME/.claude/.credentials.json"` (the
      fixture's `security` finds no Keychain item), then
      `tb --non-interactive create b25 …` and
      `TREEBOX_NON_INTERACTIVE=1 tb create b25 …` (launches)
      Expect: exit 1 each; stderr says `No claude login for an unattended
      launch`; the agent never starts; no worktree, no branch.
- [ ] **B26 resource flags without an engine** - `tb create b26 … --memory 4g
      --json` (host isolation); `tb create e2e-b26 … --isolation docker
      --memory lots --json`; `… --isolation docker --cpus 1.5 --memory 512m
      --pids-limit 64 --dry-run --json`
      Expect: exit 2 `RESOURCE_UNSUPPORTED`; exit 2 `INVALID_RESOURCE`; neither
      leaves a worktree or branch; the plan has `resources: {cpus: 1.5,
      memory: "512m", pids: 64}` and its `docker run` carries `--cpus 1.5
      --memory 512m --pids-limit 64`.
- [ ] **B27 INVALID_ROOT** - a plain file `$SBX/afile`; `create`, `enter`,
      `list` and `teardown` with `--root "$SBX/afile"` and with
      `--root "$SBX/afile/below"` (`--json`)
      Expect: exit 2 `INVALID_ROOT` each; the real worktree is untouched.
- [ ] **B28 the branch's own .gitmodules** - add submodule `sub` on `main`;
      branch `b28-extra` adds a `ghost` entry to `.gitmodules`; branch
      `b28-nosub` drops `.gitmodules` and `sub`. `tb create b28 … --dry-run
      --json`, `tb create --checkout b28-extra … --dry-run --json`, then the
      real `--print` and `tb create --checkout b28-nosub … --json`
      Expect: the `main` plan has `# copy submodule sub …`; the `b28-extra` plan
      has `# submodules: skipped ghost …`, and the real run notes `skipped
      ghost` on stderr, copies `sub`, and leaves `git status` clean (the
      branch's `.gitmodules` is never written); `b28-nosub` gets no `sub` and
      is clean too.
- [ ] **B29 RUNNING_AS_ROOT** (root only) - as uid 0,
      `tb create e2e-root … --isolation docker --json`
      Expect: exit 1 `RUNNING_AS_ROOT`; nothing provisioned. SKIP when the run
      is not root (the Mac and the CI runners are not).

## C. enter

Setup: `tb create fix-auth … --json` (as B1).

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
      Expect: all three resolve to the same worktree, exit 0.
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

- [ ] **C8 real launch on enter** - `tb enter fix-auth … -- --stub-exit=7 "continue the refactor"`
      Expect: exit 7 (enter exits with the agent's status); the last stderr
      line is `treebox: agent exited with status 7`; the stub saw
      `--dangerously-skip-permissions --stub-exit=7 'continue the refactor'` in
      the worktree dir.
- [ ] **C9 RECORD_INVALID** - create `c9-corrupt`, set `"isolation": "qemu"` in
      its record `$ROOT/.records/c9-corrupt.json`, then
      `tb enter c9-corrupt … --json`
      Expect: exit 5, `RECORD_INVALID`, `error.path` is the record; `list
      --json` shows `health: "unreadable_state"` and isolation `""`; a plain
      `tb teardown c9-corrupt … --force --json` exits 0 and removes the
      worktree and its record.
- [ ] **C10 recorded harness** - `tb create c10-codex … --harness codex`, then
      `tb enter c10-codex … --print`, with `--harness claude`, and once more without
      Expect: codex, then claude (a per-session override), then codex again.
- [ ] **C11 enter --cold** - change `c11-cold`'s manifest (`version`, `uv lock`),
      then `tb enter c11-cold … --cold --print`
      Expect: exit 0; the re-sync reports `from-source (cold)`; deps `fresh`.
- [ ] **C12 enter --quiet / --verbose** - `tb enter fix-auth … --quiet --print`;
      change the manifest, then `--verbose --print`
      Expect: `--quiet` leaves stderr empty; `--verbose` streams uv's own output
      (`Resolved …`) during the re-sync.
- [ ] **C13 policy lives in the host-owned record** - write a state file into
      the sandbox-writable git dir
      (`$(git -C "$ROOT/fix-auth" rev-parse --absolute-git-dir)/treebox-state.json`)
      with `{"isolation": "docker", "template": "/etc", "provisioned": true}`,
      then `tb enter fix-auth … --print`
      Expect: exit 0, still the host launch command; `tb list --json` still
      reports `host`, as does `$ROOT/.records/fix-auth.json`.
- [ ] **C14 failed setup is a failed create; enter retries** - with
      `setup_hook = ["exit 7"]` in `$TREEBOX_CONFIG`, `tb create c14 … --json`
      Expect: exit 1, `SETUP_FAILED`, the message names `setup · hook 1` and
      status 7, `error.path` is `$ROOT/.logs/c14.setup.log` (it exists); the
      worktree is kept; `list --json` shows `setup: "failed"`,
      `deps: "unknown"`. `tb enter c14 … --json` runs the hook again (exit 1,
      `SETUP_FAILED`); after fixing the hook it exits 0 and `list` shows
      `setup: "done"`, `deps: "fresh"`.
- [ ] **C15 INVALID_LINKAGE** - create `c15`, `rm "$ROOT/c15/.git"`, then
      `tb enter c15 … --json`
      Expect: exit 5, `INVALID_LINKAGE`; `list --json` shows
      `health: "invalid_linkage"`, `missing: true`; `tb teardown c15 … --force`
      removes the directory and the registration.
- [ ] **C16 RECORD_MISSING: recreate** - `tb create e2e-norec … --print`, then
      `rm "$ROOT/.records/e2e-norec.json"` and `tb enter e2e-norec … --json`
      Expect: exit 5, `RECORD_MISSING`, hint names `treebox teardown e2e-norec`
      and `treebox create`; nothing is emitted; `list` shows isolation `""`
      and `health: "unreadable_state"`. `tb teardown e2e-norec … --force
      --json` (no extra flag): exit 0, the worktree is gone.
- [ ] **C17 RECORD_CONFLICT** - with `setup_hook = ["exit 7"]`,
      `tb create c17 … --harness codex --json` (exit 1, `SETUP_FAILED`); with
      an empty config, `tb create c17 … --harness claude --json`, then the same
      with `--harness codex`
      Expect: the resumed create with other settings exits 5
      `RECORD_CONFLICT` and its hint gives the recorded ones (`-H codex`); the
      one with the recorded settings exits 0 and finishes setup
      (`setup: "done"`).
- [ ] **C18 absolute path refs** - `tb enter "$ROOT/fix-auth" … --json`; `tb
      enter "$ROOT/fix" … --json` and `tb teardown "$ROOT/fix" … --force
      --json` (a path no worktree is registered at, a substring of one); `tb
      teardown "$ROOT/fix-auth" … --force --json`
      Expect: exit 0 with that `worktree_path`; exit 3 `NOT_FOUND` both (a path
      never falls back to a substring match), `fix-auth` intact; exit 0 and it
      is removed.

## D. list

Setup: `d-one`, `d-two` (its branch renamed to `feature/renamed`), and `d-raw`,
a registration treebox never provisioned (`git -C "$REPO" worktree add -b d-raw
"$ROOT/d-raw" main`).

- [ ] **D1 human table** — `tb list --repo "$REPO" --root "$ROOT"`
      Expect: exit 0; one row per worktree with name, live branch, isolation,
      deps freshness, `.env` presence, plus one worktree-root footer; renamed
      branches (C4) show the new branch under the stable name. A registration
      without readable Treebox state shows isolation as `unknown`, never the
      current config default.
- [ ] **D2 json shape** — `tb list --repo "$REPO" --root "$ROOT" --json`
      Expect: exit 0; `worktrees[]` where each entry has `name`, `branch`,
      `unnamed`, `missing`, `path`, `base`, `isolation`, `harness`, `deps`
      (`fresh|stale`), `env`, `health`, `setup`, `id`, `port_base`,
      `port_count`, `resources`. `d-one`: `health: "healthy"`, `setup:
      "done"`; `d-raw` (no record): isolation `""`, `health:
      "unreadable_state"`, `setup: ""`, `port_base` and `resources` `null`.
- [ ] **D3 empty root** — `tb list --repo "$REPO" --root "$SBX/empty" --json`
      Expect: exit 0; `"worktrees": []` (not an error).

- [ ] **D4 ls alias** - `tb ls … --json`
      Expect: exit 0; the same payload as `list --json`.

## E. teardown

Setup: `e2e-clean-td` (clean), and `fix-auth` with its branch renamed to
`fix/auth-renamed`.

- [ ] **E1 non-TTY refusal** - precondition: a **clean** worktree. Use the
      fresh `e2e-clean-td` from this scenario's group setup. A dirty tree takes
      the uncommitted-changes path instead.
      `tb teardown e2e-clean-td --repo "$REPO" --root "$ROOT"` (no `--force`, stdin
      not a TTY)
      Expect: exit 5 (`NEEDS_CONFIRMATION` with `--json`); worktree
      untouched; message says `--yes` (or `--force`) is required for
      non-interactive teardown. With a dirty `fix-auth`, `--yes --json` is
      still exit 5 `DIRTY_WORKTREE` (it confirms only). Then `-y`: exit 0,
      `e2e-clean-td` removed.
- [ ] **E2 dirty worktree needs --force** — `touch "$ROOT/fix-auth/junk.txt"`,
      teardown without `--force`
      Expect: exit 5 (dirty), worktree intact; then with `--force`: removed.
- [ ] **E3 force removes, branch survives** - force-remove this scenario's
      `fix-auth` worktree with `tb teardown fix-auth … --force --json`
      Expect: exit 0; directory gone; `git -C "$REPO" worktree list` no longer
      mentions it; the branch (`fix/auth-renamed`) still exists — branches are
      kept by default.
- [ ] **E4 --delete-branch** — create a throwaway `del-me`, then
      `tb teardown del-me … --force --delete-branch --json`
      Expect: exit 0; JSON record of what was removed on stdout; local branch
      `del-me` gone.
- [ ] **E5 multiple refs** — create two throwaways, tear both down in one call
      Expect: exit 0; both directories removed; JSON lists both.
- [ ] **E6 teardown resolves original name after rename** - create `e6-name`,
      rename its branch to `e6/renamed`, then tear down `e6-name`.
      Expect: exit 0; the original name resolves and the record names `e6/renamed`.
- [ ] **E7 corrupt worktree is not "dirty"** - create a throwaway `e2e-corrupt`,
      remove its pointer file (`rm "$ROOT/e2e-corrupt/.git"`), dirty the main
      checkout (`touch "$REPO/main-dirt.txt"`), then teardown without `--force`
      (`--json`)
      Expect: exit 5 with `NEEDS_CONFIRMATION`, *not* `DIRTY_WORKTREE` (the main
      checkout's dirt must not be attributed to the corrupt tree); then with
      `--force --json`: exit 0, directory gone, registration pruned from
      `git -C "$REPO" worktree list`, and `$REPO/main-dirt.txt` untouched
      (clean it up afterwards).

- [ ] **E8 rm alias** - `tb rm rm-me … --force --json`
      Expect: exit 0; removed, as with `teardown`.
- [ ] **E9 MAIN_WORKTREE** - `git -C "$REPO" branch repo`, then
      `tb teardown repo --repo "$REPO" --root "$SBX" --force --json` (a root that
      contains the repo, a branch named like its directory)
      Expect: exit 5, `MAIN_WORKTREE`; the main checkout intact.
- [ ] **E10 WORKTREE_LOCKED** - `git -C "$REPO" worktree lock "$ROOT/e2e-clean-td"`,
      then `tb teardown e2e-clean-td … --force --json`
      Expect: exit 5 `WORKTREE_LOCKED` (also with `--force`), the hint says
      `worktree unlock`; nothing removed. After the unlock the same teardown
      exits 0.
- [ ] **E11 teardown --quiet / --verbose** - `--force --quiet` on one throwaway,
      `--force --verbose` on another
      Expect: exit 0; `--quiet` leaves stderr empty; `--verbose` reports the removal.

## F. template

Uses the scratch `$TREEBOX_HOME` - must never write to the real `~/.treebox`.
F4, F5 and F8 each create their own `mytpl` before the check. F9 creates its
own `copytpl` before testing permissions.

- [ ] **F1 list bundled default** — `tb template list` (and `--json`)
      Expect: exit 0; the bundled `default` appears with its source and
      required-file status, marked as the configured default.
- [ ] **F2 path resolution** — `tb template path` / `tb template path default`
      Expect: exit 0; prints an existing directory on stdout (scriptable:
      `cd "$(tb template path)"` works). `tb template path ghost`: exit 3.
      `tb template path /etc` and `tb template path ../x`: exit 2 (a template
      name is an identifier, never a path).
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

- [ ] **F7 template ls alias** - `tb template ls --json`
      Expect: exit 0; the same payload as `template list --json`.
- [ ] **F8 bad names and self-copy** - `tb template init ../escape --json`;
      `tb template init mytpl --from mytpl --force --json`
      Expect: exit 2 `INVALID_NAME`; exit 2 `TEMPLATE_CONFLICT`, `mytpl` intact.
- [ ] **F9 init --force is all-or-nothing** (non-root) - `chmod 555` an empty
      templates dir, `tb template init denied --json`; restore it, `tb template
      init copytpl`, mark it, `chmod 555` the templates dir again and `tb
      template init copytpl --force --json`; restore it, put a read-only subdir
      in `copytpl`, `tb template init copytpl --force`
      Expect: exit 1 `COPY_FAILED`; exit 1 `OVERWRITE_FAILED` and the old
      template is byte-for-byte unchanged; exit 0 with a warning on stderr that
      the previous copy could not be removed, and `copytpl` is the complete new
      template (`valid: true`, no marker). SKIP as root.

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

- [ ] **G5 harness** - `harness = "codex"`, `tb create g5 … --print`; reset the
      config; `tb enter g5 … --print`
      Expect: codex both times (the recorded harness outlives the config).
- [ ] **G6 root** - `root = "trees"`, `tb create g6 --repo "$REPO" --json` (no `--root`)
      Expect: `worktree_path` is `$REPO/trees/g6` (repo-relative).
- [ ] **G7 env_file** - `env_file = "<abs path to $SBX/secrets.env>"`, `tb create g7 … --json`
      Expect: the worktree's `.env` has that file's content.
- [ ] **G8 setup_hook** - `setup_hook = ["echo hook-ran > hook.txt"]`, `tb create g8 … --json`
      Expect: `hook.txt` in the worktree reads `hook-ran`; no `.venv` (the hook
      replaces auto-detected setup).
- [ ] **G9 caches / XDG_CACHE_HOME** - `[caches] uv = "$SBX/uv-cache"`, create
      `g9`; then, with an empty config and `XDG_CACHE_HOME=$SBX/xdg`, create `g9b`
      Expect: uv wrote into `$SBX/uv-cache`; `$SBX/xdg/uv` exists.
- [ ] **G10 firewall** - `firewall = true`: `tb create e2e-fw … --isolation docker --dry-run --json`,
      again with `--no-firewall`; empty config with `--firewall`
      Expect: the plans with the firewall carry `--cap-add=NET_ADMIN` and the
      `init-firewall.sh` exec; `--no-firewall` beats the config. Needs no engine.
- [ ] **G11 template** - `template = "ghost"`, `tb create e2e-g11 … --isolation docker --json`
      Expect: exit 3 `TEMPLATE_NOT_FOUND`; nothing provisioned.
- [ ] **G12 closed vocabulary** - `isolatoin = "host"`, then `firewall = 1`; `tb list … --json`
      Expect: exit 2 `INVALID_CONFIG` each; messages say `did you mean
      'isolation'` and `boolean`.
- [ ] **G13 TREEBOX_TEMPLATE_DIR / TZ** - `TREEBOX_TEMPLATE_DIR=<dir> tb template list --json`
      and `tb template path any-name`; `TZ=Europe/Helsinki tb create e2e-tz … --isolation docker --dry-run --json`
      Expect: `templateDirOverride` is the dir and wins for any name; the plan's
      build carries `--build-arg TZ=Europe/Helsinki`. (`TREEBOX_HOME` is F3,
      `TREEBOX_CONFIG` is G1.)
- [ ] **G14 [ports]** - `[ports] start = 30000, end = 30019, block_size = 10`;
      create `g14-a`, `g14-b`, then `g14-c`; tear down `g14-a`; create `g14-c`
      again; `tb enter g14-b … --json`
      Expect: `port_base` 30000 and 30010; `g14-c` exits 5 `PORTS_EXHAUSTED`
      with `error.path` = `$TREEBOX_HOME/ports.json` and leaves no worktree or
      branch; after the teardown `g14-c` gets 30000; the enter keeps 30010.
- [ ] **G15 PORT_LEDGER_INVALID** - write `not json` to `$TREEBOX_HOME/ports.json`,
      `tb create g15 … --json`
      Expect: exit 1 `PORT_LEDGER_INVALID`, `error.path` is the ledger, the
      ledger is byte-for-byte unchanged, no worktree.
- [ ] **G16 [resources]** - `[resources] memory = "2g", pids = 128`;
      `tb create e2e-g16 … --isolation docker --dry-run --json`, again with
      `--memory 1g`; `tb create g16-host … --json` (host); then
      `memory = "lots"` and `tb list … --json`
      Expect: the plan's `resources` is `{cpus: null, memory: "2g", pids: 128}`
      and its `docker run` carries `--memory 2g --pids-limit 128`; the flag
      wins (`1g`); the host create exits 0 and records no limits (config
      limits never refuse a host worktree); the bad value is exit 2
      `INVALID_CONFIG`.

## H. docker isolation (requires a reachable engine and the base image)

Setup: install the offline template (`tests/e2e/offline-template`) as
`$TREEBOX_HOME/templates/e2e-offline` (the fixture does this; by hand:
`cp -RL tests/e2e/offline-template "$TREEBOX_HOME/templates/e2e-offline"`), and
pass `--isolation docker --template e2e-offline` (`…` below). The template
builds with no network once its pinned base image is present, and its agent is
the stub, so no scenario here needs the bundled 5.6 GB image or launches a real
agent. Without a reachable engine or the base image, every H scenario is a
SKIP, never a pass.

**Name every worktree here with the `e2e-` prefix** (see the docker naming marker
in the preamble): the container/image become `treebox-e2e-*`, so `e2e_cleanup` /
`e2e_reap_docker` remove them even when a scenario (H6's `--skip-container`) leaves
one behind on purpose.

- [ ] **H1 docker create** - `tb create e2e-dockered --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --print`
      Expect: exit 0; host-side provisioning identical to host isolation
      (worktree, `.env`, branch); the container `treebox-e2e-dockered-*` is
      running and the template's post-create step ran in it as the host uid in
      the worktree dir (`docker exec <ctr> cat /tmp/treebox-e2e-post-create`);
      launch command targets the container (`docker exec …`), not a bare host
      shell. (Dependency setup in a docker sandbox is the template's
      post-create; the offline template does not run `uv sync`.)

H2, H4, H5 and H6 each create `e2e-dockered` before their checks. H8 and H11
each create `e2e-launch`. These scenarios need no earlier H scenario.

- [ ] **H2 sandbox config outside the mount** - create `e2e-dockered`
      Expect: the rendered container config/build context (and staged credential
      copies) live under `$ROOT/.containers/<branch>/`, **not** inside the worktree
      — the boxed agent must not be able to edit its own sandbox definition.
- [ ] **H3 unknown template is loud** - `tb create e2e-tpl-ghost … --isolation docker --template ghost --json`, twice
      Expect: exit 3 `TEMPLATE_NOT_FOUND` both times (no fallback to the
      bundled default), the message names the template; the template is
      resolved before provisioning, so no worktree, branch, sandbox files or
      container is left and the second run gets the same error, never
      `SLUG_CONFLICT`. (Under *host* isolation an unknown `--template` is
      accepted: templates are docker-only.)
- [ ] **H4 recorded isolation reused** — `tb enter e2e-dockered … --print` with no
      `--isolation` flag
      Expect: exit 0; still docker (recorded at create), even if the config
      default says host.
- [ ] **H5 stopped container revived on enter** — `docker stop` the
      `treebox-e2e-dockered-*` container, then `tb enter e2e-dockered … --print`
      Expect: exit 0; the container is running again (`docker ps`) *before* the
      command is printed, so the emitted `docker exec …` replays cleanly — a
      stopped sandbox never yields a dead entry command: run the printed command
      and the stub answers from inside the container. (The firewall on every
      entry is H14; the real egress lockdown after a restart is H18.)
- [ ] **H6 docker teardown** — `tb teardown e2e-dockered … --force --json`; then
      `--remove-volumes` on a fresh `e2e-dockered2`; then `--skip-container` on a
      fresh `e2e-dockered3`
      Expect: exit 0 each; `--remove-volumes` reports `volumes_removed: true`;
      `--skip-container` reports `container: "skipped"` and leaves the container
      running (the `e2e-` name means `e2e_cleanup` still reaps it later); the
      worktree dir is removed. (A cleanup step that fails is H10.)

- [ ] **H7 real launch in the container** - `tb create e2e-launch …` (no
      `--print`), then `tb enter e2e-launch … -- --stub-exit=3`
      Expect: create exits 0 and the stub's record says `in_container=yes`,
      `cwd` = the worktree (mounted at its host path), `uid` = the host uid,
      `api_key=unset`; enter exits 3 with the stub's args.
- [ ] **H8 in-container git** - `docker exec -u agent -w <worktree> <ctr> git branch --show-current`
      and `git status`; `touch <repo>/.git/hooks/planted` in the container
      Expect: branch `e2e-launch`, status exits 0; the touch fails (the shared
      hooks are read-only) and nothing appears on the host.
- [ ] **H9 missing container** - create `e2e-gone`, `docker rm -f` its container,
      `tb enter e2e-gone … --json`
      Expect: exit 1, `ERROR`, message `No container exists …` (never a dead
      `docker exec` command).
- [ ] **H10 incomplete teardown** (boundary) - put `tests/e2e/fakes` first on
      `PATH` with `TREEBOX_E2E_DOCKER_FAIL=rm` and `TREEBOX_E2E_REAL_DOCKER`
      set. Create `e2e-gone`, remove its container by hand, and tear it down
      through the fake. Then create `e2e-fail-rm` and
      `tb teardown e2e-fail-rm … --force --json` through the fake; then the
      same teardown without the fake
      Expect: `e2e-gone` exits 0 (no container to remove). `e2e-fail-rm` exits
      1 `TEARDOWN_INCOMPLETE`: the record says `container: "failed"`,
      `complete: false`, `remaining` names the container, and the worktree,
      its record and `.containers/e2e-fail-rm/treebox-resources.json` stay.
      The retry exits 0 and leaves no container, worktree, record or
      manifest.
- [ ] **H11 enter --template** - `tb enter e2e-launch … --template e2e-offline --print`,
      then `--template ghost --json`
      Expect: exit 0 both: the template is read only when setup re-runs, so an
      existing container stays enterable.
- [ ] **H12 resource limits** - `tb create e2e-lim … --cpus 1 --memory 512m
      --pids-limit 64 --json`; `tb enter e2e-lim … --memory 512m --json` and
      `--memory 1g --json`. Drift: with `[resources] memory = "2g"` create
      `e2e-lim-cfg`, set `memory = "3g"`, then `enter --json` of `e2e-lim-cfg`
      and of `e2e-lim`
      Expect: create exit 0 with `resources: {cpus: 1, memory: "512m", pids:
      64}`; `docker inspect` shows `HostConfig.Memory` 536870912, `NanoCpus`
      1000000000, `PidsLimit` 64; `list --json` shows the same `resources`. The
      same value enters (exit 0); another exits 5 `RESOURCE_MISMATCH` with
      `treebox teardown` / `treebox create` in the hint. `e2e-lim-cfg` reports
      `resources_drift.memory: "3g"` and one warning; `e2e-lim` (limits chosen
      by flags) has `resources_drift` all `null`.
- [ ] **H13 macOS Keychain login staged** (macOS + docker; boundary: a fake
      `security`) - `rm "$HOME/.claude/.credentials.json"`, a `security` that
      prints a JSON login, `tb create e2e-kc … --json`
      Expect: exit 0; `$ROOT/.containers/e2e-kc/credentials/claude/.credentials.json`
      is a regular file with that login and no group or other permission
      bits; `cat /home/agent/.claude/.credentials.json` in the container shows
      it. SKIP without macOS or docker.
- [ ] **H14 firewall on every entry, FIREWALL_FAILED** - a template `e2e-fw`:
      the offline template plus `firewall.json` (`TREEBOX_FIREWALL=1`) and an
      `init-firewall.sh` that appends to `/tmp/treebox-e2e-firewall-runs` and
      fails when `/tmp/treebox-e2e-firewall-fail` exists (it counts runs; it
      blocks nothing). `tb create e2e-fw … --template e2e-fw --firewall
      --json`; `tb enter … --print`; `docker restart` the container and
      `enter --print` again; create the fail flag, then `tb enter … --json`
      and a plain `tb enter …` (launch)
      Expect: the first run happened before the post-create setup; each entry
      adds one run, also to a running container and after a restart outside
      treebox; with the flag: exit 1 `FIREWALL_FAILED` whose message has the
      script's `ERROR` line and whose hint names `treebox template path
      e2e-fw`, stdout empty (no entry command), and the launch exits 1
      without starting the agent.
- [ ] **H15 sandbox restarted under the agent** - `tb enter e2e-rst … --
      --stub-sleep=60` in the background; once the stub runs, `docker restart
      -t 1` the container
      Expect: exit 1; stderr says the sandbox `stopped or restarted`; no agent
      exit line (the agent's own status is unknown).
- [ ] **H16 HARNESS_MISSING in the sandbox** - `docker exec -u root <ctr> rm
      /usr/local/bin/codex`, then `tb enter e2e-nocodex … --harness codex`
      (launch); then a plain `tb enter e2e-nocodex …`
      Expect: exit 1, stderr says `'codex' not found in the sandbox`, no agent
      exit line; the plain enter (claude) exits 0.
- [ ] **H17 cancellation in the container** - `tb enter e2e-cancel … --
      --stub-sleep=60` in the background; once the stub runs, SIGTERM the
      treebox process
      Expect: exit 143, stderr `Cancelled by SIGTERM`, no agent exit line;
      `docker top` shows no `sleep 60`; the container is still running.
- [ ] **H18 bundled image** (opt-in: `TREEBOX_E2E_BUNDLED_IMAGE=1`; builds the
      5.6 GB bundled image and needs the network) - `tb create e2e-bundled …
      --isolation docker --firewall --print` with the default template
      Expect: exit 0; `claude`, `codex`, `uv` and `gh` are on the agent's
      `PATH`; `/opt/google/chrome/chrome --headless --no-sandbox --dump-dom
      about:blank` exits 0 as the agent (Chromium needs no sandbox of its
      own); `curl https://example.com` fails (egress lockdown) and `iptables
      -S OUTPUT` has `REJECT` and no blanket `ACCEPT` for the host gateway (the
      template's `ALLOWED_HOST_PORTS` is empty). After `docker restart` the
      rules are gone; the next `enter --print` puts them back. SKIP unless
      opted in.

## I. interactive prompts (pseudo-terminal)

Setup: `i-keep`, `i-yes`, `i-pick`, `i-stay`, `i-dirty`. Drive these on a real
terminal (stdin, stdout and stderr a tty); the harness uses Python's `pty`.

- [ ] **I1 confirmation: no** - `tb teardown i-keep …`, answer `n`
      Expect: exit 1 (`Aborted`); the worktree kept.
- [ ] **I2 confirmation: yes** - `tb teardown i-yes …`, answer `y`
      Expect: exit 0; removed; branch kept.
- [ ] **I3 chooser** - `tb teardown …` (no refs): tick `i-pick` only (arrows,
      space, enter), tick no branch at the second question
      Expect: exit 0; only `i-pick` removed; its branch kept.
- [ ] **I4 chooser cancel** - open the chooser, press Ctrl+C
      Expect: exit 0; nothing removed.
- [ ] **I5 chooser mixed pick** - dirty `i-dirty`; tick it and `i-stay`, and
      `i-stay`'s branch at the second question
      Expect: exit 5; `i-stay` and its branch removed; `i-dirty` kept and named
      as kept for uncommitted changes.

## P. parallel dispatch

- [ ] **P1 parallel creates** - four `tb create par-<x> … --json` started at once
      Expect: exit 0 each (the repository lock serializes the shared git
      writes); every worktree listed with fresh deps and a `.env`.
- [ ] **P2 same name at once** - two `tb create par-same … --json` at once
      Expect: one exits 0, the other 5 (`LOCK_HELD`, `BRANCH_EXISTS` or
      `SLUG_CONFLICT`); exactly one registration.
- [ ] **P3 a held lock** - with `setup_hook = ["sleep 6"]`, start
      `tb create par-held … --print` in the background; while it provisions,
      `tb enter par-held … --json` and `tb teardown par-free par-held … --force --json`
      Expect: both exit 5 `LOCK_HELD`; `par-free` is not removed either (the
      batch takes every lock first); the background create exits 0, and a later
      enter works (the lock is released).

## R. regressions (bugs fixed on 11 November 2026)

Each fails on `e8a7fa1` (before the fixes) and passes after them.

- [ ] **R37 `.env` directory (#37)** - a branch `envdir` that commits `.env/config`;
      `tb create --checkout envdir … --json`, then `tb enter envdir … --print`
      Expect: exit 0 both; the create's `warnings` has the `.env not copied`
      entry; `.env` stays a directory with its tracked content; `list --json`
      says `env: absent`; enter warns `.env not copied` on stderr; no
      traceback.
- [ ] **R38 stray directory hint (#38)** - `mkdir "$ROOT/junk"`, `tb create junk … --json`
      Expect: exit 5 `SLUG_CONFLICT` whose hint gives `rm -rf …` and does not
      recommend `treebox teardown junk` (which exits 3 for it); after the
      removal, create succeeds.
- [ ] **R39 cold dry-run (#39)** - `--dry-run --json` with and without `--cold`,
      host and docker
      Expect: the host cold plan routes caches to `treebox-cold-…`, the warm
      plan does not; the docker warm plan mounts `/caches/…`, the cold plan
      does not; no cache directory is created.
- [ ] **R43 nested roots (#43)** - `R/foo` on `foo` and `R/nested/foo` on
      `nested-branch`; `tb enter nested-branch --root R --json`, then
      `tb teardown nested-branch --root R --force --json`
      Expect: both act on `R/nested/foo`; `R/foo` untouched.
- [ ] **R44 credential refresh (#44)** (docker) - replace the staged
      `settings.json` of `e2e-cred` with a symlink, then a hardlink, to an
      outside file; `tb enter e2e-cred … --print` after each
      Expect: exit 0; the outside file's content and mode unchanged; the staged
      entry is a regular file again with the (fake) login content.
- [ ] **R45 submodule copy (#45)** - add a submodule `sub`; a branch
      `copy-target` puts a symlink at `sub/inner` to an outside dir;
      `tb create --checkout copy-target … --json`
      Expect: exit 5 `DESTINATION_CONFLICT` with `error.path` at the link; the
      outside file untouched; after removing the link, a retry exits 0 and
      copies the submodule.
- [ ] **R45b unreadable submodule source** (non-root) - add submodule `sub`
      within this scenario, then `chmod 000` a file in
      `$REPO/sub`, `tb create r45b … --json`
      Expect: exit 1 `SUBMODULE_COPY_FAILED`; no traceback.
- [ ] **R46 merged badge (#46)** (boundary: fake `gh`) - `reused` and
      `squashed`, each pushed, each with a fake merged PR at its tip; then a
      new commit on `reused`; origin set to `https://forge.invalid/…` (never
      resolves); open the chooser with `tests/e2e/fakes` first on `PATH`
      Expect: `reused` shows `merge not verified for current tip`, never
      `✓ merged`; `squashed` shows `✓ merged (PR #48)`; Ctrl+C removes nothing.

## U. unattended mode

Setup: `tb create u1 … --json`.

- [ ] **U1 agent args on create** - `tb create u1-task … --json -- -p "a task"`;
      replay its `entry_command` (with the stub first on `PATH`); `tb create
      … --json -- -p x` (no name)
      Expect: exit 0; `entry_command` ends with `-p 'a task'`; `warnings: []`;
      the replay gives the agent `--dangerously-skip-permissions -p 'a task'`;
      agent args without a name exit 2.
- [ ] **U2 no prompt with a terminal** - on a pty: `tb --non-interactive
      teardown u1 …`, `TREEBOX_NON_INTERACTIVE=1 tb teardown u1 …`, and
      `tb --non-interactive teardown …` (no refs)
      Expect: exit 5 each at once, no confirmation prompt and no chooser; the
      message says it refuses non-interactively; `u1` is kept.
- [ ] **U3 agent exit line** - `tb enter u1 … -- --stub-exit=3`, `-- --stub-kill=KILL`,
      then a plain `tb enter u1 …`
      Expect: exit 3 with the last stderr line `treebox: agent exited with
      status 3`; exit 137 with `treebox: agent exited with status 137 (killed
      by SIGKILL)`; exit 0 and no such line.
- [ ] **U4 cancellation** - `tb enter u1 … -- --stub-sleep=60` in the
      background; once the stub runs, SIGTERM the treebox process; again with
      SIGINT
      Expect: exit 143, then 130; stderr `Cancelled by SIGTERM` /
      `Cancelled by SIGINT` (`CANCELLED`), no agent exit line; the stub
      process is gone; the worktree is kept. (The docker case is H17.)

## Cross-cutting invariants (assert continuously, fail the run on any hit)

- [ ] **X1 stream discipline** — in every `--json`/`--print` scenario stdout
      parsed cleanly with zero diagnostic lines mixed in.
- [ ] **X2 exit-code table** — no scenario produced a code outside its
      documented meaning (`0/1/2/3/4/5`).
- [ ] **X3 schemaVersion** — every JSON payload (success *and* error) carried
      `schemaVersion`; the number matches `tests/golden/` (currently `1`).
      If code and `tests/golden/` disagree with `docs/usage.md` or
      `skills/treebox/SKILL.md`, report **doc drift** as a finding.
- [ ] **X4 worktree lock** - if any command reported a lock conflict (exit 5)
      during sequential runs (P2 and P3 provoke one on purpose), that's a bug - the lock must be released on exit.
- [ ] **X5 no host pollution** — after `e2e_cleanup`: nothing new under the real
      `~/.treebox`, no stray `treebox-e2e.*` dirs left in `$TMPDIR`,
      `git -C <treebox repo> status` unchanged by the run, and — if group H ran —
      **no leftover docker artifacts**: `docker ps -a --filter name=treebox-e2e-`
      and `docker images 'treebox-e2e-*'` both empty. `e2e_cleanup` reaps these via
      `e2e_reap_docker`; if a run crashed before cleanup, call `e2e_reap_docker`
      directly (it only removes `treebox-e2e-*`, never real worktrees). This check
      only works because every group-H worktree carries the `e2e-` name prefix.

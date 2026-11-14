# Create

Create makes one isolated git worktree for one task: a fresh branch from the freshly fetched `origin/<base>` (or an existing branch), the repo's `.env` and submodules copied in, dependencies set up, then the agent launched in it, or its launch command printed.

Used with: [Enter](./enter.md) re-launches what create made, [Teardown](./teardown.md) removes it, [Config](./config.md) gives the defaults, [Docker sandbox](./docker-sandbox.md) and [Firewall](./firewall.md) and [Resource limits](./resource-limits.md) are set at create, [Identity and ports](./identity-and-ports.md) are assigned at create, [Unattended use](./unattended.md) is the launch contract, [Remote workers](./remote-workers.md) retry it.

## Sub-features

- `create-named`: an explicit name is the branch, made fresh from `origin/<base>`.
- `create-placeholder`: no name gives a generated petname on a `treebox/<petname>` branch.
- `create-flatten`: a slash in a name becomes `--` in the directory.
- `create-checkout`: `--checkout` uses an existing branch instead of a new one.
- `create-base`: `--base` picks the base branch.
- `create-harness`: `--harness` picks the agent CLI that launches.
- `create-emit`: `--print` and `--json` provision, then emit the launch command instead of launching.
- `create-launch`: without `--print`, `--json` or `--dry-run`, the agent launches in the worktree.
- `create-agent-args`: args after `NAME --` go to the agent.
- `create-dry-run`: `--dry-run` prints the plan, changes nothing, and fails like a real create would.
- `create-conflicts`: an existing branch, directory or checked-out branch is a conflict, never an overwrite.
- `create-invalid`: a bad name, branch, repo or root stops before anything is made.
- `create-fetch`: a failed fetch stops the create; `--no-fetch` is the only escape.
- `create-setup`: a failed setup step fails the create, keeps the worktree, and the same create finishes it.
- `create-guard`: every worktree refuses to push `treebox/*` branches.
- `create-submodules`: submodules the main checkout has are copied in.
- `create-output`: `--quiet`, `--verbose` and `NO_COLOR` change only stderr.
- `create-parallel`: creates of different names in one repo can run at once; one name is locked.

## How to get to it (user POV)

- `treebox create [NAME] [-- AGENT_ARGS...]`
- Flags: `--checkout BRANCH`, `--base BRANCH`, `--repo PATH`, `--root PATH`, `--harness/-H claude|codex`, `--cold`, `--no-fetch`, `--dry-run/-n`, `--print`, `--json`, `--quiet/-q`, `--verbose/-v`.
- Docker flags (see their feature files): `--isolation`, `--template`, `--firewall/--no-firewall`, `--cpus`, `--memory`, `--pids-limit`.
- Config defaults: `base`, `root`, `harness`, `env_file`, `setup_hook`, `caches` in `config.toml` ([Config](./config.md)).
- `NO_COLOR` in the environment.

## Driving it with treebox

Preconditions: the fixture (`source .claude/skills/verify-treebox/fixture.sh`), `e2e_doctor` all `ok`. No engine is needed. The fixture repo has `main` and `dev` on `origin`, a uv project, and an untracked `.env` with `SECRET=canonical`.

- **Named create.** Run `treebox create fix-auth --repo "$REPO" --root "$ROOT" --json`. Exit 0. stdout is one JSON object with `name` and `branch` `fix-auth`, `base` `main`, `created` true, `worktree_path` `$ROOT/fix-auth`, `id` (10 hex), `port_base` 20000, `port_count` 10, `warnings` `[]`, and `entry_command`, an argv array `["sh", "-c", "cd … && export TREEBOX_NAME=… && exec claude --dangerously-skip-permissions"]`. `$ROOT/fix-auth/.env` reads `SECRET=canonical`, `$ROOT/fix-auth/.venv` exists, and `git -C "$ROOT/fix-auth" branch --show-current` prints `fix-auth`.
- **Placeholder.** Run `treebox create --repo "$REPO" --root "$ROOT" --json`. Exit 0, `name` is a petname (for example `clever-robin`) and `branch` is `treebox/<name>`.
- **Flatten.** Run `treebox create feature/auth --repo "$REPO" --root "$ROOT" --json`. Exit 0, `branch` `feature/auth`, `name` `feature--auth`, `worktree_path` `$ROOT/feature--auth`.
- **Checkout.** Run `treebox create review-dev --checkout dev --repo "$REPO" --root "$ROOT" --json`. Exit 0, `name` `review-dev`, `branch` `dev` (no new branch).
- **Base.** Run `treebox create from-dev --base dev --repo "$REPO" --root "$ROOT" --json`. Exit 0, `branch` `from-dev`, `base` `dev`.
- **Print.** Run `treebox create print-x --repo "$REPO" --root "$ROOT" --print`. Exit 0. stdout is exactly one line, `sh -c 'cd $ROOT/print-x && export TREEBOX_NAME=print-x TREEBOX_ID=… TREEBOX_PORT_BASE=… TREEBOX_PORT_COUNT=10 && exec claude --dangerously-skip-permissions'`.
- **Harness.** Run `treebox create cdx --repo "$REPO" --root "$ROOT" --harness codex --print`. Exit 0, the command ends with `exec codex --dangerously-bypass-approvals-and-sandbox` (no codex login is needed to print).
- **Launch.** Run `treebox create launch-host --repo "$REPO" --root "$ROOT"`. Exit 0. stdout is the stub agent's record between `TREEBOX_E2E_STUB_BEGIN` and `TREEBOX_E2E_STUB_END`: `name=claude`, `cwd=$ROOT/launch-host`, `in_container=no`, `api_key=unset`, `treebox_name=launch-host`, and one `arg=--dangerously-skip-permissions`. stderr ends with a `Ready in …ms` line that names `claude`.
- **Agent args.** Run `treebox create task-x --repo "$REPO" --root "$ROOT" --json -- -p "a task"`. Exit 0, the last `entry_command` element ends with `exec claude --dangerously-skip-permissions -p 'a task'`. Without a name, `treebox create --repo "$REPO" --root "$ROOT" --json -- -p x` exits 2 `INVALID_BRANCH` and the hint says to name the worktree first.
- **Dry run.** Run `treebox create plan-x --repo "$REPO" --root "$ROOT" --dry-run --json`. Exit 0, `dry_run` true, `commands` is an array whose first entry is `# reserve port block … (… a dry run reserves nothing)`. `$ROOT/plan-x` does not exist. The same without `--json` exits 0 with stdout empty and the plan on stderr.
- **Branch exists.** After the named create, run `treebox create fix-auth --repo "$REPO" --root "$SBX/wts2" --json`. Exit 5, stderr `error.code` `BRANCH_EXISTS`, the hint names `treebox create --checkout fix-auth`. `$SBX/wts2/fix-auth` does not exist. With `--dry-run` added: the same exit 5 `BRANCH_EXISTS` and stdout empty.
- **Name exists.** Run `treebox create fix-auth --repo "$REPO" --root "$ROOT" --json` again. Exit 5 `SLUG_CONFLICT`, the hint names `treebox enter fix-auth`. For a stray directory (`mkdir "$ROOT/junk"`, then `treebox create junk --repo "$REPO" --root "$ROOT" --json`): exit 5 `SLUG_CONFLICT` and the hint gives `rm -rf $ROOT/junk`, not `treebox teardown`.
- **Branch in use.** Run `treebox create dup --checkout fix-auth --repo "$REPO" --root "$ROOT" --json`. Exit 5 `BRANCH_IN_USE`, the message names `$ROOT/fix-auth`.
- **Invalid input.** `treebox create "Bad Name" --repo "$REPO" --root "$ROOT" --json` exits 2 `INVALID_NAME`. `treebox create --checkout bad..ref --repo "$REPO" --root "$ROOT" --json` exits 2 `INVALID_BRANCH`. `mkdir "$SBX/not-a-repo"` then `treebox create x --repo "$SBX/not-a-repo" --json` exits 2 `NOT_A_REPO`, the hint names `--repo`. `touch "$SBX/afile"` then `treebox create x --repo "$REPO" --root "$SBX/afile" --json` exits 2 `INVALID_ROOT`.
- **Fetch.** Run `git -C "$REPO" remote set-url origin /nonexistent`, then `treebox create off-line --repo "$REPO" --root "$ROOT" --json`. Exit 4 `FETCH_FAILED`, the hint names `--no-fetch`, and `$ROOT/off-line` does not exist. Then `treebox create off-line --repo "$REPO" --root "$ROOT" --no-fetch --print` exits 0. Restore with `git -C "$REPO" remote set-url origin "$SBX/origin.git"`.
- **Failed setup.** Run `echo 'setup_hook = ["exit 7"]' > "$TREEBOX_CONFIG"`, then `treebox create c14 --repo "$REPO" --root "$ROOT" --json`. Exit 1 `SETUP_FAILED`, the message names `setup · hook 1` and status 7, and `error.path` is `$ROOT/.logs/c14.setup.log` (it exists). `treebox list --repo "$REPO" --root "$ROOT" --json` shows `c14` with `setup` `failed`, `deps` `unknown`. Run `: > "$TREEBOX_CONFIG"`. Then `treebox create c14 --repo "$REPO" --root "$ROOT" --harness codex --json` exits 5 `RECORD_CONFLICT` (the hint gives the recorded settings, `-H claude`), and `treebox create c14 --repo "$REPO" --root "$ROOT" --json` exits 0 with `created` true; `list` then shows `setup` `done`, `deps` `fresh`.
- **Cold.** Run `treebox create cold-x --repo "$REPO" --root "$ROOT" --cold --print`. Exit 0, stderr has `cache  cold (from source)` and `setup · uv  from-source (cold)`.
- **Pre-push guard.** Run `git -C "$ROOT/fix-auth" branch treebox/scratch`, then `git -C "$ROOT/fix-auth" push origin treebox/scratch`. Exit 1, stderr `treebox: refusing to push placeholder branch 'treebox/scratch'`. `git -C "$ROOT/fix-auth" push origin fix-auth` exits 0.
- **Submodules.** Make a submodule source and add it: `git init -q -b main "$SBX/subsrc" && echo hi > "$SBX/subsrc/f" && git -C "$SBX/subsrc" add f && git -C "$SBX/subsrc" -c user.name=t -c user.email=t@t commit -qm f`, then `git -C "$REPO" -c protocol.file.allow=always submodule add -q "$SBX/subsrc" sub && git -C "$REPO" commit -qm sub && git -C "$REPO" push -q origin main`. `treebox create sub-x --repo "$REPO" --root "$ROOT" --dry-run --json` lists `# copy submodule sub from $REPO into $ROOT/sub-x`. The real `--json` create exits 0, `$ROOT/sub-x/sub/f` reads `hi`, and `git -C "$ROOT/sub-x" status --short` is empty.
- **Output.** `treebox create q-x --repo "$REPO" --root "$ROOT" --quiet --print` exits 0 with stderr empty. `--verbose` instead streams uv's own lines (`Resolved 1 package in …`) on stderr. `NO_COLOR=1 treebox create nc-x --repo "$REPO" --root "$ROOT" --print 2>err` leaves no escape byte in `err`.
- **Parallel.** Start four creates at once: `for x in a b c d; do treebox create par-$x --repo "$REPO" --root "$ROOT" --json >/dev/null & done; wait`. `treebox list --repo "$REPO" --root "$ROOT" --json` shows all four with `deps` `fresh` and `env` `present`. With `echo 'setup_hook = ["sleep 6"]' > "$TREEBOX_CONFIG"`, start `treebox create par-held --repo "$REPO" --root "$ROOT" --print &`; while it runs, `treebox enter par-held --repo "$REPO" --root "$ROOT" --json` exits 5 `LOCK_HELD`. The background create exits 0, and after `: > "$TREEBOX_CONFIG"` the same enter exits 0.

## Gotchas

- `--json` and `--print` never launch; a bare create does. In the fixture the agent is the stub, but never replay a printed `sh -c '… exec claude …'` in your own shell: there `claude` is the real agent. Replay it only as `PATH="$SBX/stub-bin:$PATH" sh -c '…'`.
- `entry_command` in `--json` is an argv array, not a string.
- A re-create of a finished name is `SLUG_CONFLICT`, never an idempotent enter. Only an unfinished setup (`pending` or `failed`) is resumed, and only with the recorded settings.
- `--dry-run` reserves no port block, so the block it shows can be the one the next real create gets.
- A failed setup keeps the worktree. Tear it down, or the next scenario in the same fixture sees it.
- The fixture repo's `.env` is untracked. A tracked `.env` dirties every worktree once you rotate the host copy.
- Submodule copy follows the branch's own `.gitmodules` and copies only paths the main checkout has on disk. A symlink in the destination is `DESTINATION_CONFLICT` (exit 5), an unreadable source file is `SUBMODULE_COPY_FAILED` (exit 1, needs a non-root user).
- Scenarios: B1, B2, B3, B4, B5, B6, B7, B8, B9, B10, B10b, B11, B12, B13, B14, B15, B17, B18, B19, B24, B27, B28, C14, C17, U1, P1, P2, P3, R37, R38, R39, R45, R45b, X4.

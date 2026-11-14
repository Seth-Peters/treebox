# Enter

Enter re-launches the agent in a worktree that already exists: it finds the worktree by name, branch, substring or path, refreshes its `.env`, re-syncs dependencies only when a manifest changed, finishes an unfinished setup, and reuses the choices recorded at create.

Used with: [Create](./create.md) made the worktree and its record, [List](./list.md) shows what enter can find, [Docker sandbox](./docker-sandbox.md) revives or re-creates the container on enter, [Firewall](./firewall.md) runs again on every enter, [Resource limits](./resource-limits.md) are only restated on enter, [Template](./template.md) is read when setup runs again, [Unattended use](./unattended.md) is the launch contract, [Remote workers](./remote-workers.md) address it by path.

## Sub-features

- `enter-emit`: `--print` and `--json` prepare the worktree and emit its launch command.
- `enter-launch`: a bare enter launches the agent and exits with its status.
- `enter-env`: the host `.env` is copied in again on every enter.
- `enter-resync`: deps re-sync only when the manifest hash changed.
- `enter-refs`: a ref is a name, a live branch, a unique substring, or an exact absolute path, also under nested roots.
- `enter-args`: args after `--` go to the agent verbatim.
- `enter-recorded`: recorded isolation and harness win over config; a mismatched `--isolation` is a conflict.
- `enter-record`: the host-owned record is the only policy source; a missing or invalid one is a classified error.
- `enter-linkage`: a worktree without its `.git` pointer is refused.
- `enter-output`: `--cold`, `--quiet` and `--verbose` act on the re-sync.

## How to get to it (user POV)

- `treebox enter REF [-- AGENT_ARGS...]`
- Flags: `--repo PATH`, `--root PATH`, `--isolation`, `--harness/-H`, `--template`, `--cold`, `--print`, `--json`, `--quiet/-q`, `--verbose/-v`, and the limit flags `--cpus`, `--memory`, `--pids-limit` ([Resource limits](./resource-limits.md)).
- REF: the worktree name, its current branch, a unique substring of either, or the absolute worktree path.

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`, and `treebox create fix-auth --repo "$REPO" --root "$ROOT" --json` (exit 0). No engine is needed.

- **Print.** Run `treebox enter fix-auth --repo "$REPO" --root "$ROOT" --print`. Exit 0, one line on stdout, the same shape as create's: `sh -c 'cd $ROOT/fix-auth && export TREEBOX_NAME=fix-auth … && exec claude --dangerously-skip-permissions'`.
- **JSON.** Run `treebox enter fix-auth --repo "$REPO" --root "$ROOT" --json`. Exit 0, `created` false, `name` and `branch` `fix-auth`, and also `resources_drift` (see [Resource limits](./resource-limits.md)).
- **.env refresh.** Run `echo SECRET=rotated > "$REPO/.env"`, then the print command again. `$ROOT/fix-auth/.env` reads `SECRET=rotated`.
- **Re-sync.** Bump the version and lock: `perl -pi -e 's/0\.1\.0/0.2.0/' "$ROOT/fix-auth/pyproject.toml" && (cd "$ROOT/fix-auth" && uv lock -q)`. `treebox list --repo "$REPO" --root "$ROOT" --json` shows `fix-auth` with `deps` `stale`. The print command then reports `dependencies changed since last setup - re-syncing` on stderr, and `list` shows `deps` `fresh` again.
- **Refs.** Run `git -C "$ROOT/fix-auth" branch -m fix/auth-renamed`. Then `treebox enter fix-auth …`, `treebox enter fix/auth-renamed …`, `treebox enter renamed …` and `treebox enter "$ROOT/fix-auth" …` (each with `--repo "$REPO" --root "$ROOT" --json`) all exit 0 with `worktree_path` `$ROOT/fix-auth`.
- **Ambiguous and missing.** Create `amb-one` and `amb-two` (`--json`). `treebox enter amb --repo "$REPO" --root "$ROOT" --json` exits 2 `AMBIGUOUS_REF` and the message lists both. `treebox enter ghost-x --repo "$REPO" --root "$ROOT" --json` exits 3 `NOT_FOUND`. `treebox enter "$ROOT/fix" --repo "$REPO" --root "$ROOT" --json` exits 3 `NOT_FOUND`: a path never falls back to a substring.
- **Nested roots.** Run `treebox create top --repo "$REPO" --root "$SBX/N" --json && git -C "$SBX/N/top" branch -m top-branch`, then `treebox create top --repo "$REPO" --root "$SBX/N/nested" --json && git -C "$SBX/N/nested/top" branch -m nested-top`. `treebox enter nested-top --repo "$REPO" --root "$SBX/N" --json` exits 0 with `worktree_path` `$SBX/N/nested/top`; `treebox enter top --repo "$REPO" --root "$SBX/N" --json` exits 2 `AMBIGUOUS_REF` and names both.
- **Agent args.** Run `treebox enter fix-auth --repo "$REPO" --root "$ROOT" --print -- "continue the refactor"`. Exit 0, the command ends with `exec claude --dangerously-skip-permissions 'continue the refactor'` (quoted inside the `sh -c` string).
- **Launch.** Run `treebox enter fix-auth --repo "$REPO" --root "$ROOT" -- --stub-exit=7 "continue the refactor"`. Exit 7. stdout has `arg=--dangerously-skip-permissions`, `arg=--stub-exit=7`, `arg=continue the refactor`; the last stderr line is `treebox: agent exited with status 7`.
- **Recorded isolation.** Run `treebox enter fix-auth --repo "$REPO" --root "$ROOT" --isolation docker --json`. Exit 5 `ISOLATION_MISMATCH`, the message says the worktree was provisioned with `host`.
- **Recorded harness.** Run `treebox create c10-codex --repo "$REPO" --root "$ROOT" --harness codex --json`. Then `treebox enter c10-codex --repo "$REPO" --root "$ROOT" --print` ends in `exec codex …`, with `--harness claude` added it ends in `exec claude …`, and without it again `exec codex …`.
- **Policy is the record.** Write a state file into the sandbox-writable git dir: `echo '{"isolation": "docker", "template": "/etc", "provisioned": true}' > "$(git -C "$ROOT/fix-auth" rev-parse --absolute-git-dir)/treebox-state.json"`. The print command still exits 0 with the host `sh -c` command, and `"$ROOT/.records/fix-auth.json"` still says `"isolation": "host"`.
- **Invalid record.** Create `c9-corrupt`, then set `"isolation": "qemu"` in `$ROOT/.records/c9-corrupt.json` (for example with `jq '.isolation="qemu"'` into a temp file and `mv`). `treebox enter c9-corrupt --repo "$REPO" --root "$ROOT" --json` exits 5 `RECORD_INVALID`, `error.path` is the record, the hint names `treebox teardown c9-corrupt`. `list --json` shows it with `health` `unreadable_state` and `isolation` `""`.
- **Missing record.** Create `norec` (`--print`), `rm "$ROOT/.records/norec.json"`, then `treebox enter norec --repo "$REPO" --root "$ROOT" --json`. Exit 5 `RECORD_MISSING`, stdout empty, the hint names `treebox teardown norec` and `treebox create`.
- **Invalid linkage.** Create `c15`, `rm "$ROOT/c15/.git"`, then `treebox enter c15 --repo "$REPO" --root "$ROOT" --json`. Exit 5 `INVALID_LINKAGE`, the hint names `treebox teardown c15`. `list --json` shows `health` `invalid_linkage`, `missing` true.
- **Cold, quiet, verbose.** Change the manifest again (as in Re-sync, `0.2.0` to `0.3.0`), then `treebox enter fix-auth --repo "$REPO" --root "$ROOT" --cold --print`: stderr has `setup · uv  from-source (cold)`. `--quiet --print` leaves stderr empty. After one more manifest change, `--verbose --print` streams uv's `Resolved 1 package in …` on stderr.

## Gotchas

- A bare `enter` launches. Only the fixture's stub makes that safe; never replay a printed command in your own shell without `PATH="$SBX/stub-bin:$PATH"` first.
- Enter needs the record. A worktree made by plain `git worktree add` is listed but cannot be entered (`RECORD_MISSING`); the recovery is teardown, then create.
- A name or branch ref falls back to a unique substring. In a test that removes worktrees, use the absolute path so a retry cannot hit another worktree.
- Two worktrees with the same leaf under nested roots are ambiguous by name (`AMBIGUOUS_REF`); enter them by branch or path.
- `--template` on enter is read only when setup runs again, so an existing container stays enterable with any value ([Template](./template.md)).
- The manifest edit in Re-sync runs `uv lock` in your shell, outside the fixture's `HOME`; it writes only to the worktree and the shared uv cache.
- Scenarios: C1, C2, C3, C4, C5, C6, C7, C8, C9, C10, C11, C12, C13, C15, C16, C18, R43.

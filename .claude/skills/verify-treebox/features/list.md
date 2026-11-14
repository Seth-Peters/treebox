# List

List shows every worktree registered under a root, with its live branch, isolation, dependency freshness, `.env` presence and health, as a table for people or as JSON for scripts. It never changes anything.

Used with: [Create](./create.md) and [Enter](./enter.md) change what it shows, [Teardown](./teardown.md) removes rows, [Identity and ports](./identity-and-ports.md) adds `id` and the port block, [Resource limits](./resource-limits.md) adds `resources`, [Remote workers](./remote-workers.md) read status from it.

## Sub-features

- `list-table`: one row per worktree, the live branch under the stable name, and a summary and root footer.
- `list-json`: `worktrees[]` with the full field set, `schemaVersion` on top.
- `list-health`: each row's `health`, `setup` and `deps`, also for registrations treebox never provisioned.
- `list-empty`: a root with no worktrees is an empty list, not an error.
- `list-alias`: `ls` is the same command.
- `list-no-git`: without `git` on `PATH`, list fails loudly.

## How to get to it (user POV)

- `treebox list [--repo PATH] [--root PATH] [--json]`
- `treebox ls` (hidden alias).
- The root default comes from `root` in `config.toml` ([Config](./config.md)).

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. Create `d-one` and `d-two` (`treebox create d-one --repo "$REPO" --root "$ROOT" --json`, the same for `d-two`), rename `d-two`'s branch (`git -C "$ROOT/d-two" branch -m feature/renamed`), and add a registration treebox never provisioned: `git -C "$REPO" worktree add -q -b d-raw "$ROOT/d-raw" main`.

- **Table.** Run `treebox list --repo "$REPO" --root "$ROOT"`. Exit 0, stderr empty. stdout has the header `NAME BRANCH ISOLATION LAST COMMIT AGE DEPS ENV`, the row `d-two  feature/renamed  host … fresh … present`, the row `d-raw  d-raw  unknown … unknown … absent`, then `Summary: 3 worktrees` and `Root: $ROOT`.
- **JSON.** Run `treebox list --repo "$REPO" --root "$ROOT" --json`. Exit 0, top-level keys `schemaVersion` and `worktrees`. Each entry has `name`, `id`, `branch`, `unnamed`, `missing`, `last_commit`, `commit_epoch`, `path`, `base`, `isolation`, `harness`, `resources`, `deps`, `env`, `health`, `setup`, `port_base`, `port_count`. `d-one`: `health` `healthy`, `setup` `done`, `deps` `fresh`, `resources` `{cpus: null, memory: null, pids: null}`. `d-raw`: `isolation` `""`, `health` `unreadable_state`, `setup` `""`, `resources` and `port_base` `null`.
- **Empty root.** Run `treebox list --repo "$REPO" --root "$SBX/empty" --json`. Exit 0, `{"schemaVersion": 1, "worktrees": []}`.
- **Alias.** Run `treebox ls --repo "$REPO" --root "$ROOT" --json`. Exit 0, byte-for-byte the same stdout as `list --json`.
- **No git.** Run `E2E_PATH=/usr/sbin:/sbin treebox list --repo "$REPO" --root "$ROOT" --json` (a `PATH` without `git`). Exit 1, stderr `error.code` `GIT_MISSING`.
- **Health of broken worktrees.** See [Enter](./enter.md): an invalid record shows `unreadable_state`, a removed `.git` pointer shows `invalid_linkage` with `missing` true, a failed setup shows `setup` `failed` and `deps` `unknown`.

## Gotchas

- A registration without a readable record shows isolation `unknown` (`""` in JSON), never the config default.
- `deps` is `stale` after a manifest change in the worktree and `fresh` again after the next enter; check it before and after.
- The table wraps a long root path at the terminal width (80 columns when piped). Assert on JSON, not on the table text.
- `--root` must match the root used at create. A different root lists other worktrees, or none.
- Scenarios: D1, D2, D3, D4, B20, C15.

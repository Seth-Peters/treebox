# Identity and ports

Every worktree gets a stable identity and a block of ports no other treebox worktree on the machine has, so repo services in parallel worktrees do not collide. Setup and the agent get `TREEBOX_NAME`, `TREEBOX_ID`, `TREEBOX_PORT_BASE` and `TREEBOX_PORT_COUNT`; `create --json`, `enter --json` and `list --json` report the same values. One machine-wide ledger under `TREEBOX_HOME` owns the blocks.

Used with: [Create](./create.md) reserves the block, [Enter](./enter.md) keeps it, [Teardown](./teardown.md) releases it, [List](./list.md) shows it, [Config](./config.md) sets the range, [Docker sandbox](./docker-sandbox.md) passes the variables into the container, [Remote workers](./remote-workers.md) use `id` and the block per task.

## Sub-features

- `identity-env`: the four `TREEBOX_*` variables reach the agent and are in every emitted command.
- `identity-id`: `id` is 10 lowercase hex characters, derived from the worktree path, the same in every payload.
- `ports-reserve`: each create reserves the next free block of the range in the ledger.
- `ports-exhausted`: a full range fails before anything is made.
- `ports-release`: a complete teardown frees the block for the next create; enter keeps its block.
- `ports-ledger`: an invalid ledger is a classified error and is never rewritten.

## How to get to it (user POV)

- `TREEBOX_NAME`, `TREEBOX_ID`, `TREEBOX_PORT_BASE`, `TREEBOX_PORT_COUNT` in setup and the agent's environment.
- `id`, `port_base`, `port_count` in `create --json`, `enter --json`, `list --json`.
- `[ports]` in `config.toml`: `start`, `end`, `block_size` (default 20000-26999, blocks of 10).
- The ledger `$TREEBOX_HOME/ports.json`.

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. No engine is needed.

- **Identity.** `treebox create id-a --repo "$REPO" --root "$ROOT" --json` exits 0 with `id` (10 hex), `port_base` 20000, `port_count` 10. A launch, `treebox create id-b --repo "$REPO" --root "$ROOT" </dev/null`, prints the stub's record with `treebox_name=id-b`, `treebox_id=<id>`, `treebox_port_base=20010`, `treebox_port_count=10`. `treebox list --repo "$REPO" --root "$ROOT" --json` shows the same `id` and `port_base` for both, and `"$TREEBOX_HOME/ports.json"` has one block per worktree with its `worktree` and `repo` paths.
- **Range.** `printf '[ports]\nstart = 30000\nend = 30019\nblock_size = 10\n' > "$TREEBOX_CONFIG"`. `treebox create g14-a …` and `treebox create g14-b …` (each `--repo "$REPO" --root "$ROOT" --json`) get `port_base` 30000 and 30010.
- **Exhausted.** `treebox create g14-c --repo "$REPO" --root "$ROOT" --json` exits 5 `PORTS_EXHAUSTED`, `error.path` `$TREEBOX_HOME/ports.json`; no `$ROOT/g14-c` and no `g14-c` branch.
- **Release.** `treebox teardown g14-a --repo "$REPO" --root "$ROOT" --force --json` exits 0; then `treebox create g14-c --repo "$REPO" --root "$ROOT" --json` exits 0 with `port_base` 30000. `treebox enter g14-b --repo "$REPO" --root "$ROOT" --json` keeps 30010. Reset with `: > "$TREEBOX_CONFIG"`.
- **Invalid ledger.** `echo 'not json' > "$TREEBOX_HOME/ports.json"`, note its `shasum`, then `treebox create g15 --repo "$REPO" --root "$ROOT" --json`. Exit 1 `PORT_LEDGER_INVALID`, `error.path` the ledger, the `shasum` unchanged, no `$ROOT/g15`.

## Gotchas

- The ledger is machine-wide per `TREEBOX_HOME`: blocks of every repo and root count. The `PORTS_EXHAUSTED` message counts blocks outside the configured range too (it said `4 block(s)` for a range of two blocks in this drive).
- `--dry-run` reserves nothing, so it can show the block the next real create gets.
- A teardown that is not complete (`TEARDOWN_INCOMPLETE`) keeps the block until a retry finishes.
- `TREEBOX_ID` is derived from the path: a worktree created again at the same path gets the same `id`.
- Scenarios: B1, B15, G14, G15, H7.

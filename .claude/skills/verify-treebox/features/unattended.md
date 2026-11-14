# Unattended use

Unattended use is the contract scripts and other agents build on: no picker, prompt or credential question ever opens (with `--non-interactive`, `TREEBOX_NON_INTERACTIVE=1`, or no terminal on stdin); what would need an answer is a classified error; data goes to stdout and diagnostics to stderr; every JSON payload carries `schemaVersion`; exit codes are stable; a launch passes the agent's exit status through and names a non-zero one in one last stderr line; a cancel signal stops the agent and exits `128 + signal`.

Used with: [Create](./create.md) and [Enter](./enter.md) launch under it, [Teardown](./teardown.md) needs `--yes` or `--force` under it, [Docker sandbox](./docker-sandbox.md) adds the in-container cases, [Doctor](./doctor.md) shows the login it needs, [Remote workers](./remote-workers.md) tell failures apart by it.

## Sub-features

- `unattended-flag`: `--non-interactive` and `TREEBOX_NON_INTERACTIVE=1` refuse to prompt even with a terminal attached.
- `unattended-login`: an unattended launch without a usable login fails before anything is made.
- `unattended-agent`: a launch without the agent binary fails before anything is made.
- `exit-line`: the agent's status passes through; a non-zero one is named in the last stderr line, a signal death too.
- `cancel`: SIGTERM, SIGINT or SIGHUP to treebox stops the agent and exits `128 + signal`, `CANCELLED`.
- `streams`: under `--json`, stdout is one JSON object on success and empty on failure; stderr holds exactly one error object.
- `exit-codes`: `0` ok, `1` runtime, `2` usage, `3` not found, `4` fetch or auth, `5` conflict.

## How to get to it (user POV)

- `treebox --non-interactive COMMAND …` (the flag goes before the command), or `TREEBOX_NON_INTERACTIVE=1`.
- No terminal on stdin (a pipe, `</dev/null`, a CI job, SSH without a tty) behaves the same.
- `--json`, `--print`, `--dry-run` on the commands that have them.
- Signals to the treebox process: `kill -TERM`, `kill -INT`, `kill -HUP`.

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`, and `treebox create u1 --repo "$REPO" --root "$ROOT" --json` (exit 0). No engine is needed; the docker cases are in [Docker sandbox](./docker-sandbox.md).

- **Exit line.** `treebox enter u1 --repo "$REPO" --root "$ROOT" -- --stub-exit=3 </dev/null` exits 3, the last stderr line is `treebox: agent exited with status 3`. `-- --stub-kill=KILL` exits 137 with `treebox: agent exited with status 137 (killed by SIGKILL)`. A plain `treebox enter u1 --repo "$REPO" --root "$ROOT" </dev/null` exits 0 and has no such line.
- **Cancel.** Start `treebox enter u1 --repo "$REPO" --root "$ROOT" -- --stub-sleep=60 </dev/null >out 2>err &`, wait about 3 s (the stub's `pid=` line is in `out`), then `kill -TERM` the treebox pid. Exit 143, stderr `Cancelled by SIGTERM: the agent was sent SIGTERM and has exited.`, no agent exit line, the stub's pid is gone, and `$ROOT/u1` is kept. With `kill -INT`: exit 130, `Cancelled by SIGINT`.
- **No prompt with a terminal.** On a pseudo-terminal (macOS: `script -q /dev/null CMD`), with `y` typed after 2 s: `(sleep 2; printf 'y\r') | script -q /dev/null treebox --non-interactive teardown u1 --repo "$REPO" --root "$ROOT"` exits 5 at once with `Refusing to remove non-interactively without confirmation.`, never a prompt. The same with `TREEBOX_NON_INTERACTIVE=1 treebox teardown u1 …`. With no refs, `treebox --non-interactive teardown --repo "$REPO" --root "$ROOT"` exits 5, `Refusing to choose a worktree non-interactively.` `u1` is kept.
- **No login.** `mv "$E2E_HOME/.claude/.credentials.json" "$SBX/cred.bak"`, then `treebox --non-interactive create b25 --repo "$REPO" --root "$ROOT" </dev/null`. Exit 1, stderr `No claude login for an unattended launch: no login at …`, no stub record, no `$ROOT/b25` and no `b25` branch. `TREEBOX_NON_INTERACTIVE=1 treebox create b25 …` (a launch) is the same. `--print` and `--json` need no login and exit 0. Move the file back.
- **No agent.** `E2E_PATH="$SBX/toolbox:/usr/bin:/bin" treebox create b16 --repo "$REPO" --root "$ROOT" </dev/null` (no stub on `PATH`). Exit 1, `'claude' not found on PATH.`, no traceback, no `$ROOT/b16`, no `b16` branch: the agent is checked before provisioning.
- **Streams.** `treebox enter ghost --repo "$REPO" --root "$ROOT" --json` exits 3: stdout is empty and stderr is one object with exactly the keys `schemaVersion` and `error` (`code`, `message`, `hint`). A success, for example `treebox list --repo "$REPO" --root "$ROOT" --json`, has the object on stdout with `schemaVersion` 1 and nothing but progress on stderr. Success payloads of `create`, `enter` and `teardown` carry `warnings` (`[]` when none).

## Gotchas

- `--json` and `--print` never launch, so they never need a login or the agent binary. To prove `LOGIN_MISSING` or `HARNESS_MISSING`, launch.
- `--non-interactive` is a global flag: it goes before the command (`treebox --non-interactive teardown …`).
- Cancel the treebox process, not the stub: the point is that treebox passes the signal on.
- treebox exits 255 only when the agent exited 255, and then with the exit line. An unexpected failure is the generic `ERROR` (exit 1).
- Every `--json` and `--print` run in the evidence sweep is checked for stream discipline (X1), the exit-code table (X2), `schemaVersion` (X3) and released locks (X4).
- Scenarios: U1, U2, U3, U4, B16, B25, R37, X1, X2, X3.

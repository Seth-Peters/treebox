# Doctor, version and help

Doctor checks what a launch needs before anything is made: git, the repo, the uid/gid, the selected agent binary and its real login (a login file, or the macOS Keychain), `.env`, git auth for the fetch, and the isolation mode's own dependencies. `version`, `--help` and shell completion are the other read-only entry points.

Used with: [Create](./create.md) and [Enter](./enter.md) fail on the same checks, [Docker sandbox](./docker-sandbox.md) adds the engine row, [Unattended use](./unattended.md) turns a missing login into `LOGIN_MISSING`, [Config](./config.md) gives the default harness and isolation, [List](./list.md) fails the same way without git.

## Sub-features

- `doctor-host`: the host checks, as rows for people and as JSON with `ok`.
- `doctor-docker`: `--isolation docker` adds the engine check.
- `doctor-harness`: `--harness` picks which agent binary and login are hard checks; the other logins are optional rows.
- `doctor-keychain`: on macOS, a missing claude login file falls back to the Keychain item `Claude Code-credentials`.
- `version`: `version`, `--version` and `-V` print the same string.
- `help`: `--help` lists the visible commands; bare `treebox` is a usage error.
- `completion`: the shell-completion protocol completes commands; `--show-completion` and `--install-completion` detect the parent shell.

## How to get to it (user POV)

- `treebox doctor [--repo PATH] [--isolation host|docker] [--harness/-H claude|codex] [--json]`
- `treebox version`, `treebox --version`, `treebox -V`
- `treebox --help`, `treebox COMMAND --help` or `-h`, bare `treebox`
- `treebox --show-completion`, `treebox --install-completion`; the completion protocol `_TREEBOX_COMPLETE=complete_bash COMP_WORDS=… COMP_CWORD=… treebox`
- Defaults: `harness` and `isolation` in `config.toml` ([Config](./config.md)).

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok` (it runs `treebox doctor` too). The engine is needed only for **Docker**.

- **Version.** `treebox version`, `treebox --version` and `treebox -V` each exit 0 and print the same string, for example `1.0.3.dev45`, the version `e2e_doctor` shows.
- **Human rows.** Run `treebox doctor --repo "$REPO"`. Exit 0. stdout has `● doctor   isolation: host`, one `✓` row each for `git`, `repo`, `uid/gid`, `agent: claude` (`$SBX/stub-bin/claude`), `login: claude` (`$SBX/home/.claude/.credentials.json`), `login: codex`, `.env`, `git auth`, `isolation: host`, and `✓ everything looks good`. Piped, it has no escape byte.
- **JSON.** Run `treebox doctor --repo "$REPO" --json`. Exit 0, `schemaVersion` 1, `ok` true, `isolation` `host`, `advisories` `[]`, and `checks[]` of `{name, ok, detail}` with the rows above.
- **Docker.** Run `treebox doctor --repo "$REPO" --isolation docker --json`. With a reachable engine: exit 0, `ok` true, `isolation: docker` row `docker daemon ok`, and the `agent: claude` row is ok with `in the docker sandbox image · checked at launch`. Without one: exit 1 and that row failed (docker work is SKIP).
- **Soft failures.** `git -C "$REPO" remote set-url origin /nonexistent`, then `treebox doctor --repo "$REPO"`: exit 0, the `git auth` row fails with a `!` advisory and the verdict `usable, but see the advisory above`; `--json` has it in `advisories`. Restore with `git -C "$REPO" remote set-url origin "$SBX/origin.git"`. `mv "$REPO/.env" "$SBX/env.bak"`: doctor still exits 0 and the `.env` row is a muted `·` ending in `optional`. Move it back.
- **Bad isolation.** Run `treebox doctor --isolation qemu`. Exit 2, `Invalid isolation mode 'qemu'. Use one of ('host', 'docker').`
- **Harness.** `treebox doctor --repo "$REPO" -H codex --json` exits 0 with `agent: codex` and `login: codex` ok. With a `PATH` that has the stub `claude` but no `codex` (`mkdir "$SBX/claude-only" && ln -s "$SBX/stub-bin/claude" "$SBX/claude-only/claude"`, then `E2E_PATH="$SBX/safe-bin:$SBX/claude-only:$SBX/toolbox:/usr/bin:/bin" treebox doctor --repo "$REPO" -H codex --json`): exit 1, `agent: codex` `'codex' not found on PATH`. After `mv "$E2E_HOME/.codex/auth.json" "$SBX/auth.bak"`: `-H codex` exits 1 with `login: codex` failed, and a plain `treebox doctor --repo "$REPO" --json` still exits 0 (`ok` true, the codex login row is optional). Move the file back.
- **Keychain.** Remove the login file: `rm "$E2E_HOME/.claude/.credentials.json"`. `treebox doctor --repo "$REPO" --json` exits 1, `login: claude` `no login at …` (the fixture's `security` finds no item). Make a fake `security` that prints a login and records what it was asked: `mkdir "$SBX/kc" && printf '%s\n' '#!/bin/sh' "echo \"\$@\" >> \"$SBX/kc/asked\"" "echo '{\"claudeAiOauth\": {\"fake\": true}}'" > "$SBX/kc/security" && chmod +x "$SBX/kc/security"`. `E2E_PATH="$SBX/kc:$SBX/stub-bin:$SBX/toolbox:/usr/bin:/bin:/usr/sbin:/sbin" treebox doctor --repo "$REPO" --json` exits 0, `login: claude` `macOS Keychain (Claude Code-credentials)`, and `$SBX/kc/asked` reads `find-generic-password -a <user> -s Claude Code-credentials -w`. macOS only.
- **Help.** `treebox --help` exits 0 and lists `create`, `enter`, `list`, `teardown`, `doctor`, `version`, `template`, but not `ls` or `rm`, and ends with the exit-code table. Bare `treebox` exits 2 and prints the same help, on stdout. `treebox create --help` and `treebox template init -h` exit 0.
- **Completion.** `_TREEBOX_COMPLETE=complete_bash COMP_WORDS="treebox cre" COMP_CWORD=1 treebox` exits 0 and prints `create`. `_TREEBOX_COMPLETE=complete_bash COMP_WORDS="treebox template " COMP_CWORD=2 treebox` prints exactly `init`, `list`, `path` (not `ls`).

## Gotchas

- `e2e_doctor` is the fixture's health check; `treebox doctor` is the product feature. Run the first before every drive, drive the second as a feature.
- On macOS, removing the login file makes treebox read the Keychain. The fixture's `security` always says "no item" so the real Keychain is never read; a fake that prints a login must come first on `E2E_PATH`, never on your own `PATH`.
- `E2E_PATH` refuses a `claude` or `codex` that is not the stub, so a `PATH` with `~/.local/bin` exits 125 before treebox runs. On macOS it also refuses a `PATH` whose first `security` is the real `/usr/bin/security`: start it with `$SBX/safe-bin`.
- The Keychain is read only when the login file is absent. A login file that holds `{}` is `no login at …` with no Keychain read.
- A hard failure prints `✗ blocked: <rows>` and exits 1; only git, repo, the selected agent and login, and isolation are hard checks.
- `--show-completion` and `--install-completion` detect the parent shell. Through the fixture's launcher the parent is not a shell, so they print `Shell  not supported.` and exit 1; the evidence run's A7 is SKIP on this Mac for the same reason. Not driven here.
- Scenarios: A1, A2, A3, A4, A5, A6, A7, A8, A9, A10.

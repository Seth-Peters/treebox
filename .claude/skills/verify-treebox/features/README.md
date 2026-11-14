# Treebox verification map

This directory is the maintained source for driving each user-facing feature of the treebox CLI by hand and proving it works.
Read this index first, then use the matching feature file as the recipe; `../references/routes.md` maps every route to its feature, source and scenarios.

## Baseline preconditions

- Work from the treebox checkout under test, in **bash**: `source .claude/skills/verify-treebox/fixture.sh`. In a new shell, attach to the same fixture with `source .claude/skills/verify-treebox/fixture.sh --attach "$SBX"`.
- The fixture is a throwaway bare origin and clone (`$REPO`: a uv project, `main` and `dev` on `origin`, an untracked `.env` with `SECRET=canonical`), a worktree root `$ROOT`, a scratch `HOME`, `TREEBOX_HOME` and `TREEBOX_CONFIG` (an empty file), and the offline docker template `e2e-offline`.
- `treebox` on `PATH` is this checkout's build (`$SBX/bin/treebox`), with the controlled stub agent first on its `PATH`. Run `e2e_doctor` and require every line `ok` before the first drive.
- Docker features need `engine: reachable` in `e2e_doctor` and the offline template's base image; without them they are SKIP.
- Never drive the real repo, the real `~/.treebox`, `~/.claude`, `~/.codex` or the macOS Keychain, and never launch a real agent.

## Driving conventions

- Start every feature from a fresh fixture with its file's `Preconditions:`. A failed drive leaves state; `e2e_cleanup`, then a new fixture.
- Every command is literal and is what a user types. Always pass `--repo "$REPO" --root "$ROOT"` to commands that act on worktrees.
- Use `--json` for assertions and read the field (`jq`, or `python3 -c 'import json,sys; …'`); exact human text belongs to `tests/golden/`.
- Variables in front of a command (`NO_COLOR=1 treebox …`, `TREEBOX_CONFIG=… treebox …`) pass through to treebox. To change the `PATH` treebox sees, set `E2E_PATH` and start it with `$SBX/safe-bin` (the fake Keychain tool alone); a `claude` or `codex` on it that is not the stub, or the real macOS `security`, is refused (exit 125).
- Give every launch (a `create` or `enter` without `--json`, `--print` or `--dry-run`) `</dev/null` in scripts, so the agent cannot read the script.
- Write `$TREEBOX_CONFIG` for one bullet, then reset it with `: > "$TREEBOX_CONFIG"`.
- Name every docker worktree `e2e-…`.

## Proof and skip reporting

- CLI proof is the command, stdout, stderr, the exit code, and the resulting state: files under `$ROOT` and `$TREEBOX_HOME`, `git -C "$REPO" worktree list` and branches, and `docker ps` / `docker inspect` for docker features.
- Prove a mutation with a second, read-only view (`treebox list --json`, the record under `$ROOT/.records/`, `docker inspect`), not only with the command's own output.
- Record the feature file and the sub-feature id with every result.
- A bullet that needs a missing prerequisite (an engine, uid 0, macOS) is SKIP with the prerequisite named, never PASS.
- Do not report an entry point as verified through a different one (a `--print` run does not prove a launch).

## Feature entry contract

Each feature file starts with an H1 title, one paragraph on the user-visible behavior, and a `Used with:` line that links the features it depends on or that use it. It then has exactly four H2 sections, in this order:

1. `Sub-features`: short ids in backticks, one line each.
2. `How to get to it (user POV)`: every user entry point (commands, aliases, flags, config keys, environment variables, template files).
3. `Driving it with treebox`: starts with `Preconditions:`, then labeled bullets that pair a user action with the exact command and the observable result (exit code, JSON field, file or container state).
4. `Gotchas`: traps that waste or invalidate a run. Its last bullet lists the scenario ids.

Keep implementation details out of feature files: user paths, stable handles, required state, commands and observable proof only. Scenario text lives in `../references/scenarios.md`; source symbols live in `../references/routes.md`.

## Features

- [Create](./create.md) covers named, placeholder and checkout creates, base, harness, print, JSON, launch, agent args, dry run, conflicts, invalid input, fetch, setup failure and resume, the pre-push guard, submodules, output modes and parallel creates.
- [Enter](./enter.md) covers re-launch, `.env` refresh, dependency re-sync, ref resolution, agent args, recorded choices, the host-owned record and broken worktrees.
- [List](./list.md) covers the table, the JSON fields, health of every kind of registration, empty roots and the `ls` alias.
- [Teardown](./teardown.md) covers confirmation, force, branch deletion, many refs, exact paths, refusals, corrupt worktrees, the prompt and the chooser.
- [Template](./template.md) covers listing, locating, scaffolding, `--from` and `--force`, `TREEBOX_TEMPLATE_DIR`, and template resolution before provisioning.
- [Doctor, version and help](./doctor.md) covers the doctor checks for host, docker and each harness, the Keychain login, version, help and shell completion.
- [Config](./config.md) covers the config file location, defaults, precedence, validation, caches and the treebox home.
- [Docker sandbox](./docker-sandbox.md) covers the docker create, entry, launch, isolation of the sandbox config, revive, re-create, restart, cancel, teardown and preflight.
- [Firewall](./firewall.md) covers the firewall plan, its template files, the run before setup and on every entry, and its failure.
- [Resource limits](./resource-limits.md) covers CPU, memory and process limits: set, precedence, validation, plan, enter and drift.
- [Identity and ports](./identity-and-ports.md) covers `TREEBOX_NAME`, `TREEBOX_ID` and the port blocks: reserve, exhaust, release and the ledger.
- [Unattended use](./unattended.md) covers non-interactive mode, missing login or agent, the agent exit line, cancellation, streams and exit codes.
- [Remote workers](./remote-workers.md) covers the worker-side coordinator contract: profiles, path refs, status, retries and the failure classes.

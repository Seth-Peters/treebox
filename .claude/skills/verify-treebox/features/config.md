# Config

The user-level `config.toml` gives defaults for every new worktree: base branch, root, harness, isolation, `.env` source, setup commands, caches, firewall, template, resource limits and port range. A flag always wins. The vocabulary is closed, a bad value is a loud usage error, and treebox never reads a config from the target repo. `TREEBOX_HOME` holds the config, templates and port ledger.

Used with: [Create](./create.md) applies the defaults, [Enter](./enter.md) prefers the record over them, [Template](./template.md), [Firewall](./firewall.md), [Resource limits](./resource-limits.md) and [Identity and ports](./identity-and-ports.md) have their own keys, [Docker sandbox](./docker-sandbox.md) is chosen by `isolation`, [Remote workers](./remote-workers.md) use one config per repository as a profile.

## Sub-features

- `config-location`: `TREEBOX_CONFIG`, else `$TREEBOX_HOME/config.toml`; a set but missing file is an error.
- `config-defaults`: `base`, `root`, `harness`, `env_file`, `setup_hook` and `[caches]` apply to a new create.
- `config-precedence`: an explicit flag beats the config where a flag exists (`--isolation`, `--harness`, `--base`, `--root`, `--firewall`, `--template`, the limit flags); the record beats it on enter.
- `config-validation`: an unknown key, a wrong type or a bad value is `INVALID_CONFIG` for every command.
- `config-caches`: `[caches]` and `XDG_CACHE_HOME` choose where setup writes its cache.
- `config-home`: everything treebox writes for the user lives under `TREEBOX_HOME`.

## How to get to it (user POV)

- The file `$TREEBOX_CONFIG`, else `$TREEBOX_HOME/config.toml`, else `~/.treebox/config.toml`.
- Top-level keys: `isolation`, `harness`, `base`, `root`, `env_file`, `firewall`, `template`, `setup_hook`. Tables: `[caches]`, `[resources]`, `[ports]`.
- Environment: `TREEBOX_CONFIG`, `TREEBOX_HOME`, `XDG_CACHE_HOME` (and the ecosystems' own cache variables such as `UV_CACHE_DIR`).

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. The fixture's `$TREEBOX_CONFIG` is an empty file. Write it for one bullet, then reset it with `: > "$TREEBOX_CONFIG"`. No engine is needed.

- **Missing file.** Run `TREEBOX_CONFIG="$SBX/nope.toml" treebox list --repo "$REPO" --root "$ROOT" --json`. Exit 2 `INVALID_CONFIG`, `$TREEBOX_CONFIG points at a missing config file: …`, the hint says to fix or unset it.
- **Default and override.** `echo 'base = "dev"' > "$TREEBOX_CONFIG"`. `treebox create cfg-base --repo "$REPO" --root "$ROOT" --json` exits 0 with `base` `dev`; `treebox create cfg-override --base main --repo "$REPO" --root "$ROOT" --json` exits 0 with `base` `main`.
- **Bad value.** `echo 'isolation = "qemu"' > "$TREEBOX_CONFIG"`, then `treebox list --repo "$REPO" --root "$ROOT" --json`. Exit 2 `INVALID_CONFIG`, `Invalid isolation mode 'qemu'. Use one of ('host', 'docker').`
- **Closed vocabulary.** `echo 'isolatoin = "host"' > "$TREEBOX_CONFIG"`: the same list exits 2 `INVALID_CONFIG`, `unknown key 'isolatoin' (did you mean 'isolation'?)`. `echo 'firewall = 1' > "$TREEBOX_CONFIG"`: exit 2, `firewall must be a boolean (true/false), got int.` `printf '[resources]\nmemory = "lots"\n' > "$TREEBOX_CONFIG"`: exit 2, the message contains `Invalid [resources] memory 'lots'`.
- **Harness is recorded.** `echo 'harness = "codex"' > "$TREEBOX_CONFIG"`, `treebox create g5 --repo "$REPO" --root "$ROOT" --print` ends in `exec codex …`. Reset the config; `treebox enter g5 --repo "$REPO" --root "$ROOT" --print` still ends in `exec codex …`.
- **Root.** `echo 'root = "trees"' > "$TREEBOX_CONFIG"`, then `treebox create g6 --repo "$REPO" --json` (no `--root`). Exit 0, `worktree_path` `$REPO/trees/g6`.
- **env_file.** `echo SECRET=from-file > "$SBX/secrets.env"` and `echo "env_file = \"$SBX/secrets.env\"" > "$TREEBOX_CONFIG"`, then `treebox create g7 --repo "$REPO" --root "$ROOT" --json`. `$ROOT/g7/.env` reads `SECRET=from-file`.
- **setup_hook.** `echo 'setup_hook = ["echo hook-ran > hook.txt"]' > "$TREEBOX_CONFIG"`, then `treebox create g8 --repo "$REPO" --root "$ROOT" --json`. `$ROOT/g8/hook.txt` reads `hook-ran` and there is no `$ROOT/g8/.venv`: the hook replaces the detected setup.
- **Caches.** `printf '[caches]\nuv = "%s/uv-cache"\n' "$SBX" > "$TREEBOX_CONFIG"`, then `treebox create g9 --repo "$REPO" --root "$ROOT" --json`: `$SBX/uv-cache` holds uv's cache (`CACHEDIR.TAG`, …). With an empty config, `XDG_CACHE_HOME="$SBX/xdg" treebox create g9b --repo "$REPO" --root "$ROOT" --json`: `$SBX/xdg/uv` exists. `UV_CACHE_DIR` beats `XDG_CACHE_HOME`: `UV_CACHE_DIR="$SBX/uvc" XDG_CACHE_HOME="$SBX/xdg2" treebox create g9c --repo "$REPO" --root "$ROOT" --json` fills `$SBX/uvc` and makes no `$SBX/xdg2/uv`.
- **Home.** After the bullets above, `ls "$TREEBOX_HOME"` shows `config.toml`, `ports.json`, `ports.lock` and `templates`, and `~/.treebox` does not exist (`e2e_doctor` checks the scratch home).

## Gotchas

- The config is read and validated by `create`, `enter`, `list`, `teardown`, `doctor` and `template list`; a broken config blocks all of them until you reset it (`version`, `template path`, `template init` and `--help` still run). Reset after each bullet.
- `root` and `env_file` are repo-relative unless absolute or `~`-prefixed.
- A config default never changes an existing worktree: the record decides isolation, firewall, harness, template and limits ([Enter](./enter.md)).
- `XDG_CACHE_HOME` and `UV_CACHE_DIR` pass through the fixture's launcher from your shell. If your shell sets it, setup writes to your real cache; `e2e_doctor` prints a note when one is set.
- A config in the target repo is never read; there is nothing to drive for it.
- Scenarios: G1, G2, G3, G4, G5, G6, G7, G8, G9, G12, X5.

# Resource limits

Sets CPU, memory and process limits for one docker sandbox, so one task cannot starve the others. They are set when a docker sandbox is created, recorded, and fixed for its life: enter only restates them, and a later config change is reported as drift, never applied.

Used with: [Create](./create.md) sets them, [Docker sandbox](./docker-sandbox.md) applies them, [Enter](./enter.md) keeps them, [Config](./config.md) gives defaults, [Template](./template.md) gives the lowest tier, [List](./list.md) shows them.

## Sub-features

- `limits-set`: flags on `create` apply to the container and are recorded.
- `limits-precedence`: flag, then `[resources]` in config, then the template's `resources`, field by field.
- `limits-invalid`: a bad value stops before anything is made, with or without an engine.
- `limits-host`: the host runner refuses a limit flag; config limits never refuse a host worktree.
- `limits-plan`: `--dry-run` shows the limits and the `docker run` flags without an engine.
- `limits-enter`: the same value on enter is accepted; another is a conflict.
- `limits-drift`: a config change after create is reported in `resources_drift` and one warning.

## How to get to it (user POV)

- `treebox create NAME --isolation docker --cpus N --memory SIZE --pids-limit N`
- The same flags on `treebox enter REF` (restate only).
- `[resources]` in `config.toml`: `cpus`, `memory`, `pids`.
- The template's `container.json` `resources` object.

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. **Invalid**, **Host** and **Plan** need no engine; the rest needs `engine: reachable`.

- **Host.** `treebox create b26 --repo "$REPO" --root "$ROOT" --memory 4g --json` exits 2 `RESOURCE_UNSUPPORTED`, the hint says `use a sandbox: --isolation docker`.
- **Invalid.** `treebox create e2e-b26 --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --memory lots --json`, the same with `--cpus 0` and with `--pids-limit -5`: exit 2 `INVALID_RESOURCE` each, and `$ROOT/e2e-b26` is never made. With an engine, `--cpus 1000` is exit 2 `INVALID_RESOURCE`, `the Docker engine has only N CPUs`.
- **No docker.** `E2E_PATH="$SBX/stub-bin:/usr/bin:/bin" treebox create e2e-b26 --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --cpus 1 --json` exits 1 `MISSING_DEPENDENCY`, the same as without a limit.
- **Plan.** `treebox create e2e-plan --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --cpus 1.5 --memory 512m --pids-limit 64 --dry-run --json` exits 0 with `resources` `{cpus: 1.5, memory: "512m", pids: 64}`, and its `docker run` command carries `--cpus 1.5 --memory 512m --pids-limit 64`. With `E2E_PATH="$SBX/stub-bin:/usr/bin:/bin"` (no docker) it also exits 0.
- **Set.** `treebox create e2e-lim --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --cpus 1 --memory 512m --pids-limit 64 --json` exits 0 with `resources` `{cpus: 1, memory: "512m", pids: 64}`. `docker inspect -f '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' <ctr>` prints `1000000000 536870912 64`, and `list --json` shows the same `resources`.
- **Enter.** `treebox enter e2e-lim --repo "$REPO" --root "$ROOT" --memory 512m --json` exits 0, `resources_drift` all `null`. `--memory 1g --json` exits 5 `RESOURCE_MISMATCH`, `error.path` is the record, and the hint gives `treebox teardown e2e-lim && treebox create e2e-lim --checkout e2e-lim … --memory 1g …`.
- **Config tier.** `printf '[resources]\nmemory = "2g"\npids = 128\n' > "$TREEBOX_CONFIG"`. `treebox create e2e-g16 --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --dry-run --json` has `resources` `{cpus: null, memory: "2g", pids: 128}`; with `--memory 1g`, `memory` is `1g` and `pids` stays `128`. `treebox create g16-host --repo "$REPO" --root "$ROOT" --json` (host) exits 0 with all `null`.
- **Drift.** `printf '[resources]\nmemory = "2g"\n' > "$TREEBOX_CONFIG"`, `treebox create e2e-lim-cfg --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --json` (`memory` `2g`), then `printf '[resources]\nmemory = "3g"\n' > "$TREEBOX_CONFIG"` and `treebox enter e2e-lim-cfg --repo "$REPO" --root "$ROOT" --json`: exit 0, `resources_drift.memory` `3g`, and one warning `limits: this sandbox keeps its recorded limits (memory 2g); …`. `treebox enter e2e-lim --repo "$REPO" --root "$ROOT" --json` (limits from flags) has `resources_drift` all `null` and no warning. Reset with `: > "$TREEBOX_CONFIG"`.

## Gotchas

- `--dry-run` does not check `--cpus` against the engine, because it needs no engine.
- A bad `[resources]` value is `INVALID_CONFIG` for every command, also `list` ([Config](./config.md)); a bad template `resources` object is `TEMPLATE_INVALID`.
- Limits chosen by flags are not drift when the config changes; only limits that came from config or the template are compared.
- A removed container is re-created with the recorded limits on enter ([Docker sandbox](./docker-sandbox.md), H9).
- Scenarios: B26, B23, H9, H12, G16.

# Template

A template is the operator-owned definition of a docker sandbox: its `Dockerfile`, `container.json`, `post-create.sh` and firewall files. treebox ships a bundled `default`, scaffolds named copies under `$TREEBOX_HOME/templates/<name>`, lists and locates them, and resolves the one a docker create asks for before anything is made. A template is never read from the target repo.

Used with: [Docker sandbox](./docker-sandbox.md) builds from it, [Create](./create.md) and [Enter](./enter.md) take `--template`, [Firewall](./firewall.md) files live in it, [Resource limits](./resource-limits.md) can come from its `resources`, [Config](./config.md) sets the default name.

## Sub-features

- `template-list`: every template with its source, firewall and status; the default is marked.
- `template-path`: the directory of a template, scriptable; a name is an identifier, never a path.
- `template-init`: scaffold the full file set, from the bundled default or `--from` another template.
- `template-force`: `--force` replaces an existing template all-or-nothing.
- `template-override`: `TREEBOX_TEMPLATE_DIR` wins for every name.
- `template-resolve`: a docker create resolves and validates the template before it provisions.

## How to get to it (user POV)

- `treebox template list [--json]`, alias `treebox template ls`.
- `treebox template path [NAME]`
- `treebox template init NAME [--from NAME] [--force] [--json]`
- `create --template NAME`, `enter --template NAME`; `template = "NAME"` in `config.toml`.
- `TREEBOX_TEMPLATE_DIR` (one template dir for every name) and `TREEBOX_HOME` (where named templates live).

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. The fixture installs `e2e-offline` in `$TREEBOX_HOME/templates`. No engine is needed.

- **List.** Run `treebox template list`. Exit 0, rows `default (default)  bundled  yes  ● ok` and `e2e-offline  user  -  ● ok`, a list of what the bundled default includes, and `Summary: 2 templates`. `--json`: `templateDirOverride` null, `templates[]` with `name`, `path`, `source`, `default`, `valid`, `missing`, `invalid`, `firewall`. `treebox template ls --json` prints the same bytes.
- **Path.** `treebox template path` and `treebox template path default` exit 0 and print the bundled template dir (under `src/treebox/assets/container` in this checkout). `treebox template path ghost` exits 3 (`No template named 'ghost'`); `treebox template path /etc` and `treebox template path ../x` exit 2 (`never a path`).
- **Init.** Run `treebox template init mytpl --json`. Exit 0, `template` `{name: "mytpl", from: "default", valid: true, missing: []}`. `$TREEBOX_HOME/templates/mytpl` holds `Dockerfile`, `container.json`, `post-create.sh`, `firewall.json`, `init-firewall.sh`, `allowed-domains.sh`, `treebox-fetch`, `treebox-online`.
- **Exists and force.** `treebox template init mytpl --json` again exits 5 `TEMPLATE_EXISTS`, the hint names `--force`. Mark it (`echo "# marker" >> "$TREEBOX_HOME/templates/mytpl/Dockerfile"`). `treebox template init copytpl --from mytpl --json` exits 0 and the copy's `Dockerfile` ends with `# marker`. `treebox template init mytpl --force --json` exits 0 and the marker is gone.
- **Bad source and name.** `treebox template init x --from ghost --json` exits 3 `TEMPLATE_NOT_FOUND`, and no `x` dir exists. `treebox template init ../escape --json` exits 2 `INVALID_NAME`. `treebox template init mytpl --from mytpl --force --json` exits 2 `TEMPLATE_CONFLICT`.
- **All-or-nothing.** `chmod 555 "$TREEBOX_HOME/templates"`, then `treebox template init denied --json`: exit 1 `COPY_FAILED`. With `copytpl` marked and the dir still `555`, `treebox template init copytpl --force --json`: exit 1 `OVERWRITE_FAILED`, and `shasum` of its `Dockerfile` is the same before and after. `chmod 755 "$TREEBOX_HOME/templates"` after.
- **Override.** `mkdir "$SBX/tdir"`, then `TREEBOX_TEMPLATE_DIR="$SBX/tdir" treebox template list --json` exits 0 with `templateDirOverride` `$SBX/tdir`, and `TREEBOX_TEMPLATE_DIR="$SBX/tdir" treebox template path any-name` prints `$SBX/tdir`.
- **Resolve before provisioning.** Copy `mytpl` to `broken` and write `{bad` into its `container.json`. `treebox create e2e-broken --repo "$REPO" --root "$ROOT" --isolation docker --template broken --json` exits 1 `TEMPLATE_INVALID` (the hint names `treebox template path broken`) and `$ROOT` was never made. `treebox create e2e-ghost … --isolation docker --template ghost --json` exits 3 `TEMPLATE_NOT_FOUND`, the hint names `treebox template init ghost`. Neither needs an engine.

## Gotchas

- `template path` has no `--json`; `treebox template path ghost --json` is a usage error (exit 2), not the not-found exit 3.
- Under host isolation an unknown `--template` is accepted (`treebox create h-ghost --template ghost --json` exits 0): templates are docker-only.
- `--force` and `--from` act on `$TREEBOX_HOME/templates` only. Never let a drive run with the real `~/.treebox`; `e2e_doctor` checks the scratch home.
- The permission checks need a non-root user: root ignores file modes.
- Building the bundled `default` needs the network and about 5.6 GB. Drive docker features with `--template e2e-offline`.
- Scenarios: F1, F2, F3, F4, F5, F6, F7, F8, F9, B21, H3, G11, G13, H11, H18.

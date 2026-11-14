# Firewall

The firewall is a default-deny egress lockdown for one docker sandbox. It is set when the sandbox is created and recorded, it runs before any workspace code (the template's post-create), and it runs again on every entry, also after a container restart outside treebox. If it fails, nothing is emitted and the agent does not start.

Used with: [Create](./create.md) sets it and [Docker sandbox](./docker-sandbox.md) applies it, [Enter](./enter.md) re-applies it, [Template](./template.md) holds its `firewall.json` (optional) and the `init-firewall.sh` and `allowed-domains.sh` every template must have, [Config](./config.md) gives the default.

## Sub-features

- `firewall-plan`: the flag or the config puts `--cap-add=NET_ADMIN` and the `init-firewall.sh` exec in the plan; `--no-firewall` beats the config.
- `firewall-template`: `--firewall` needs a template with a `firewall.json`.
- `firewall-order`: the first run happens before the post-create setup.
- `firewall-entry`: every enter runs it again, also on a running container and after `docker restart`.
- `firewall-failed`: a failing script is `FIREWALL_FAILED`; no command is emitted and the agent does not launch.
- `firewall-recorded`: on enter the recorded firewall wins over the config (enter has no firewall flag).

## How to get to it (user POV)

- `treebox create NAME --isolation docker --firewall` or `--no-firewall`.
- `firewall = true` in `config.toml`.
- The template's `firewall.json` (merged onto `container.json` when the firewall is on), `init-firewall.sh`, `allowed-domains.sh` ([Template](./template.md)).

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` all `ok`. **Plan** needs no engine; the rest needs `engine: reachable` and a firewall test template. Make it from the offline template: its `init-firewall.sh` only counts its runs and fails on demand, so it proves when treebox runs the firewall, not what a real one blocks.

```bash
T="$TREEBOX_HOME/templates/e2e-fw"; cp -RL "$TREEBOX_HOME/templates/e2e-offline" "$T"
echo '{"env": {"TREEBOX_FIREWALL": "1"}}' > "$T/firewall.json"
printf '%s\n' '#!/bin/sh' \
  'if [ -f /tmp/fw-fail ]; then echo "ERROR: injected firewall failure"; exit 1; fi' \
  'when=before-setup; [ -f /tmp/treebox-e2e-post-create ] && when=after-setup' \
  'echo "$when" >> /tmp/fw-runs' > "$T/init-firewall.sh"
printf 'COPY init-firewall.sh /usr/local/bin/init-firewall.sh\nRUN chmod 0755 /usr/local/bin/init-firewall.sh\n' >> "$T/Dockerfile"
```

`treebox template list --json` then shows `e2e-fw` with `valid` true and `firewall` true.

- **Plan.** `echo 'firewall = true' > "$TREEBOX_CONFIG"`, then `treebox create e2e-fw --repo "$REPO" --root "$ROOT" --isolation docker --dry-run --json`: exit 0 and `commands` contains `--cap-add=NET_ADMIN` and `init-firewall.sh`. With `--no-firewall` added, neither appears. With an empty config and `--firewall`, they appear again.
- **Template without a firewall.** `treebox create e2e-fw --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --firewall --json` exits 1 `TEMPLATE_INVALID`, `Template 'e2e-offline' has no firewall.json`.
- **Before setup.** Run `treebox create e2e-fw --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-fw --firewall --json`. Exit 0. `docker exec <ctr> cat /tmp/fw-runs` prints `before-setup` then `after-setup`: the first run precedes the post-create, the second is the entry.
- **Every entry.** `treebox enter e2e-fw --repo "$REPO" --root "$ROOT" --print` exits 0 and `/tmp/fw-runs` has 3 lines. `docker restart -t 1 <ctr>`, then the same enter: exit 0 and 4 lines.
- **Failure.** `docker exec <ctr> touch /tmp/fw-fail`, then `treebox enter e2e-fw --repo "$REPO" --root "$ROOT" --json`: exit 1 `FIREWALL_FAILED`, the message ends with `ERROR: injected firewall failure`, the hint says `the agent was not launched` and names `treebox template path e2e-fw`, and stdout is empty. A plain `treebox enter e2e-fw --repo "$REPO" --root "$ROOT" </dev/null` exits 1 and stdout has no stub record.
- **Recorded.** `"$ROOT/.records/e2e-fw.json"` has `"firewall": true` and `"template": "e2e-fw"`; enter uses them whatever the config says.

## Gotchas

- The offline template has no `firewall.json` on purpose: `--firewall` with it is `TEMPLATE_INVALID`. Use the `e2e-fw` copy above.
- The counting script blocks nothing. What a real firewall blocks (egress lockdown, no blanket host-gateway `ACCEPT`, rules back after a restart) needs the bundled image: H18, opt-in with `TREEBOX_E2E_BUNDLED_IMAGE=1`, 5.6 GB and the network.
- iptables rules do not survive a container restart; that is why every entry runs the script. A count that does not grow after `docker restart` is a product bug.
- The failure hint prints the full re-create command with the fixture's `--repo` and `--root`; it is safe to copy into the fixture.
- Scenarios: G10, H14, H18.

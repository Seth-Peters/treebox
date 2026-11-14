# Remote workers

A coordinator runs treebox on a worker machine over SSH and reads machine-readable results. The whole lifecycle runs on the worker: the coordinator calls `create`, `enter`, `list` and `teardown` there, unattended, with one config file per repository as a profile, addresses each task's worktree by its absolute path, and tells agent failures, cancellations and treebox failures apart by the exit status, the agent exit line and `error.code`. treebox has no SSH client, daemon or queue of its own.

Used with: [Unattended use](./unattended.md) is the contract it uses, [Create](./create.md) and [Teardown](./teardown.md) are its retried requests, [Enter](./enter.md) runs a task by path, [List](./list.md) is its status, [Docker sandbox](./docker-sandbox.md), [Firewall](./firewall.md) and [Resource limits](./resource-limits.md) are its isolation, [Identity and ports](./identity-and-ports.md) give each task its `id` and ports, [Config](./config.md) is the profile.

## Sub-features

- `remote-profile`: `TREEBOX_CONFIG` selects one profile per repository; `TREEBOX_HOME` stays shared.
- `remote-create`: `create NAME --json` gives the `worktree_path` the coordinator stores; a retry is `SLUG_CONFLICT`, never a second worktree.
- `remote-path-refs`: `enter` and `teardown` by absolute path act only on that worktree; a retry after removal is `NOT_FOUND`.
- `remote-status`: `list --json`, matched by `path`, gives `health`, `setup`, `id` and `port_base`.
- `remote-failure-classes`: agent failure (the exit line), cancelled (`128 + signal`), treebox failure (any other non-zero, `error.code`).
- `remote-repo`: `--repo` takes a path on the worker; a URL is `NOT_A_REPO`.

## How to get to it (user POV)

- On the worker, through the coordinator's SSH: `TREEBOX_NON_INTERACTIVE=1 TREEBOX_CONFIG=~/profiles/<repo-id>.toml treebox create|enter|list|teardown … --repo <clone>`.
- The worker check: `treebox --non-interactive doctor --isolation docker --json`.
- The contract: `docs/remote-workers.md`.

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` with `engine: reachable`. These bullets drive the worker side locally: the fixture is the worker and your shell is the coordinator. The SSH transport, a remote worker machine and the detached-run helper are not driven here. Make a profile and set unattended mode for every call:

```bash
printf 'isolation = "docker"\ntemplate = "e2e-offline"\n[resources]\nmemory = "1g"\npids = 256\n' > "$SBX/profile.toml"
export TREEBOX_NON_INTERACTIVE=1
```

- **Worker check.** `TREEBOX_CONFIG="$SBX/profile.toml" treebox doctor --repo "$REPO" --isolation docker --json` exits 0 with `ok` true.
- **Create.** `TREEBOX_CONFIG="$SBX/profile.toml" treebox create e2e-t-1 --repo "$REPO" --root "$ROOT" --json` exits 0. Store `worktree_path` (`$ROOT/e2e-t-1`); `resources` is `{cpus: null, memory: "1g", pids: 256}` from the profile. The same request again exits 5 `SLUG_CONFLICT`.
- **Status.** `treebox list --repo "$REPO" --root "$ROOT" --json`, the row whose `path` is the stored path: `health` `healthy`, `setup` `done`, `isolation` `docker`, and the same `id` and `port_base` as the create.
- **Agent failure.** `TREEBOX_CONFIG="$SBX/profile.toml" treebox enter "$ROOT/e2e-t-1" --repo "$REPO" --root "$ROOT" -- --stub-exit=4 </dev/null` exits 4 and the last stderr line is `treebox: agent exited with status 4`. Without the stub arg it exits 0 and the stub says `in_container=yes`.
- **Cancelled.** The same enter with `-- --stub-sleep=60` in the background, then `kill -TERM` its pid after about 4 s: exit 143, stderr `Cancelled by SIGTERM`.
- **treebox failure by path.** `TREEBOX_CONFIG="$SBX/profile.toml" treebox enter "$ROOT/e2e-t-9" --repo "$REPO" --root "$ROOT" --json` exits 3 `NOT_FOUND`: a path never matches `e2e-t-1` by substring.
- **Teardown and retry.** `TREEBOX_CONFIG="$SBX/profile.toml" treebox teardown "$ROOT/e2e-t-1" --repo "$REPO" --root "$ROOT" --yes --json` exits 0, `complete` true, and `$TREEBOX_HOME/ports.json` has no block left. The same request again exits 3 `NOT_FOUND` and touches nothing.
- **Repo is a path.** `treebox create t-x --repo https://example.invalid/repo.git --json` exits 2 `NOT_A_REPO`.

## Gotchas

- The SSH worker is not part of the fixture. `ssh` exit 255, a dropped connection and a worker restart (runs lost or `CANCELLED`) are the coordinator's side of the contract; read them in `docs/remote-workers.md`, they are not driven here.
- `TREEBOX_NON_INTERACTIVE` passes through the fixture's launcher; `unset TREEBOX_NON_INTERACTIVE` when you are done, or later drives in this shell refuse every prompt.
- Name ref retries are unsafe: after `t-1` is removed, `teardown t-1` can match `t-12`. Always store and use the path.
- A sandbox that stopped or restarted under the agent is a treebox failure (exit 1), not an agent status ([Docker sandbox](./docker-sandbox.md), H15).
- `teardown --yes` keeps the template's per-worktree volume (`treebox-e2e-vol-e2e-t-1` here); add `--remove-volumes` when the task's volume must go too. `e2e_cleanup` reaps it in the fixture.
- `TEARDOWN_INCOMPLETE` keeps the worktree, record and port block so the same teardown retries (H10).
- Scenarios: C18, H10, H15, H17, U3, U4, P2.

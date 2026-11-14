# Docker sandbox

With `--isolation docker` the worktree is provisioned on the host as usual, and the agent then runs in a container built from an operator template: the worktree and its git common dir are mounted at their host paths, the shared hooks are read-only, the agent gets staged copies of its login, and the sandbox definition lives outside the mount. Enter revives a stopped container and re-creates a removed one from the record; teardown removes what the runner-owned manifest names.

Used with: [Create](./create.md) and [Enter](./enter.md) drive it, [Template](./template.md) defines it, [Firewall](./firewall.md) and [Resource limits](./resource-limits.md) are applied to it, [Teardown](./teardown.md) removes it, [Doctor](./doctor.md) checks the engine, [Unattended use](./unattended.md) is the launch contract, [Remote workers](./remote-workers.md) run it on a worker.

## Sub-features

- `docker-create`: the host-side provisioning, then image build, container start and the template's post-create, as the host uid.
- `docker-entry`: the launch command is a `docker exec` into the container, never a host shell.
- `docker-launch`: a real launch runs the agent in the container, with the worktree at its host path.
- `docker-isolation`: the sandbox config and staged credential copies live under `$ROOT/.containers/<name>/`, outside the worktree; the shared hooks are read-only.
- `docker-revive`: enter starts a stopped container before it emits a command.
- `docker-recreate`: enter re-creates a removed container from the record, limits included.
- `docker-restart`: a sandbox that stops or restarts under the agent is a treebox failure, not an agent exit.
- `docker-harness`: an agent missing in the image is a classified failure.
- `docker-cancel`: a cancel signal stops the agent in the container; the container keeps running.
- `docker-teardown`: teardown removes container and image, volumes with `--remove-volumes`, nothing with `--skip-container`.
- `docker-preflight`: no docker CLI, no engine, or uid 0 fails before anything is made.

## How to get to it (user POV)

- `treebox create NAME --isolation docker [--template NAME]`; `isolation = "docker"` in `config.toml`.
- `treebox enter REF` (the recorded isolation applies); `treebox teardown REF [--remove-volumes] [--skip-container]`.
- `treebox doctor --isolation docker`.
- `TZ` (passed into the image build), `DOCKER_HOST` (which engine).

## Driving it with treebox

Preconditions: the fixture, `e2e_doctor` with `engine: reachable`, and the offline template's pinned base image present (`docker image ls alpine/git`; the evidence run pulls it once). `<ctr>` is the worktree's container, `docker ps --filter name=treebox-<name>- --format '{{.Names}}'`. Name every worktree `e2e-…`. Without an engine every bullet is SKIP with that reason, except **Preflight**.

- **Create.** Run `treebox create e2e-dockered --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --print`. Exit 0 in a few seconds. stderr ends with `credentials  scoped copies staged (outside worktree)`, `image  built`, `container  started`, `setup  post-create complete`. stdout is one `docker exec -i -e TREEBOX_NAME=e2e-dockered … -u agent -w $ROOT/e2e-dockered treebox-e2e-dockered-<id> claude --dangerously-skip-permissions`. `docker ps --filter name=treebox-e2e-dockered-` shows it running, and `docker exec <ctr> cat /tmp/treebox-e2e-post-create` prints `uid=<your uid>` and `cwd=$ROOT/e2e-dockered`. `$ROOT/e2e-dockered/.env` reads `SECRET=canonical`.
- **Outside the mount.** `ls "$ROOT/.containers/e2e-dockered"` lists `Dockerfile`, `container.json`, `post-create.sh`, `credentials`, `treebox-resources.json`, …; none of them is in `$ROOT/e2e-dockered`. `$ROOT/.containers/e2e-dockered/credentials/claude/.credentials.json` is mode `-rw-------` and `docker exec <ctr> cat /home/agent/.claude/.credentials.json` prints the fixture's fake login.
- **Revive.** `docker stop -t 1 <ctr>`, then `treebox enter e2e-dockered --repo "$REPO" --root "$ROOT" --print`. Exit 0 and `docker inspect -f '{{.State.Running}}' <ctr>` is `true` before you use the command. Replaying the printed command (`eval "$cmd" </dev/null`) prints the stub's record with `in_container=yes` and `uid=<your uid>`.
- **Launch.** Run `treebox create e2e-launch --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline </dev/null`. Exit 0, the stub's record says `in_container=yes`, `cwd=$ROOT/e2e-launch`, `uid=<your uid>`, `api_key=unset`. Then `treebox enter e2e-launch --repo "$REPO" --root "$ROOT" -- --stub-exit=3 </dev/null` exits 3 with `arg=--stub-exit=3` and the last stderr line `treebox: agent exited with status 3`.
- **In-container git.** `docker exec -u agent -w "$ROOT/e2e-launch" <ctr> git branch --show-current` prints `e2e-launch` and `git status --short` exits 0. `docker exec -u agent <ctr> touch "$REPO/.git/hooks/planted"` fails with `Read-only file system`, and no `planted` file appears on the host.
- **Re-create.** Run `treebox create e2e-gone --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --cpus 1 --memory 512m --pids-limit 64 --json`, then `docker rm -f <ctr>`, then `treebox enter e2e-gone --repo "$REPO" --root "$ROOT" --json`. Exit 0, and the new container has the recorded limits: `docker inspect -f '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' <ctr>` prints `1000000000 536870912 64`.
- **Agent missing in the image.** `docker exec -u root <ctr> rm /usr/local/bin/codex` in `e2e-launch`, then `treebox enter e2e-launch --repo "$REPO" --root "$ROOT" --harness codex </dev/null`. Exit 1, `'codex' not found in the sandbox of 'e2e-launch'`, the hint names `treebox template path e2e-offline`; no agent exit line. A plain enter (claude) exits 0.
- **Restarted under the agent.** Start `treebox enter e2e-launch --repo "$REPO" --root "$ROOT" -- --stub-sleep=60 </dev/null &`, wait until the stub runs (about 4 s), then `docker restart -t 1 <ctr>`. The enter exits 1 with `The sandbox of 'e2e-launch' stopped or restarted while the agent ran (docker exec exited 137): the agent's result is unknown.` and no agent exit line.
- **Cancel.** The same background enter, then `kill -TERM <treebox pid>`. Exit 143, stderr `Cancelled by SIGTERM` and the hint `The worktree is kept.`; `docker top <ctr>` has no `sleep 60` and the container is still running.
- **Teardown.** `treebox teardown e2e-dockered --repo "$REPO" --root "$ROOT" --force --json` exits 0 with `container` `cleaned`, `volumes_removed` false, `complete` true; no `treebox-e2e-dockered-*` container or image is left and `$ROOT/.containers/e2e-dockered` is gone. On a fresh `e2e-dockered2`, `--force --remove-volumes --json` reports `volumes_removed` true and `treebox-e2e-vol-e2e-dockered2` is gone. On a fresh `e2e-dockered3`, `--force --skip-container --json` reports `container` `skipped`, `removed` true, and the container is still running (remove it with the cleanup).
- **TZ.** `TZ=Europe/Helsinki treebox create e2e-tz --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --dry-run --json` exits 0 and a command in `commands` carries `--build-arg TZ=Europe/Helsinki`.
- **Preflight.** No engine needed. `E2E_PATH="$SBX/stub-bin:/usr/bin:/bin" treebox create e2e-nodocker --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --json` (no `docker` on `PATH`) exits 1 `MISSING_DEPENDENCY` and `$ROOT/e2e-nodocker` is not made. `DOCKER_HOST="unix://$SBX/no-daemon.sock" treebox create e2e-nodaemon --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --json` exits 1 `DOCKER_UNAVAILABLE`, and `DOCKER_HOST="unix://$SBX/no-daemon.sock" treebox doctor --repo "$REPO" --isolation docker --json` exits 1 with `isolation: docker` failed.
- **Root user.** Prerequisite: uid 0. `treebox create e2e-root --repo "$REPO" --root "$ROOT" --isolation docker --template e2e-offline --json` as root exits 1 `RUNNING_AS_ROOT` with nothing provisioned. Not driven on a non-root machine (the Mac and the CI runners); B29 is SKIP there.

## Gotchas

- A launch (`docker exec -i`) reads stdin. In a script that is itself read from stdin (`bash <<EOF`), the launch eats the rest of the script: give every launch `</dev/null`.
- The container and image are `treebox-<name>-<id>`, so only an `e2e-…` name is found by `e2e_reap_docker`. A docker worktree without the prefix leaks into the user's engine.
- `--skip-container` leaves a running container and its image on purpose; `e2e_cleanup` reaps it.
- A teardown without `--remove-volumes` keeps the template's volume (`treebox-e2e-vol-<name>`); `e2e_cleanup` reaps it.
- The offline template has no `uv sync`: dependency setup in a docker sandbox is the template's post-create.
- In the container, the staged credential files show as `root`-owned through Docker Desktop's file sharing; check the mode on the host side.
- `TEARDOWN_INCOMPLETE` needs a docker step that fails on demand; it is the boundary scenario H10 (a fake `docker` that fails `rm`).
- The bundled image (H18) builds 5.6 GB and needs the network; it runs only with `TREEBOX_E2E_BUNDLED_IMAGE=1`.
- Scenarios: H1, H2, H4, H5, H6, H7, H8, H9, H10, H13, H15, H16, H17, H18, B22, B23, B29, G13, R44.

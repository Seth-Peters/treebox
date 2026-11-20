#!/usr/bin/env bash
# Keep Docker e2e optional: an unusable engine must leave the runner on its
# established no-engine path rather than making every Factory task fail.

set -u

readonly E2E_BASE_IMAGE='buildpack-deps:trixie-scm@sha256:4f34e1d673fdb65371de9f88bdd32c4e30152fe692b35604aef5c0b3c0e6da10'
daemon_started=0
runtime_dir=''

warn_skip() {
  printf 'Warning: no usable docker engine; docker scenarios are SKIP (%s)\n' "$1" >&2
}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n "$@"
  else
    return 1
  fi
}

docker_ready() {
  docker info >/dev/null 2>&1
}

stop_started_daemon() {
  if [ "$daemon_started" -eq 1 ] && [ -f "$runtime_dir/dockerd.pid" ]; then
    run_as_root kill "$(cat "$runtime_dir/dockerd.pid")" >/dev/null 2>&1 || true
  fi
}

skip() {
  stop_started_daemon
  [ -z "$runtime_dir" ] || rm -rf "$runtime_dir"
  warn_skip "$1"
  exit 0
}

if ! command -v docker >/dev/null 2>&1 || ! command -v dockerd >/dev/null 2>&1; then
  if ! run_as_root apt-get update --error-on=any \
    || ! run_as_root apt-get install -y --no-install-recommends docker.io; then
    skip 'Docker could not be installed'
  fi
fi

if ! command -v docker >/dev/null 2>&1 || ! command -v dockerd >/dev/null 2>&1; then
  skip 'Docker installation did not provide both docker and dockerd'
fi

if ! docker_ready; then
  runtime_dir="$(mktemp -d)"
  if ! run_as_root sh -c '
    nohup dockerd --host=unix:///var/run/docker.sock --group "$1" >"$2/dockerd.log" 2>&1 &
    printf "%s\n" "$!" >"$2/dockerd.pid"
  ' sh "$(id -g)" "$runtime_dir"; then
    skip 'dockerd could not be started'
  fi
  daemon_started=1

  attempt=0
  while ! docker_ready; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 10 ] || skip 'dockerd did not become reachable within ten seconds'
    sleep 1
  done
fi

if ! docker pull "$E2E_BASE_IMAGE" >/dev/null; then
  skip 'the offline e2e base image could not be pulled'
fi

probe_dir="$(mktemp -d)"
if docker run --rm \
  --mount "type=bind,src=$probe_dir,dst=/probe,readonly" \
  "$E2E_BASE_IMAGE" sh -c 'if touch /probe/blocked; then exit 0; fi; exit 42'; then
  rm -rf "$probe_dir"
  skip 'the read-only bind-mount probe allowed a write'
else
  probe_status=$?
fi

if [ "$probe_status" -ne 42 ]; then
  rm -rf "$probe_dir"
  skip 'the read-only bind-mount probe could not run'
fi

if [ -e "$probe_dir/blocked" ]; then
  rm -rf "$probe_dir"
  skip 'the read-only bind-mount probe wrote to the host directory'
fi

rm -rf "$probe_dir" "$runtime_dir"
printf 'Docker engine validated for Factory e2e scenarios\n'

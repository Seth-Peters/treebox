# Runner mechanics: setup, entry, and agent sessions

Both runners have to prepare a worktree, hand back an entry command that
works when replayed later, and run an agent session that can be cancelled
and whose exit code can be trusted. These decisions record how setup is
planned and cached, how the docker sandbox is wired to the host, and how
entry and launch behave in each runner.

## Decisions

### Every entry makes the sandbox entry-ready first

Every entry calls `prepare_entry` before anything launches or is emitted,
whether the session launches the agent or only prints the command
(`--print` / `--json`). A stopped container is started, and the firewall is
re-applied even on a running container, because a `docker restart` done
outside treebox drops the rules. A failure here is a classified error, and
nothing launches or is emitted.

Where: `cli.py` · `_run_session`; `runners/base.py` · `Runner.prepare_entry`

### Credentials are refreshed on every entry, outside the setup cache

`enter` calls `runner.refresh` on every entry, like the `.env` copy. The
docker runner re-stages its credential copies there, so host logins and
logouts propagate each time. Auth must not ride on the lockfile-hash cache
that decides whether setup runs.

The host runner's `refresh` does nothing: no credentials are staged per
worktree, the agent runs with the live host `~/.claude` / `~/.codex`, and
host logins and logouts apply immediately.

Where: `provision.py` · `enter`; `runners/docker.py` ·
`DockerRunner.refresh`; `runners/host.py` · `HostRunner.refresh`

### Lockfiles both activate an ecosystem and feed the hash

The presence of any of an ecosystem's lockfiles means the ecosystem is
active. The lockfiles plus `extra_manifests` are the files hashed to detect
dependency changes. The setup command is an argv run in the worktree
directory.

Where: `ecosystems.py` · `Ecosystem` (`lockfiles`, `extra_manifests`,
`command`, `manifest_files`)

### The in-container cache variable can differ from the host one

`container_cache_env` names the env var that points in-container tools at
the mounted cache, and falls back to `cache_env_var`. It exists because of
pnpm: on the host pnpm takes its store via `--store-dir`, but it also reads
the npm-style env form, so containers get `npm_config_store_dir`.

Where: `ecosystems.py` · `Ecosystem.container_cache_env`

### Planning a setup step never touches disk

A `SetupStep` carries the cache directory it reads and writes (or none).
Planning only names that directory. A real run creates it through
`ensure_cache_dirs`, so a dry run has no filesystem effect.

Where: `ecosystems.py` · `SetupStep.cache_dir`, `ensure_cache_dirs`

### A host dry run detects ecosystems from the revision it will build

A new worktree may come from a different `--base` / `--checkout` revision
than the one currently checked out in the repo, so the host dry run detects
ecosystems from the planned revision when one is given. A resumed worktree
already has the exact tree real setup will inspect, so there the plan reads
the tree on disk.

Where: `runners/host.py` · `HostRunner.dry_run_setup`

### A docker dry run reports reuse of an existing container

Real setup reuses a container that already exists for the worktree (an
interrupted `create` leaves one behind), warm or cold. The dry-run plan says
so whenever the engine can be asked. The lookup is a read-only query.

Where: `runners/docker.py` · `DockerRunner.dry_run_setup`

### Docker argv builders return the full command

The docker command builders return the complete argv, leading `docker`
included. The same list can then be shown verbatim by `--dry-run` and by
reporter steps, and `_run_engine` executes one unchanged.

Where: `runners/docker.py` · `DockerRunner._build_command`,
`_run_command`, `_exec_command`, `_run_engine`

### The container idles on `sleep infinity` under `--init`

The detached sandbox container is kept alive between execs by
`sleep infinity`. `docker run --init` puts a real PID 1 above it, so
orphaned grandchildren are reaped.

Where: `runners/docker.py` · `_KEEPALIVE`, `DockerRunner._run_command`

### The firewall command is guarded and idempotent

The command that re-establishes egress lockdown is guarded on the
`TREEBOX_FIREWALL` env var baked in at `docker run`. For a container created
without the firewall it is a no-op. For a container created with it, the
command is idempotent, because `init-firewall.sh` skips itself when the
rules are already live. That makes it safe to run on every entry.

Where: `runners/docker.py` · `_FIREWALL_GUARDED`

### Image user and timezone come from the invoking host user

The build args `USER_UID` and `USER_GID` are pinned to the invoking user, so
files in the worktree and other bind mounts are not owned by root. When `TZ`
is set on the host, it overrides the template's default timezone.

Where: `runners/docker.py` · `DockerRunner._inject`

### The worktree and git common dir are mounted at their host paths

The worktree and its git common dir are bind-mounted 1:1 at their host
paths. Every gitdir pointer inside the worktree then resolves identically in
the container, so plain `git status` works with no relative-worktree setup
and no git-version requirement.

Where: `runners/docker.py` · `DockerRunner._inject`

### Cache mounts are driven by the ecosystem registry

Every configured shared host cache is bind-mounted and the in-container
tools are pointed at it by iterating `ECOSYSTEMS`, the single source of
cache wiring. A new ecosystem therefore cannot silently miss container
warmth. Cold mode adds no cache mounts.

Where: `runners/docker.py` · `DockerRunner._inject`; `ecosystems.py` ·
`ECOSYSTEMS`

### The docker entry command always has `-i`, and `-t` only on a terminal

The `docker exec` entry command always passes `-i`, so stdin piped into the
printed command reaches the agent. It passes `-t` only when a terminal is
attached, so the command emitted by `--print` / `--json` also works when a
script replays it without a TTY.

Where: `runners/docker.py` · `DockerRunner.entry_command`

### The host entry command is self-contained

The host entry command carries its own `cd` into the worktree, like the
docker runner's `docker exec -w`. Without it, pasting the command that
`--print` / `--json` emitted would launch a full-autonomy agent in whatever
directory the user happens to be in, typically the main checkout that
treebox exists to keep agents out of. The `TREEBOX_*` variables are exported
in the command as well, so a replayed command gives the agent the same
values as a direct launch.

Where: `runners/host.py` · `HostRunner.entry_command`

### A cancel reaches the agent inside the container

Signalling the local `docker exec` client does not stop the process in the
container. The docker runner therefore launches the agent through a small
shell wrapper that writes its own pid to a per-session pidfile and then
execs the agent in that same process, so the recorded pid is the agent's.
Two companion helpers read the pid back: one signals the agent's process
group, falling back to the agent alone, and one tests whether the agent
still runs.

On cancel, the local docker CLI can exit before the agent in the container
does, since it received the signal too. treebox waits for the agent itself
for the grace time and then kills it.

Where: `runners/docker.py` · `_AGENT_WRAPPER`, `_AGENT_SIGNAL`,
`_AGENT_ALIVE`, `DockerRunner.launch`

### A sandbox that stopped under the agent is a treebox failure

`docker exec` ends when its container stops or restarts (an engine restart,
`docker restart`, a worker shutdown) and returns a status the agent never
produced. Passing that on as the agent's exit code would make a lost sandbox
look like a finished task. When the exit is non-zero and the container's
start time changed during the session, treebox raises a sandbox error
saying the agent's result is unknown.

Where: `runners/docker.py` · `DockerRunner.launch`

### On the host, a cancel signal is passed to the agent child

A cancel signal sent to treebox is passed on to the agent, which is the
child process. If the agent has not exited after the grace time, it is
killed.

Where: `runners/host.py` · `HostRunner.launch`; `system.py` · `run_agent`

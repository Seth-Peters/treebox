# The Runner protocol contract

`Runner` is the one thing that differs between isolation modes. What a
runner may assume (host locality) and must guarantee (the security
invariants) is in `CLAUDE.md` under "The isolation contract" and in
[record 0001](0001-host-owned-state-and-the-sandbox-trust-boundary.md).
This record holds the rules for the individual methods: when the CLI calls
each one, and what it may and may not do.

## Decisions

### `preflight` gates everything that asks the engine

`preflight` verifies the runner's host dependencies before provisioning and
is also the runner row of `doctor`. It raises `PreflightError`, which
carries a stable code for `--json` callers and a remediation hint.
`check_resources` refuses limits the engine would refuse at sandbox creation
(docker: more CPUs than the engine has). It may ask the engine, so the CLI
calls it only after `preflight`, and never on `--dry-run`, which skips both.
Checks on the value alone come first, as usage errors that do not depend on
the host.

Where: `runners/base.py` · `Runner.preflight`, `Runner.check_resources`,
`PreflightError`

### `initialize` writes runner-owned metadata before the first setup

Before a worktree's first setup, the runner persists what it will later
need to clean up. Sandboxed runners keep these ownership records outside
every sandbox-writable mount; the host runner has nothing to write.

Where: `runners/base.py` · `Runner.initialize`

### `refresh` runs on every `enter`; `setup` only when needed

`setup` is gated by the setup model and the lockfile hash. `refresh`
re-stages state that must never go stale between sessions (docker: the
credential copies) and runs on every `enter`, independent of that gate:
authentication is not a cache. `setup_reason` lets a runner force `setup`
although the model would skip it, and is called after `preflight`.

Where: `runners/base.py` · `Runner.setup`, `Runner.refresh`,
`Runner.setup_reason`

### `prepare_entry` runs before every launch and every emitted command

The CLI calls `prepare_entry` on every entry, before `launch` and before
`--print` or `--json` output. Docker starts a stopped container and checks
its firewall even when the container is already running. A failed firewall
initialisation raises `FirewallError`: nothing launches and no entry command
is emitted. It is a no-op on the host.

Where: `runners/base.py` · `Runner.prepare_entry`, `FirewallError`

### `dry_run_setup` plans without touching anything

The worktree may not exist on disk yet, so a runner that needs repository
context reads `wt.repo`. `source_ref` is the revision a new worktree will
use; `None` means a resumed worktree at `wt.path`. `cold` must select the
same cache isolation as `setup` without creating its disposable cache.

Where: `runners/base.py` · `Runner.dry_run_setup`

### `teardown` reports instead of raising, and keeps what a retry needs

A runner's teardown warns and keeps going when a cleanup step fails, then
returns what happened and what remains (`"<resource> (<reason>)"`). While
anything remains, the caller keeps the worktree and its record, and the
runner keeps its own metadata, so the same command can finish the job.
Teardown options (docker's volume removal) arrive at the runner's
constructor, never through the protocol.

Where: `runners/base.py` · `Runner.teardown`, `RunnerTeardownResult`

### Doctor vocabulary stays out of the run methods

`RunnerFacts` holds what `doctor` shows: the detail printed when preflight
passes, and whether the agent runs in a sandbox. `sandboxed` decides what
`doctor` checks for the selected harness: on the host, the binary on `PATH`
and the host login; in a sandbox, the login file the sandbox gets a copy of
(the binary is part of the image and is checked at launch).

Where: `runners/base.py` · `RunnerFacts`, `Runner.facts`

### A sandbox that cannot be created or used is one error

`SandboxError` covers every engine refusal: the container will not run or
start, a path cannot be mounted, the container is gone or was not made by
this runner, or it stopped under the agent. The CLI classifies all of them
as `SANDBOX_FAILED` (exit 1) with a recovery hint, because a coordinator
does the same thing for each.

Where: `runners/base.py` · `SandboxError`

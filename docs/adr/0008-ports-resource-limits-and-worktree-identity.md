# Ports, resource limits, and worktree identity

Parallel worktrees need three things that stay fixed for a worktree's life:
a block of ports, the sandbox's resource limits, and an id repo scripts can
build names from. Each is decided on the host before anything is
provisioned, is held in the host-owned record, and is keyed by the
worktree's resolved path, so a later config change or a path alias never
silently changes what an existing worktree has.

## Decisions

### The default port range is 20000-26999, 10 ports per worktree

The default range lies above the well-known ports (0-1023) and below both
ephemeral ranges (Linux 32768-60999, macOS and IANA 49152-65535), so the
kernel never hands one of these numbers to an outgoing connection. It also
stays clear of common dev-service ports (3000, 5432, 6379, 8080, 9000,
27017). With 10 ports per worktree the range holds 700 blocks.

A configured range is bounded by 1024 and 65535: below 1024, binding needs
privileges on Linux; above 65535, no port exists.

The range and block size come from `[ports]` in user config only.

Where: `config.py` · `DEFAULT_PORT_START`, `DEFAULT_PORT_END`,
`DEFAULT_PORT_BLOCK_SIZE`, `_PORT_MIN`, `_PORT_MAX`, `Config.ports`

### The ledger is written first, the record copy second

On `enter`, the ledger entry is reserved first and the record's copy is
updated after it. The ledger normally already has the recorded block. If the
ledger lost the entry, the reservation takes the recorded block back, or
takes a free one when another worktree holds that block now. The record is
rewritten only when the block changed.

Where: `provision.py` · `enter` (`_reserve_ports`)

### A ledger entry is matched by resolved path

A worktree's own ledger entry is found by comparing resolved paths. A root
spelled through an alias (`/var` against `/private/var`) is still the same
worktree and keeps its block.

Where: `ports.py` · `_own`

### A dry run previews the port block without touching the ledger

`--dry-run` computes the block a real run would reserve now and shows it in
the plan. The call is read-only: the ledger is not written. Exhaustion fails
the dry run exactly as it would fail the real run.

Where: `provision.py` · `dry_run_plan` (`ports.preview`)

### treebox exports variables and starts no service

treebox exports `TREEBOX_NAME`, `TREEBOX_ID`, `TREEBOX_PORT_BASE` and
`TREEBOX_PORT_COUNT` to setup and to the agent entry. Repo-owned scripts
read them to run services in parallel worktrees. treebox starts no service
itself.

Where: `models.py` · `ENV_NAME`, `ENV_ID`, `ENV_PORT_BASE`,
`ENV_PORT_COUNT`, `Worktree.treebox_env`

### The worktree id is exactly 10 lowercase hex characters, forever

The `worktree_id` format is documented contract. Repo scripts put the id in
database names and Compose project names, and the docker runner's sandbox
names embed it, so the length never changes.

Where: `models.py` · `WORKTREE_ID_LENGTH`, `worktree_id`

### `list` shows the id that `enter` would give

git may canonicalize a root alias (for example `/var` to `/private/var`).
`list` therefore computes the id from the path spelled the way `enter`
spells it, through `resolve.Candidate.registered`, so the id it shows is the
one the worktree's setup and agent actually get.

Where: `cli.py` · `_collect_rows`

### Limits are final before provisioning, and the record holds them

`create` resolves the effective limits before anything is provisioned.
Those values are what the host-owned record holds.

In `Config`, `resources` holds the `[resources]` CPU, memory and process
limits; an unset field means treebox applies no cap. For an existing
worktree the CLI replaces this with the limits in its record, which are what
the sandbox actually has.

Where: `cli.py` · `create` (`_creation_limits`); `config.py` ·
`Config.resources`

### Explicit flags are remembered so that only defaults drift

`Config.explicit_resources` names the limits an explicit `create` flag
chose. It is never read from TOML. The record keeps it, so on a later
`enter` only config or template values can be reported as drift; a limit the
operator picked by flag is pinned.

When checking for drift, only a value the config or template actually sets
counts. An unset default asks for nothing, so it never contradicts a limit
chosen at create.

Where: `config.py` · `Config.explicit_resources`; `cli.py` ·
`_check_entry_resources`

### Each runner declares the limits it can apply

`Runner.supported_resources` is the set of generic limits
(`resources.RESOURCE_NAMES`) a runner can apply to its sandbox. The runner
maps each one to its own engine. An explicit flag for a limit outside the
set is refused. A config or template default for one is simply not applied.

Where: `runners/base.py` · `Runner.supported_resources`

### The host runner applies no limits and refuses to pretend

The host runner has no sandbox, so there is no cgroup to cap: the agent runs
as the operator. Its `supported_resources` is empty.

The CLI refuses explicit limits for this runner first. `check_resources`
still raises `ResourceLimitError` for any limit, so a direct caller that
bypasses the CLI cannot silently run uncapped.

Where: `runners/host.py` · `HostRunner.supported_resources`,
`HostRunner.check_resources`

### treebox owns every limit it sets on the container

When the docker runner builds the run command, it drops the raw `runArgs`
spelling of any limit treebox sets. That spelling was already folded into
the record at create, so the recorded value is the one the container gets
and the two can never disagree.

Where: `runners/docker.py` · `DockerRunner._run_command`
(`_split_limit_args`)

### Memory sizes follow Docker's grammar

A memory value is parsed with Docker's own size grammar (go-units
`RAMInBytes`): a number, an optional space, an optional unit, an optional
`i`, an optional `b`. What treebox accepts is what Docker accepts.

Where: `resources.py` · `_MEMORY_RE`

### Resource values in config are strictly typed

A `[resources]` or template value must have the TOML type of its limit, like
every other config value. A quoted `"512"` where a number is expected is a
mistake and raises `ResourceLimitError`; it is not coerced. A boolean is
rejected explicitly, because Python treats `bool` as an `int`.

Where: `resources.py` · `from_mapping`

### No resource limits by default

treebox caps nothing unless asked. Limits are options (`--cpus`, `--memory`,
`--pids-limit`, `[resources]`, the template), and each has verbose,
agent-readable help. Default caps were rejected: a limit nobody chose fails
work in ways that are hard to trace back to treebox.

Decided: 12 November 2026, issue #67.
Where: `resources.py` · `ResourceLimits`; `cli.py` · `_CPUS_HELP`

### One limit vocabulary, a fixed precedence, and a refused mismatch

There is one generic vocabulary (`cpus`, `memory`, `pids`) for every runner;
the per-runner flags proposed in PR #51 (`--sbx-cpus`, `--sbx-memory`) were
rejected because they would give treebox two vocabularies. Per field the
order is flag, then user config, then operator template. A template that
sets one limit twice with different values is `TEMPLATE_INVALID`, so the
result never depends on Docker's last-flag-wins. On the host runner an
explicit flag is an error (`RESOURCE_UNSUPPORTED`), while a config or
template value is skipped with a note, so one `[resources]` table does not
break host worktrees. On `enter`, a limit that differs from the recorded one
and cannot be applied to the existing container is refused
(`RESOURCE_MISMATCH`), never applied silently and never ignored.

Decided: PR #84.
Where: `cli.py` · `_creation_limits`, `_refuse_unsupported_limits`,
`_check_entry_resources`; `runners/docker.py` · `_split_limit_args`

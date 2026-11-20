# Host-owned state and the sandbox trust boundary

A docker sandbox gets the worktree and the repository's git common dir
mounted writable, so everything under them is under the boxed agent's
control. treebox therefore keeps every piece of state that decides policy,
defines the sandbox, or authorizes destructive cleanup on the host, outside
every mount, and treats the worktree, the shared git dir, and the target
repo's contents as untrusted input. The decisions below are the concrete
places where that boundary is drawn.

## Decisions

### The creation policy is read only from the host-owned record

On `enter`, the creation policy (isolation, firewall, template, harness)
comes only from the host-owned record. A missing or invalid record is a
classified error with a recovery path. It never falls back to the config
default, and never to the worktree's sandbox-writable state: either fallback
would let a lost or tampered record silently change the policy a worktree
was created with.

Where: `cli.py` · `enter`

### `list` shows an unknown policy as unknown

The policy columns of `list` also come from the host-owned record. When the
record is missing or invalid, the columns are empty (`""`). An honest
"unknown" is preferred over a guess taken from config defaults.

Where: `cli.py` · `_collect_rows`

### A template name is an identifier, never a path

A sandbox template name is one path segment from a conservative,
filesystem-safe charset (`^[A-Za-z0-9._-]+$`). It can therefore only select
a directory under the operator-owned templates root that
`assets.template_dir` resolves; it can never point somewhere else.

- The config field `template` names an operator-owned container template.
  It is resolved by `assets.template_dir` and never read from the target
  repo.
- Config validation rejects a path or `..` as a template name, because
  either would let the name escape the operator-owned template locations.
- `template init`, `template path`, and template resolution all apply the
  same rule (`models.is_valid_template_name`), so `init` can never write
  outside the templates root.

Where: `models.py` · `TEMPLATE_NAME_RE`, `is_valid_template_name`;
`config.py` · `Config.template`, `validate_config`; `cli.py` ·
`template_init`

### Unknown user-config keys are rejected

`load_config` fails loudly on an unknown top-level key. A typo such as
`isolatoin` or `firewal` would otherwise be security posture silently not
applied, which is the failure the `INVALID_CONFIG` contract exists to
prevent. The accepted vocabulary is `USER_KEYS`, not every `Config` field:
an internal field is not a key a user config can set or be pointed at.

Where: `config.py` · `load_config`, `USER_KEYS`

### Unknown `container.json` keys are rejected

The same rule applies to the sandbox template's `container.json` and to the
firewall overlay, which shares its schema. Any key outside `_KNOWN_KEYS` is
a loud error. This config is security-sensitive, and a typoed key that was
silently dropped would strip behavior the operator relies on. `_KNOWN_KEYS`
is the runtime half of the contract and the `ContainerConfig` type is the
static half; `_require_known_keys` is the one place a freshly parsed dict is
validated and narrowed into that type.

Where: `runners/docker.py` · `_KNOWN_KEYS`, `ContainerConfig`,
`_require_known_keys`

### Host-side git pins the config keys that run code

The docker runner mounts the git common dir writable so in-container git
can commit and fetch. A boxed agent can therefore write the shared
`.git/config`. Some config keys make git run code on the host when treebox
invokes it, so every treebox git call pins them with `-c`:
`core.hooksPath=/dev/null`, `core.fsmonitor=false`, `core.pager=cat`. A
hostile hooks path, fsmonitor command, or pager planted in the shared config
is ignored whenever treebox shells out to git.

The pin is per invocation on purpose. It does not touch the agent's own
in-container git, which resolves its config independently and stays free to
use hooks and fsmonitor normally.

Where: `git.py` · `_SAFE_CONFIG`, `_git`

### Caller-supplied paths and refs are always parsed as operands

`git worktree add` is called with `--` ahead of the path and ref, so a
flag-shaped path or ref is always parsed as an operand. For the same reason
`git rev-parse` gets `--end-of-options` before the ref: the ref comes from
the caller and may be flag-shaped.

Where: `git.py` · `worktree_add`, `_commit_oid`

### The shared `.git/hooks` is mounted read-only

Host git executes the shared `.git/hooks` under its default
`core.hooksPath`: on the operator's next `treebox create` or `enter`, or on
a manual git command in the repo. Because the common dir is writable in the
sandbox, a boxed agent could otherwise drop a hook there that the host later
runs. The runner mounts `hooks/` read-only on top of the writable common dir
to prevent that. Commit, fetch, push, branch, and checkout never write
hooks, so the agent's own git is unaffected.

Config-based redirects (`core.hooksPath` or `core.fsmonitor` set in the
shared config) are a separate path and are neutralized by the pinned config
on treebox's host-side git, described above.

Where: `runners/docker.py` · `DockerRunner._inject`

### The read-only hooks mount is probed before setup runs

A read-only mount is only a guarantee if the engine enforces it, so setup
checks it inside the container: the probe tries to create a file in the
shared hooks dir, which a read-only mount refuses for every user. A write
that goes through exits with a dedicated status (73) so it is told apart
from the probe itself failing, and a hooks dir that is not mounted is a
failed check, not a pass. The probe runs right after the container starts
and before post-create, because post-create is the first workspace-derived
code to run there (firewall init is operator-owned). A writable hooks dir
is `SANDBOX_FAILED`.

Where: `runners/docker.py` · `_HOOKS_PROBE`, `_HOOKS_WRITABLE`,
`DockerRunner._require_readonly_hooks`

### Sandboxes get scoped credential copies, not the live login dir

Each harness lists the only login files that are staged into a sandbox:
what the agent needs to authenticate, plus its user-level settings. One of
them is the login file, a JSON object holding the subscription login itself;
the rest are settings. Everything else in the live login dir (state,
history, host-executed hooks) stays on the host.

The runner mounts these staged copies, never the live host login dirs. They
are mounted in cold mode too: authentication is not a cache, so `--cold`
does not drop it.

Where: `harnesses.py` · `Harness._credential_files`, `Harness._login_file`;
`runners/docker.py` · `DockerRunner._inject`,
`DockerRunner._refresh_credentials`

### Credential staging never writes through a sandbox-controlled file

The staged credential directory is mounted into the sandbox, so its entries
are under the agent's control. Staging prepares each complete file in the
host-owned parent directory and then atomically replaces the destination
entry. It never opens or chmods a sandbox-controlled file for writing. The
mounted directory is pinned by an open descriptor first, so replacing the
directory with something else cannot redirect the writes.

Where: `harnesses.py` · `Harness.stage_credentials`, `_replace_credential`

### Submodule copies treat the worktree as an untrusted destination

`plan_submodules` validates the source side of a submodule copy.
`copy_submodule` owns the destination side, because the worktree tree is
untrusted. It also skips `.git` pointer files: their relative gitdir would
break git in the linked worktree (issue #16).

Where: `provision.py` · `copy_submodules`; `submodule_copy.py` ·
`copy_submodule`

### The sandbox definition lives beside the worktree, outside every mount

Each worktree's operator-owned container config and build context are
rendered into a host-side `.containers` directory. Those files are never
mounted into the container. Only the worktree, its git common dir, and the
scoped credential copies are, so a boxed agent cannot reach the definition
of its own sandbox.

The `credentials` subdirectory of that per-worktree directory holds the
staged credential copies, one subdirectory per harness.

Where: `runners/docker.py` · `_CONFIG_ROOT`, `_CREDS_SUBDIR`

### The sandbox config is regenerated from the template on every run

The operator template is the single source of truth, so the config dir is
wiped and rendered again each run; leftovers from a previous run are never
trusted. Two entries survive the wipe:

- The staged credentials, because an existing container's bind mounts point
  at them and must stay valid.
- The host-owned resource manifest, because config or template drift must
  never rewrite the ownership record that teardown relies on.

The config dir doubles as the docker build context, so a generated
`.dockerignore` excludes the credentials subdirectory. The staged
credential copies are never sent to the docker daemon.

Where: `runners/docker.py` · `DockerRunner._write_config`

### A host-owned manifest is the only authority for destructive cleanup

The image and volumes that belong to one worktree are recorded in
`treebox-resources.json`, beside the generated container config and outside
every sandbox mount. Worktree state under `.git` is deliberately not used
for this: the git common dir is sandbox-writable, so it cannot authorize
destructive cleanup on the host.

Provisioning writes this runner-owned metadata (`runner.initialize`) before
setup runs, so destructive host cleanup never needs to trust
sandbox-writable state.

Where: `runners/docker.py` · `_RESOURCE_MANIFEST`, `DockerRunner.initialize`;
`provision.py` · `_finish_setup`

### Teardown removes only what the manifest names

During docker teardown:

- The container's mounts are read only to print the non-destructive "kept"
  note. Authority to delete comes exclusively from the manifest.
- Volume names in the manifest are authoritative but may already be gone.
  They are intersected with the engine's live volume list so a retried
  teardown stays quiet.
- Only the exact image the manifest records is removed, and it is removed
  with or without a container, so a manual `docker rm` does not leak it. An
  image that a container uses but the manifest does not name is never
  removed.
- The container is removed with `docker rm -f -v`. `-v` also removes the
  container's anonymous volumes (a base image `VOLUME`). They are nameless
  and nothing can use them again once the container is gone, so without `-v`
  every teardown would leak one. `-v` never removes a named volume; those
  stay under the manifest rule. An anonymous volume still used by another
  container (`--volumes-from`) is kept by the engine, and `docker rm` still
  exits 0.
- The host-side config and build-context dir is removed only when nothing
  remains. It holds the resource manifest, and a retry needs it.

Where: `runners/docker.py` · `DockerRunner.teardown`

### Docker isolation refuses to run as root

The preflight checks for uid 0 first, because no docker setup can fix it.
The sandbox user takes the invoking user's uid, so as uid 0 it would be root
inside the box and free to delete its own firewall rules. (The template's
`useradd -u 0` would also fail in the middle of the build.) The run is
refused before any provisioning.

Where: `runners/docker.py` · `DockerRunner.preflight`

### Egress lockdown comes before any workspace-derived code

When the firewall is enabled, it is applied before `post-create.sh`. Setup
runs code derived from the untrusted workspace (for example, `uv sync`
executes the repo's build backend), so the lockdown has to exist first.
`post-create.sh` itself fails closed if this ordering is ever lost.

Where: `runners/docker.py` · `DockerRunner.setup`

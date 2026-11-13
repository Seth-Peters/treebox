# Configuration

Configuration is **user-level only** — treebox never reads config from the
target repo, because a repo-level config could run arbitrary commands on your
host. Everything is optional; flags always win over the file.

## The config file

Lives at `$TREEBOX_CONFIG` if set, else `$TREEBOX_HOME/config.toml` (default
`~/.treebox/config.toml`). That treebox home is where global settings and
templates live — the same `.treebox` name a repo uses for its worktrees, just
at `$HOME` instead of in a repo, mirroring `~/.claude` vs. a project's
`.claude`. All keys optional:

Treebox no longer consults `$XDG_CONFIG_HOME/treebox` for config or templates.
Move old files to the treebox home (`$TREEBOX_HOME` when set, otherwise
`~/.treebox`), or set `$TREEBOX_CONFIG` / `$TREEBOX_TEMPLATE_DIR` explicitly.

```toml
isolation = "host"     # host | docker
harness  = "claude"    # claude | codex
base     = "main"      # default base branch
root     = ".treebox/worktrees"
env_file = ".env"      # canonical secrets path, copied into each worktree
firewall = false       # container firewall (docker isolation)
template = "default"   # sandbox template name

# Replace auto-detected dependency setup with your own shell commands,
# each run in the worktree dir. A single string works too.
setup_hook = ["uv sync --frozen", "uv run python -m scripts.seed"]

# Override where shared package caches live, per ecosystem
# (uv, npm, pnpm, go, cargo). Defaults honor the standard env vars
# (UV_CACHE_DIR, GOMODCACHE, …); docker isolation bind-mounts
# these into the container. Entries you omit keep their defaults.
[caches]
uv  = "/mnt/fast/cache/uv"
npm = "/mnt/fast/cache/npm"

# Default CPU, memory, and process limits for every new sandbox
# (docker isolation). Omit a key for no treebox cap. See "Resource limits".
[resources]
cpus   = 2        # CPUs (a decimal is fine: 1.5)
memory = "4g"     # Docker's memory syntax
pids   = 512      # most processes + threads at once
```

Every key is optional, but the vocabulary is closed: an unknown top-level key
(a typo like `isolatoin`) is a loud error (exit `2`, with a did-you-mean
suggestion), never silently ignored - a typo'd `isolation` or `firewall` would
otherwise drop your security posture without warning. Only the subkeys inside
`[caches]` are open-ended (ecosystem cache names); `[resources]` accepts only
`cpus`, `memory` and `pids`.

Path-valued keys understand a leading `~`: `root`, `env_file`, and every
`caches` entry expand `~/…` to your home directory before use, as do quoted
`--root '~/trees'` and `--repo '~/proj'` on the command line. Ordinary
relative paths keep their meaning: `root` and `env_file` stay relative to the
repo.

For a new `create`, precedence is what you'd expect:

```text
command-line flag  >  config.toml  >  built-in default
```

For an existing worktree, `enter` and `teardown` also read the worktree's
[host-owned record](how-it-works.md#the-host-owned-record) of its creation
policy. Recorded isolation wins over the config default, and a conflicting
explicit `enter --isolation` exits `5`; `enter` always reuses the recorded
firewall, while recorded harness and template beat config defaults unless
`--harness` or `--template` is passed. `enter` refuses a worktree without a
usable record (tear it down and create it again), never falling back to the
config default. For
Docker teardown ownership and missing-manifest behavior, see the
[runner-owned resource manifest](how-it-works.md#the-host-owned-record).

| Key        | Default              | What it controls                                          |
| ---------- | -------------------- | --------------------------------------------------------- |
| `isolation`| `host`               | Where agents run: the worktree shell, or a docker sandbox. |
| `harness`  | `claude`             | Which agent `create` launches by default; `enter` reuses the harness the worktree was created with unless `--harness` overrides it. |
| `base`     | `main`               | Base branch for new branches (resolved as `origin/<base>`). |
| `root`     | `.treebox/worktrees` | Where worktree directories are created: repo-relative, absolute, or `~`-prefixed. |
| `env_file` | `.env`               | The secrets file copied into every new worktree; repo-relative unless absolute or `~`-prefixed. |
| `firewall` | `false`              | Restrict container egress (docker isolation).              |
| `template` | `default`            | Which operator template defines the sandbox.              |
| `setup_hook` | *(auto-detect)*    | Your own setup commands instead of the detected package manager's. |
| `caches`   | *(standard env vars)* | Per-ecosystem shared cache locations treebox installs from and mounts. |
| `resources` | *(no limits)*       | Default `cpus` / `memory` / `pids` limits for a new sandbox; see [resource limits](#resource-limits). |

## Resource limits

By default a docker sandbox has **no** CPU, memory, or process limit. Set them
per worktree so one sandboxed build or test run cannot starve the other tasks
on the same machine:

```bash
treebox create fix-auth --isolation docker --cpus 2 --memory 4g --pids-limit 512
```

| Flag | `[resources]` key | Template `resources` key | Value | Docker maps it to |
| ---- | ----------------- | ------------------------ | ----- | ----------------- |
| `--cpus N` | `cpus` | `cpus` | CPUs, a decimal (`2`, `1.5`); at least `0.01`, at most the engine's CPU count | `docker run --cpus` |
| `--memory SIZE` | `memory` | `memory` | Docker's memory syntax: a number and a unit `b`, `k`, `m`, `g`, `t` or `p` (binary multiples, an optional `b` / `ib`, any case; a bare number is bytes); at least `6m` | `docker run --memory` |
| `--pids-limit N` | `pids` | `pids` | A positive whole number of processes and threads | `docker run --pids-limit` |

**Precedence**, field by field: explicit flag, then `[resources]` in the user
config, then the operator template's `resources` object (in its
`container.json`). An omitted value means no treebox cap. No resource policy
is ever read from the target repo.

**Validation happens before provisioning.** A bad value (`--memory lots`,
`--cpus 0`, `--pids-limit -5`, `--memory 1m`, or `--cpus 99` on an engine with
fewer CPUs) exits `2` with `INVALID_RESOURCE` and leaves no worktree, branch,
image or container behind. A bad `[resources]` table is `INVALID_CONFIG`
(exit `2`); a bad template `resources` object is `TEMPLATE_INVALID` (exit `1`).

**The limits are fixed when the sandbox is created.** The effective limits are
written to the worktree's [host-owned record](how-it-works.md#the-host-owned-record),
and from then on they are what the sandbox has: on `enter`, after a
`docker stop` / `docker restart`, and when `enter` has to re-create a removed
container. `list --json` and `create --dry-run` show them (`resources`). A
container's limits cannot change in place, so:

- `enter --memory 8g` on a sandbox recorded with another value is a conflict
  (exit `5`, `RESOURCE_MISMATCH`) whose hint gives the exact re-create commands
  (`treebox teardown <name> && treebox create <name> --checkout <branch> ...`).
  Restating the recorded value is fine.
- The record also keeps which limits a `create` flag chose. A limit that came
  from `[resources]` or the template, and that they now give differently, is
  drift: a warning on every `enter`, with the same re-create commands. The
  sandbox keeps its recorded limits. `enter --json` prints no warning; its
  `resources_drift` object (`cpus`, `memory`, `pids`; `null` = no drift) holds
  the new values instead.
- A limit a flag chose never drifts: `create --memory 4g` under
  `memory = "2g"` is a choice, so `enter` is silent, also after the config
  default changes. A limit that `enter` restates by flag does not drift either.
- The re-create commands restate the limits a flag chose. The other limits
  come from `[resources]` and the template again, as on any `create`.

**Raw `runArgs` limits.** A template may already set `--cpus`, `--memory` /
`-m` or `--pids-limit` in `runArgs`. That keeps working: treebox reads it as the
template's own value (the lowest tier, like the template `resources` object),
records it, and passes it to `docker run` once. When a flag or the config sets
the same limit, treebox drops the raw `runArgs` flag and passes only its own
value, so the result never depends on Docker's last-flag-wins. A template that
sets one limit twice with different values (for example `resources.memory` and
`runArgs` `--memory`) is `TEMPLATE_INVALID`.
For a limit the record leaves unset (no treebox cap), a raw `runArgs` flag
that the template has when the container is re-created passes through unchanged.

**Host isolation** has no sandbox to cap. An explicit `--cpus` / `--memory` /
`--pids-limit` with `--isolation host` is refused (exit `2`,
`RESOURCE_UNSUPPORTED`), never ignored. A `[resources]` table in config is a
default for sandboxes, so it does not make host worktrees fail: `create` notes
that it is not applied and records no limits.

## Customizing the sandbox

In docker isolation, the container is built from a **template** — a directory
of files that *you* own and edit. treebox ships one (pinned to CPython 3.14.6);
you copy it out, tweak it, and point runs at it.

Two properties are deliberate: the template is never read from the target repo
(a repo you don't trust must not define the box it runs in), and it's rendered
*beside* the worktree, never inside the mount — so a sandboxed agent can't edit
its own cage.

### The files you own

A template is a directory. Copy it out (below) and these become yours:

| File                                | What it's for                                                                     |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| `Dockerfile`                        | The image: base, system packages, global CLIs.                                    |
| `container.json`                    | How the container runs — most edits touch only `postCreate` here. Schema below.   |
| `post-create.sh`                    | The default setup script `postCreate` runs. Edit inline or point elsewhere.       |
| `init-firewall.sh` · `allowed-domains.sh` | Egress rules, applied only when the firewall is on.                         |
| `firewall.json`                     | Overrides merged onto `container.json` when the firewall is on.                    |

### 1. Scaffold a template

You edit a *copy*, never the shipped one. `treebox template init` copies the
default into `~/.treebox/templates/<name>` for you — a `node` box and a `python`
box can coexist, one per stack:

```bash
treebox template init node          # copies from the built-in default
treebox template init node --from python   # or fork one of your own
```

It always writes the full required file set, so a later `create` never fails
with "*file* not found in template dir". Inspect what you have any time:

```bash
treebox template list               # names, source, firewall, status, default + what the default bundles (`ls` works too)
treebox template path node          # where it lives: cd "$(treebox template path node)"
```

### 2. Edit two things

The shipped image already bundles Node 22, uv, `gh`, ripgrep, and the agent
CLIs, so most projects change just two files.

**Global tooling → the `Dockerfile`:**

```dockerfile
USER root
RUN npm install -g pnpm@9 typescript tsx
USER ${USERNAME}
```

**Your install command → `postCreate` in `container.json`:**

```json
"postCreate": "if [ -f pnpm-lock.yaml ]; then pnpm install --frozen-lockfile; elif [ -f package-lock.json ]; then npm ci; else npm install; fi"
```

In docker isolation, dependency setup runs `postCreate` — *not* the host-mode
ecosystem auto-detect — so a non-Python project must wire its own install here.
The package cache is mounted, so it stays warm across worktrees.

### 3. Point treebox at it

Select it per run, or make it your default:

Templates in `~/.treebox/templates/<name>` are picked by the `template` config
key or per-invocation with `--template <name>`. Unknown template names are
errors rather than silent fallbacks to `default`. A template name is an
identifier (letters, digits, `.`, `_`, `-`), never a path: `--template /abs/dir`
or `--template ../x` exits `2` (`INVALID_CONFIG`). To use a directory anywhere
else, point `$TREEBOX_TEMPLATE_DIR` at it.

```bash
treebox create my-feature --isolation docker --template node   # this run only
```

```toml
template = "node"   # the default, in ~/.treebox/config.toml
```

First run builds your image (cached after that), provisions the worktree, runs
`postCreate` to install deps, and launches the agent inside.

### The container.json schema

`container.json` is treebox's own small schema (not Docker's `devcontainer`):

- `build.dockerfile` / `build.args` → feed `docker build`
- `user`, `env`, `mounts` (docker `--mount` syntax, with `${workspaceName}`
  substituted per worktree), `runArgs` → feed `docker run`
- `resources` → default `cpus` / `memory` / `pids` limits for new sandboxes
  (the lowest [precedence](#resource-limits) tier)
- `postCreate` → the command exec'd in the workspace after the container starts

When the firewall is enabled (`firewall = true` in config, or `--firewall` per
run; `--no-firewall` opts one run out), `firewall.json` deep-merges on top. Any
container config in the target repo itself is deliberately ignored — see
[how it works](how-it-works.md#the-sandbox-config-lives-outside-the-box).

## Environment variables

| Variable          | Effect                                                        |
| ----------------- | ------------------------------------------------------------- |
| `TREEBOX_HOME`    | Base dir for `config.toml` and `templates/` (default `~/.treebox`). |
| `TREEBOX_CONFIG`  | Explicit path to the config file (overrides `TREEBOX_HOME`). Setting it asserts the file exists — a missing file here is a loud error (exit `2`), not a silent fall-back to defaults. |
| `TREEBOX_TEMPLATE_DIR` | Explicit template dir; wins for any `--template` name. |
| `XDG_CACHE_HOME`  | Standard XDG base for the shared package caches treebox mounts. |

All four expand a leading `~` to your home directory, like the path-valued
config keys above.

Secrets stay in files: treebox copies your repo's `.env` (or the configured
`env_file`) into each worktree and mounts it into containers. Host isolation
uses your live subscription login; docker isolation mounts scoped copies of the
login files each harness declares and refreshes them on every entry.

The `.env` copy replaces a file or symlink at `<worktree>/.env` (a symlink is
removed, never written through). If the worktree has a **directory** there -
the branch commits `.env/`, or an agent created one - treebox leaves it
untouched and skips the copy with a warning (`.env not copied: ... is a
directory in this worktree`); `create` and `enter` still succeed, and
`treebox list --json` reports `"env": "absent"` for that worktree. Rename or
remove the directory on the branch to get the copy back. `.gitmodules` follows
the same rule.

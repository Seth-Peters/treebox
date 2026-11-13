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
```

Every key is optional, but the vocabulary is closed: an unknown top-level key
(a typo like `isolatoin`) is a loud error (exit `2`, with a did-you-mean
suggestion), never silently ignored - a typo'd `isolation` or `firewall` would
otherwise drop your security posture without warning. Only the subkeys inside
`[caches]` are open-ended (ecosystem cache names).

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
| `setup_hook` | *(auto-detect)*    | Your own setup commands instead of the detected package manager's. They run in order; one that exits non-zero fails `create` / `enter` (`SETUP_FAILED`). |
| `caches`   | *(standard env vars)* | Per-ecosystem shared cache locations treebox installs from and mounts. |

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
| `treebox-fetch` · `treebox-online`  | Build helpers the default `Dockerfile` copies in. Optional: a template needs them only if its `Dockerfile` copies them. |
| `firewall.json`                     | Overrides merged onto `container.json` when the firewall is on.                    |

### 1. Scaffold a template

You edit a *copy*, never the shipped one. `treebox template init` copies the
default into `~/.treebox/templates/<name>` for you — a `node` box and a `python`
box can coexist, one per stack:

```bash
treebox template init node          # copies from the built-in default
treebox template init node --from python   # or fork one of your own
```

For copy and recovery behavior, see the
[`template` command reference](usage.md#template-scaffold-and-inspect-sandbox-templates).
Inspect what you have any time:

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
- `postCreate` → the command exec'd in the workspace after the container starts

When the firewall is enabled (`firewall = true` in config, or `--firewall` per
run; `--no-firewall` opts one run out), `firewall.json` deep-merges on top.
The bundled `init-firewall.sh` permits DNS, GitHub IP ranges, and addresses
resolved from `ALLOWED_DOMAINS` in `allowed-domains.sh`.

The default gateway has no blanket exception. To permit specific TCP ports
at that address, set `ALLOWED_HOST_PORTS` in your template's
`allowed-domains.sh`, for example `ALLOWED_HOST_PORTS=(5432 8080)`.
Use decimal port numbers from `1` to `65535`, without leading zeros.
An empty or absent array opens no additional ports. On Docker Desktop, the
gateway is the engine VM, not the macOS host.

The allowlist is baked into the image from the operator template. After an
edit, [re-create the sandbox](usage.md#docker-entry-and-firewall-recovery)
to apply the change. Existing operator templates keep their own scripts;
treebox does not replace them with updated bundled scripts. Any container
config in the target repo itself is deliberately ignored. See
[how it works](how-it-works.md#the-sandbox-config-lives-outside-the-box).

### Build inputs

The image is not published. Each machine builds it from the template. The
bundled `Dockerfile` pins the base image and direct tool versions. These pins
do not guarantee reproducible images because Debian package versions and npm
dependency ranges can resolve differently between builds.

- The base image uses a multi-architecture index digest. The index selects
  the platform-specific image for amd64 or arm64.
- Direct tool downloads have an exact version and a SHA-256 for each architecture
  (`*_SHA256_AMD64`, `*_SHA256_ARM64`). The helper `treebox-fetch` verifies
  each downloaded file. A wrong checksum stops the build. No installer script
  is piped into a shell.
- Direct npm packages have exact versions. Their transitive dependencies
  are not locked by the template.
- Debian packages come from the signed Debian archive of the base image's
  release (trixie). They are not pinned to a version: they take Debian's
  security updates, so these package versions can change between builds.

| Tool | Pin (`ARG`) | Pin or checksum source |
| ---- | ----------- | ---------------------- |
| Base image | `FROM …@sha256:` | Docker Hub index digest (`docker buildx imagetools inspect`) |
| Node.js | `NODE_VERSION` | `SHASUMS256.txt` of the release on nodejs.org |
| GitHub CLI | `GH_VERSION` | `gh_<version>_checksums.txt` of the GitHub release |
| uv | `UV_VERSION` | `<asset>.sha256` of the GitHub release |
| Claude Code | `CLAUDE_CODE_VERSION` | `manifest.json` of the release on downloads.claude.ai |
| Codex CLI | `CODEX_CLI_VERSION` | exact npm version |
| Playwright | `PLAYWRIGHT_VERSION`, `PLAYWRIGHT_CLI_VERSION` | exact npm versions (each pins its browser build) |

**Update policy for the agent CLIs.** `CLAUDE_CODE_VERSION` is the release
that the Claude Code `stable` channel
(`https://downloads.claude.ai/claude-code-releases/stable`) gives when it is
bumped. `CODEX_CLI_VERSION` is the npm `latest` dist-tag of `@openai/codex`
when it is bumped. treebox bumps both on purpose, in a dedicated change, at
least once a month and soon after an upstream security fix. A Claude Code
update changes its version and both architecture checksums together, from the
upstream source in the table. A Codex update changes its npm version.

To run a different tool version in your own template, edit its `Dockerfile`.
For a checksummed download, change the version and both architecture checksums
together. A version-only `build.args` override in `container.json` fails
checksum verification.

**What the image leaves out.** To keep the image small, it installs only
what a supported setup path or a bundled agent uses:

- Playwright installs Chromium's headless shell and FFmpeg. Firefox, WebKit,
  and full Chromium are not installed. Browser workflows must run headless.
  `playwright-cli` opens it through `/opt/google/chrome/chrome`.
- AWS CLI, vim, fzf, delta, cmake, man-db, and GitHub CLI man pages are omitted.
  Other Debian packages can still include man pages.
- Debian's `python3-dev` and `python3-venv` are omitted. The image's CPython
  includes its own headers, and `uv` creates virtual environments. If a build
  backend needs CMake, declare `cmake` as a build dependency or add it to your
  template.
- gcc, g++, make, and pkg-config stay, so `uv sync` and `npm install` can
  build native dependencies from source.

To add a tool, put it in your own template's `Dockerfile` (see
[Scaffold a template](#1-scaffold-a-template)), for example:

```dockerfile
USER root
RUN apt-get update && apt-get install -y --no-install-recommends vim cmake \
  && rm -rf /var/lib/apt/lists/*
USER ${USERNAME}
```

**Chromium runs without its own sandbox.** In the container, Playwright
starts Chromium with `--no-sandbox`. Chromium's sandbox needs new user
namespaces, and Docker's default seccomp profile blocks them on a Linux Docker
host (for example a stock Ubuntu 24.04 host or a GitHub Actions runner), so
there Chromium stops with `No usable sandbox!`. This is acceptable because the
container is the isolation boundary: the agent in it already runs any code
it likes, so Chromium's sandbox adds no protection against the agent. The
playwright library and `playwright screenshot` turn the sandbox off by
default. `playwright-cli` turns it on, so the image sets
`ENV PLAYWRIGHT_MCP_SANDBOX=false` (see the `PLAYWRIGHT_MCP_SANDBOX` variable
in the [playwright-cli README](https://github.com/microsoft/playwright-cli#configuration-file), under "Configuration via env").
To turn the sandbox back on, remove that line from your own template's
`Dockerfile`, and give the container a seccomp profile that allows user
namespaces (`runArgs` in `container.json`); a script also needs
`chromiumSandbox: true` in its launch options.

**Hosts the build needs.** A build needs outbound HTTPS to these hosts (Debian
uses HTTP):

| Host | For |
| ---- | --- |
| `registry-1.docker.io`, `auth.docker.io`, `production.cloudflare.docker.com` | the base image (only if it is not already pulled) |
| `deb.debian.org` | Debian packages, and the Chromium libraries Playwright installs |
| `nodejs.org` | Node.js |
| `github.com`, `release-assets.githubusercontent.com` | GitHub CLI, uv (GitHub redirects release downloads) |
| `downloads.claude.ai` | Claude Code |
| `registry.npmjs.org` | Codex CLI, Playwright |
| `cdn.playwright.dev` | the Chromium headless shell (and FFmpeg) for Playwright |

If the build cannot reach one of them, the step fails with a message that
names the host, for example:

```text
treebox: cannot download https://nodejs.org/dist/v22.23.5/node-v22.23.5-linux-arm64.tar.xz (curl exit 7).
treebox: this build needs network access to nodejs.org.
```

The egress firewall (`--firewall`) does not apply to the build: it only
limits the running container.

**Builders.** The bundled `Dockerfile` supports BuildKit and Docker's legacy
builder. On a Docker installation that provides the legacy builder, set
`DOCKER_BUILDKIT=0` to select it. The buildx plugin is optional when the legacy
builder is available.

## Environment variables

| Variable          | Effect                                                        |
| ----------------- | ------------------------------------------------------------- |
| `TREEBOX_HOME`    | Base dir for `config.toml` and `templates/` (default `~/.treebox`). |
| `TREEBOX_CONFIG`  | Explicit path to the config file (overrides `TREEBOX_HOME`). Setting it asserts the file exists — a missing file here is a loud error (exit `2`), not a silent fall-back to defaults. |
| `TREEBOX_TEMPLATE_DIR` | Explicit template dir; wins for any `--template` name. |
| `XDG_CACHE_HOME`  | Standard XDG base for the shared package caches treebox mounts. |
| `TREEBOX_NON_INTERACTIVE` | Set (`1`, `true`) to run every command in [unattended mode](usage.md#unattended-use), like the global `--non-interactive`. |

The four path variables expand a leading `~` to your home directory, like the
path-valued config keys above.

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
remove the directory on the branch to get the copy back. Treebox never copies
`.gitmodules`: the worktree keeps its branch's own file (see
[submodule copies](usage.md#submodule-copies-and-recovery)).

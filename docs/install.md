# Install

treebox runs on **Python 3.11+** and installs like any other PyPI CLI. The
recommended path is [uv](https://docs.astral.sh/uv/), which also fetches a
managed interpreter — so the host Python doesn't matter.

## One line

```bash
curl -fsSL https://raw.githubusercontent.com/Seth-Peters/treebox/main/install.sh | sh
```

!!! note "No uv?"

    The script stops and tells you how to get it — it won't install a package
    manager behind your back.
    [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
    first, or re-run with `TREEBOX_AUTO_UV=1` to let the script do it.

## With a package manager

=== "uv"

    ```bash
    uv tool install treebox
    ```

=== "pipx"

    ```bash
    pipx install treebox          # needs Python >=3.11 on the host
    ```

=== "from the repo"

    ```bash
    uv tool install git+https://github.com/Seth-Peters/treebox
    ```

## What each isolation mode needs

| Requirement                                      | `host`        | `docker`        |
| ------------------------------------------------ | :-----------: | :-------------: |
| `git`                                            | ✓             | ✓               |
| Subscription login (`~/.claude` / `~/.codex`)    | ✓             | ✓               |
| Host agent CLI on PATH (`claude` / `codex`)      | ✓             | — (template-provided) |
| `docker`                                         | —             | ✓               |

That's the whole table: docker isolation speaks plain `docker build` /
`docker run` / `docker exec`, so there is no Node.js and no dev-container CLI
to install. The default template installs the agent CLIs in the image; the host
supplies scoped subscription-login copies. The worktree and its git dir are
mounted at their host paths, so in-container `git` just works — with no special
git version requirement.

Host isolation launches against your live `~/.claude` / `~/.codex`
**subscription login**. Docker isolation stages scoped copies of those login
files and never mounts the live dirs. The sandbox template docker isolation
provisions is pinned to CPython 3.14.6, independent of whatever runs treebox
itself.

## Verify

```console
$ treebox doctor

  ● doctor   isolation: host

    ✓ git             2.47.3
    ✓ repo            ~/code/myapp
    ✓ uid/gid         1000:1000
    ✓ login: claude   ~/.claude
    ✓ login: codex    ~/.codex
    ✓ .env            ~/code/myapp/.env
    ✓ git auth        authenticated · fresh fetch will succeed
    ✓ isolation: host    no container dependencies

  ✓ ready
```

`doctor` checks the exact things `create` will need — git, agent logins,
credentials for the required `origin` fetch — and prints an advisory with the
fix when something is off. Pass `--isolation docker` to check the Docker daemon
too.

## Updating

When a new version is released to PyPI, upgrade with the same tool you used to install:

=== "uv"

    ```bash
    uv tool upgrade treebox
    ```

=== "pipx"

    ```bash
    pipx upgrade treebox
    ```

=== "installer"

    ```bash
    curl -fsSL https://raw.githubusercontent.com/Seth-Peters/treebox/main/install.sh | sh
    ```

The project uses normal PyPI versions derived from release tags, so users receive updates when you publish a newer version and they run their package manager's upgrade command.

## Installer overrides

The `install.sh` script reads a few environment variables:

| Variable               | Effect                                                                     |
| ---------------------- | -------------------------------------------------------------------------- |
| `TREEBOX_INSTALL_SPEC` | What to install — a PyPI spec (`treebox==0.4.0`) or a `git+https://…` URL. |
| `TREEBOX_TESTPYPI`     | Set to `1` to install from TestPyPI (dependencies still come from PyPI).   |
| `TREEBOX_AUTO_UV`      | Set to `1` to auto-install uv if it's missing (default: fail loudly).      |

## Developing treebox itself

```bash
git clone https://github.com/Seth-Peters/treebox && cd treebox
uv run treebox ...                    # run the CLI from the working tree
uv run --extra dev pre-commit install # lint/format/strict type-check hooks (see CONTRIBUTING.md)
uv run --extra dev python -m pytest   # unit + integration suite
uv run --extra dev mypy               # strict type check
./scripts/validate.sh                 # full gate: lint + typecheck + shell assets + tests + snapshots + smoke
```

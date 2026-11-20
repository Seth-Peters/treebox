# CLI contract: exit codes, JSON, and unattended use

Agents and scripts drive treebox and branch on what it returns, so the exit
codes, the `--json` payload shapes, the error codes, and the split between
stdout and stderr are a contract and not presentation. The decisions below
fix that contract and the rules that keep unattended runs from ever blocking
on a prompt.

## Decisions

### Exit codes are stable and documented

The exit codes are documented in the `--help` epilog and agents branch on
them:

- `0`: ok.
- `1`: generic or unexpected failure.
- `2`: bad invocation (invalid name or branch, ambiguous ref, bad option).
- `3`: the worktree or branch the command needs does not exist.
- `4`: auth or permission.
- `5`: conflict (already exists, uncommitted changes, lock held).

Where: `cli.py` · `EXIT_OK`, `EXIT_ERROR`, `EXIT_USAGE`, `EXIT_NOTFOUND`,
`EXIT_PERMISSION`, `EXIT_CONFLICT`

### JSON payloads only gain fields within a schema version

Every `--json` payload carries `schemaVersion`. Within one version a payload
only ever gains fields (git-porcelain discipline); a breaking reshape or a
rename bumps the version. Agents branch on these payloads, so the shape is
treated as a contract.

One consequence is visible in `list --json`: each row keeps its `missing`
field for older consumers although `health` now carries the full
classification. `missing` is true when `health` is `directory_missing` or
`invalid_linkage`, the two cases where only the git registration is
reliable.

Where: `cli.py` · `SCHEMA_VERSION`; `models.py` · `WorktreeRow.missing`

### A bad flag value is a usage error, not a traceback

`load_config` only vets the config file. The merged config is validated
again after the command-line overrides are applied, so a bogus
`--isolation` or `--harness` is reported as a usage error.

Where: `cli.py` · `_resolve_config`

### `--firewall` is tri-state

An explicit `--firewall` or `--no-firewall` overrides the config in either
direction. Leaving the flag unset (`None`) falls through to the config
default, like every other config-backed option.

Where: `cli.py` · `create`

### A missing git binary has its own error code

When git itself cannot run, that is not a repository problem. It gets the
distinct stable code `GIT_MISSING` (exit 1), so an agent does not chase the
misleading "run inside a git repo" hint. In error classification this case
is checked before the fetch-error and git-error branches, because with the
binary absent their auth and repo hints would mislead.

Where: `cli.py` · `_repo_root`, `_classify`

### Template errors are classified the same way everywhere

- A missing template is not-found (`TEMPLATE_NOT_FOUND`, exit 3) in
  provisioning, the same classification the `template` sub-app gives it. It
  never falls into the generic catch-all.
- A template that exists but whose contents cannot serve the run is a
  runtime error (`TEMPLATE_INVALID`, exit 1) with a stable code, like a
  preflight failure. Agents branch on `error.code`.

Where: `cli.py` · `_classify`

### There is exactly one catch-all error

`ERROR` (exit 1, no hint) is the single catch-all. It covers a failure that
no classified cause covers, for example a git command that failed in a way
treebox does not anticipate. The message is the cause, in git's own words.

Where: `cli.py` · `_classify`

### treebox prompts for git credentials only when it may ask

The fetch during `create` lets git and ssh prompt for credentials only when
treebox is interactive (a terminal on stdin and no `--non-interactive`),
the same way `git pull` behaves. git and ssh ask on the terminal itself, so
a prompt stays out of the way of `--json`'s stdout.

Where: `cli.py` · `create`, `_interactive`

### Teardown never blocks on a confirmation prompt

`--json` and unattended mode are a scripting contract: teardown must never
block on a prompt, and a prompt must never leak into stdout. Without a
terminal, or under `--json`, an unconfirmed teardown fails with
`NEEDS_CONFIRMATION` (exit 5) and requires `--yes`.

Where: `cli.py` · `teardown`

### The agent's exit status passes through, named in one stderr line

When the launched agent exits non-zero, treebox exits with the same status.
A negative code from the launcher means the agent was killed by that signal;
it is mapped to `128 + signal`, following the shell convention.

The last line treebox prints, on stderr, has a fixed prefix:
`treebox: agent exited with status <N>`. This is a documented contract
(`docs/usage.md`): a caller uses it to tell an agent exit from a treebox
failure. The line is never printed for a treebox failure.

Where: `cli.py` · `_run_session`, `AGENT_EXIT_PREFIX`

### `doctor --json` keeps the exit-code contract of the human path

When a hard check fails (git, the repo, the selected agent and its login,
the runner), `doctor --json` exits 1 just as the human path does. A CI line
such as `doctor --json && create` can then branch on `$?` and does not need
to parse `ok`.

Where: `cli.py` · `doctor`

### `doctor` checks what `create` will actually use

- The `.env` row is resolved by the same resolver `copy_env` uses
  (`provision.resolve_env_file`), so doctor reports the exact secrets source
  that provisioning will copy.
- `create` requires a fresh fetch, so doctor validates up front that git
  can authenticate to origin. It exercises the same silent paths `create`
  uses: the ssh-agent first, then the HTTPS fallback through a host CLI,
  credential helper, or token.

Where: `cli.py` · `doctor` (and its nested `_check_git_auth`)

### Under `--json` the reporter is silent and the CLI writes the result

In `--json` mode the reporter emits nothing at all; the CLI writes the
structured result to stdout and the structured error to stderr itself.
`silent` implies `quiet`, and it also suppresses the errors and warnings
that `quiet` alone keeps.

Warnings are still collected in silent mode. Success payloads carry them as
`warnings`, so a caller can branch on what was skipped.

Where: `output.py` · `Reporter.__init__`, `Reporter.warnings`

### Verbose step output goes to stderr

In verbose mode a step's raw output is streamed straight through to stderr.
It is diagnostics, and stdout stays data only (the `--json` payload).

Where: `output.py` · `Reporter.step`

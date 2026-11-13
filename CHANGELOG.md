# Changelog

All notable changes to treebox will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are written at release time, from the squash-commit titles of the pull
requests merged since the previous tag, so this file has no `[Unreleased]`
section. To see what has merged since the latest release, run
`git log --oneline <latest-tag>..origin/main`.

## [1.0.2] - 2026-07-27

### Fixed

- Submodule copies no longer break git in the new worktree: a submodule's
  `.git` pointer file (or an old-style embedded `.git` directory, nested
  submodules included) is no longer copied, so `git status`, `git diff` and
  `git add` work instead of exiting 128 with "not a git repository" (#18).

- `--print` and `--json` sessions on a docker worktree whose container has
  stopped now restart the container, and re-apply the `--firewall` egress
  lockdown, before they emit `entry_command`, so the printed command works on
  replay (#19).

- A docker `--template` that does not exist is now `TEMPLATE_NOT_FOUND`
  (exit 3) on `create`, `enter` and `--dry-run`, not the exit-1 `ERROR`
  catch-all. `create` checks the template before it makes any git state, so a
  missing template no longer leaves a worktree and branch behind (#22).

- `teardown --remove-volumes` now removes a docker worktree's volumes even
  when its container and its recorded template are gone: the volume names are
  recorded in the worktree state when it is provisioned. A worktree whose
  directory was deleted by hand still gets its container and volumes cleaned
  up (#23).

- `enter` refuses a corrupt worktree whose `.git` pointer file is missing with
  `SLUG_CONFLICT` (exit 5) and the `treebox teardown <name>` recovery hint,
  instead of re-running setup against the main checkout and writing state into
  its `.git/` (#27).

- An unknown top-level key in `config.toml` is now an `INVALID_CONFIG` error
  (exit 2) with a did-you-mean suggestion, instead of being silently ignored.
  A `caches` value that is not a table gets the same clean error (#28).

- A template with a malformed or unreadable `container.json`, or a
  `--firewall` run with a template that has no `firewall.json`, now fails with
  `TEMPLATE_INVALID` (exit 1) and a `treebox template path <name>` hint instead
  of a traceback. `create` checks this before it makes any git state.
  `template list` shows which templates can serve `--firewall` (a new FIREWALL
  column, and new `invalid` and `firewall` fields in `--json`) (#29).

- `doctor --json` now reports `ok: false` whenever `doctor` exits 1 (for
  example when the Docker daemon is unreachable), and off-TTY `doctor` output
  no longer hard-wraps long paths onto a new line (#30).

- The human `list` table shows each worktree's recorded isolation (`host` or
  `docker`, `unknown` only when its state cannot be read), names the worktree
  root in a footer, and keeps the name, branch and isolation columns readable
  at narrow widths. The `--json` payload is unchanged (#31).

- A missing git binary is now a clean `GIT_MISSING` error (exit 1, with an
  install hint) on every command instead of a traceback, and `doctor` still
  renders its full checklist with the git row failed (#33).

- When the worktree root contains the repository (for example `root = ".."`),
  `list`, the teardown chooser and ref resolution no longer show the main
  checkout as a worktree, and `teardown` refuses it with `MAIN_WORKTREE`
  (exit 5) instead of deleting the repository (#35).

### Fixed

- Docker isolation now checks the [non-root user requirement](docs/install.md#what-each-isolation-mode-needs)
  before provisioning (#60).

## [1.0.1] - 2026-07-10

### Fixed

- Path-valued configuration now expands a leading `~` consistently for
  `root`, `env_file`, cache paths, `TREEBOX_*` environment variables, and the
  `--repo` / `--root` CLI flags. Relative `root` and `env_file` values remain
  repository-relative (#6).

- `create --dry-run` now enforces the same read-only preconditions as a real
  `create` - `BRANCH_EXISTS` for a name whose branch already exists,
  `SLUG_CONFLICT` for an occupied worktree directory, `NOT_FOUND` (exit 3) for
  a missing `--checkout` or base branch, `BRANCH_IN_USE` for a `--checkout`
  branch already backing another worktree - failing with the same exit codes
  and JSON errors instead of printing a plan a real run would refuse (#4).
  A half-provisioned same-name worktree previews finishing setup, mirroring
  real `create`; a dry run still changes nothing on disk or in git.

- `teardown` no longer misreports a corrupt worktree (a registered directory
  whose `.git` pointer file is missing) as `DIRTY_WORKTREE` when the *main*
  checkout has uncommitted changes: git linkage is verified before the
  dirtiness check, so the corrupt tree takes the normal confirmation path
  instead (`NEEDS_CONFIRMATION` under `--json` / non-TTY), and `--force`
  removes the directory and git's stale registration without touching the
  main checkout's files (#3). Container cleanup survives the corruption too:
  the recorded isolation and template are recovered through git's own worktree
  registration rather than the missing `.git` pointer, so a corrupt docker
  worktree is still torn down with the runner it was created with.

- `doctor` no longer renders a missing (optional) `.env` as a red `✗` failure
  row before concluding all-good: the row is now a muted `·` note marked
  `optional`, still showing the configured path (#5). Exit codes and the
  `--json` payload are unchanged.

- `treebox enter` now finishes an interrupted setup: when a prior run died
  before setup completed, `enter` re-runs setup (reporting "setup never
  completed") instead of skipping it as up-to-date because the lockfile hash
  is unchanged, and records the worktree as provisioned once it succeeds (#7).

- `teardown --json` now reports what runner cleanup actually did: when Docker
  is unavailable the worktree is still removed but the record says
  `container: "skipped"` with `volumes_removed: false` (even under
  `--remove-volumes`), and `volumes_removed` is `true` only when docker
  volumes were really removed - never on host isolation (#2).

## [1.0.0] - 2026-07-06

First stable release. treebox is Apache-2.0 licensed and ready for production
use: isolated, ready-to-run git worktrees for AI coding agents, run host-native
or inside a docker sandbox.

### Added

- `treebox template init|list|path` — scaffold and inspect operator-owned docker
  sandbox templates from any install (`uv tool` / pipx included), so customizing
  a sandbox no longer means hand-copying the shipped template directory or
  reaching into the package internals (#155).

### Changed

- **Breaking:** `treebox create NAME` now uses `NAME` directly as the branch
  name, created fresh from `origin/<base>` — no more `treebox/NAME`
  placeholder or forced rename for explicitly named work (#153). Names may
  contain slashes (`feature/auth`); the directory flattens them to `--`
  (`feature--auth`). Scripts that expected the guarded `treebox/<NAME>`
  placeholder get a directly pushable `NAME` branch instead. The `treebox/`
  prefix is rejected as a name (`INVALID_NAME`), and naming a branch that
  already exists locally or on origin is a new `BRANCH_EXISTS` conflict
  (exit 5) pointing at `--checkout`.
- The pre-push guard is now installed in **every** worktree (including
  explicit names and `--checkout`): pushing any `treebox/*` ref is always
  blocked, so generated placeholder branches must still be renamed before
  push. Nameless `create` behavior is unchanged.
- `enter REF` for a branch that exists but has no worktree now hints
  `treebox create --checkout REF` instead of the generic not-found advice.

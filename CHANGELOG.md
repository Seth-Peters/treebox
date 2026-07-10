# Changelog

All notable changes to treebox will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

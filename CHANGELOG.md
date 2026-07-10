# Changelog

All notable changes to treebox will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `create --dry-run` now enforces the same read-only preconditions as a real
  `create` - `BRANCH_EXISTS` for a name whose branch already exists,
  `SLUG_CONFLICT` for an occupied worktree directory, `NOT_FOUND` (exit 3) for
  a missing `--checkout` or base branch, `BRANCH_IN_USE` for a `--checkout`
  branch already backing another worktree - failing with the same exit codes
  and JSON errors instead of printing a plan a real run would refuse (#4).
  A half-provisioned same-name worktree previews finishing setup, mirroring
  real `create`; a dry run still changes nothing on disk or in git.
- `doctor` no longer renders a missing (optional) `.env` as a red `✗` failure
  row before concluding all-good: the row is now a muted `·` note marked
  `optional`, still showing the configured path (#5). Exit codes and the
  `--json` payload are unchanged.

## [1.0.0] - 2026-07-06

First stable release. treebox is Apache-2.0 licensed and ready for production
use: isolated, ready-to-run git worktrees for AI coding agents, run host-native
or inside a docker sandbox.

### Added

- `treebox template init|list|path` — scaffold and inspect operator-owned docker
  sandbox templates from any install (`uv tool` / pipx included), so customizing
  a sandbox no longer means hand-copying the shipped template directory or
  reaching into the package internals.

### Changed

- **Breaking:** `treebox create NAME` now uses `NAME` directly as the branch
  name, created fresh from `origin/<base>` — no more `treebox/NAME`
  placeholder or forced rename for explicitly named work. Names may
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

#!/usr/bin/env bash
# Stand up an isolated end-to-end fixture for exercising the treebox CLI by
# hand: a throwaway bare origin, a clone with a real uv project + an untracked
# .env, a scratch HOME and TREEBOX_HOME, and the controlled stub agent first on
# PATH, so nothing touches the user's ~/.treebox, ~/.claude, ~/.codex or this
# repo, and no real agent can launch.
#
# The fixture is built by tests/e2e/run.py (`fixture`), the same code the
# evidence command uses, so a manual sweep and the automated one see the same
# world.
#
# Usage:  source .claude/skills/treebox-e2e/fixture.sh     (from bash)
# After sourcing: $SBX (sandbox root), $REPO (clone), $ROOT (worktree root),
# $E2E_TEMPLATE (the offline docker template, installed in the scratch
# treebox home), and `tb <args>` runs this working tree's CLI with the
# fixture's hermetic environment. `tb` adds no output of its own. Pass
# --repo "$REPO" --root "$ROOT" on every tb call that operates on worktrees.
# Naming marker ($E2E_MARK, "e2e"): docker containers, images and volumes
# escape into the global daemon namespace as `treebox-<worktree-name>-<hash>`.
# Name every *docker* scenario worktree with the `e2e-` prefix so those become
# `treebox-e2e-*` and the reaper can find them.
# Cleanup:  e2e_cleanup      (reaps `treebox-e2e-*` docker resources, then removes $SBX)
#           e2e_reap_docker  (docker-only sweep; safe anytime, even after a crash)
set -uo pipefail

# The treebox checkout this skill lives in (.claude/skills/verify-treebox/ -> repo root).
TB_PROJECT="${TB_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)}"
export TB_PROJECT

_e2e() { (cd "$TB_PROJECT" && env -u VIRTUAL_ENV uv run --quiet python tests/e2e/run.py "$@"); }

if ! _e2e_exports="$(_e2e fixture)"; then
  echo "fixture: could not build the fixture (see above)" >&2
  # Sourced: return to the caller's shell. Run as a script: exit.
  # shellcheck disable=SC2317
  return 1 2>/dev/null || exit 1
fi
eval "$_e2e_exports"
unset _e2e_exports

# The fixture's CLI: a launcher in $SBX that sets the hermetic environment.
tb() { "$SBX/tb" "$@"; }

# Reap the docker resources a sweep created. Safe to call anytime: it removes
# only containers, images and volumes whose names start with `treebox-e2e-`
# (or the bundled template's `treebox-shellhistory-e2e-`), never a user's
# real `treebox-<name>-*` worktrees.
e2e_reap_docker() { _e2e reap; }

# One-pass teardown: docker resources (global namespace) first, then the
# sandbox. Scenarios make read-only dirs on purpose, so restore write access.
e2e_cleanup() {
  e2e_reap_docker
  chmod -R u+w "$SBX" 2>/dev/null
  rm -rf "$SBX"
}

echo "fixture ready: SBX=$SBX REPO=$REPO ROOT=$ROOT TREEBOX_HOME=$TREEBOX_HOME" >&2
echo "  cleanup: e2e_cleanup (sandbox + treebox-e2e-* docker resources)" >&2

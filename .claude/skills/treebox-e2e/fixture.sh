#!/usr/bin/env bash
# Stand up an isolated end-to-end fixture for exercising the treebox CLI:
# a throwaway bare origin, a clone with a real uv project + .env, and a
# scratch TREEBOX_HOME so nothing touches the user's ~/.treebox or this repo.
#
# Usage:  source .claude/skills/treebox-e2e/fixture.sh     (from bash)
# After sourcing: $SBX (sandbox root), $REPO (clone), $ROOT (worktree root),
# TREEBOX_HOME/TREEBOX_CONFIG point inside the sandbox, and `tb <args>` runs
# the CLI from this working tree. Pass --repo "$REPO" --root "$ROOT" on every
# tb call that operates on worktrees.
# Naming marker ($E2E_MARK, "e2e"): filesystem/git artifacts already live under
# $SBX, but docker containers/images escape into the global daemon namespace as
# `treebox-<worktree-name>-<hash>`. Name every *docker* scenario worktree with
# the `e2e-` prefix so those become `treebox-e2e-*` and the reaper can find them.
# Cleanup:  e2e_cleanup   (reaps `treebox-e2e-*` docker artifacts, then removes $SBX)
#           e2e_reap_docker  (docker-only sweep; safe anytime, even after a crash)
set -uo pipefail

# The treebox checkout this skill lives in (…/.claude/skills/treebox-e2e/ -> repo root).
TB_PROJECT="${TB_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)}"
export TB_PROJECT
# `env -u VIRTUAL_ENV` silences uv's harmless "VIRTUAL_ENV does not match the
# project environment path" warning, which otherwise lands on every command's
# stderr and muddies the stream-discipline / no-chatter checks (X1, B14).
tb() { env -u VIRTUAL_ENV uv run --project "$TB_PROJECT" treebox "$@"; }

SBX="$(mktemp -d -t treebox-e2e.XXXXXX)"
export SBX
export REPO="$SBX/repo"
export ROOT="$SBX/wts"
export E2E_MARK="e2e"   # reserved prefix for docker scenario names (see reaper below)
export TREEBOX_HOME="$SBX/treebox-home"
export TREEBOX_CONFIG="$TREEBOX_HOME/config.toml"
unset TREEBOX_TEMPLATE_DIR
mkdir -p "$TREEBOX_HOME"
: > "$TREEBOX_CONFIG"   # empty config = built-in defaults (a *missing* $TREEBOX_CONFIG is a loud exit-2 error)

git init -q --bare -b main "$SBX/origin.git"
git clone -q "$SBX/origin.git" "$REPO"
(
  cd "$REPO"
  git config user.email e2e@treebox.test
  git config user.name treebox-e2e
  printf '[project]\nname="demo"\nversion="0.1.0"\nrequires-python=">=3.14"\ndependencies=[]\n' > pyproject.toml
  uv lock -q
  printf 'SECRET=canonical\n' > .env
  git add -A && git commit -qm init && git push -q origin main
  # A second branch so --base and --checkout scenarios have a target.
  git switch -qc dev && git push -q origin dev && git switch -q main
)

# Reap the docker artifacts a sweep created. Safe to call anytime — even after
# $SBX is gone or a run crashed mid-scenario: it only touches `treebox-e2e-*`
# containers/images, never a user's real `treebox-<name>-*` worktrees. This is
# the "one big pass" cleanup for anything that escaped the sandbox dir.
e2e_reap_docker() {
  command -v docker >/dev/null 2>&1 || return 0
  local ids imgs
  ids="$(docker ps -aq --filter 'name=treebox-e2e-' 2>/dev/null)"
  [ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1
  imgs="$(docker images --format '{{.Repository}}' 2>/dev/null | grep '^treebox-e2e-' | sort -u)"
  [ -n "$imgs" ] && docker rmi -f $imgs >/dev/null 2>&1
  return 0
}

# One-pass teardown: docker artifacts (global namespace) first, then the sandbox.
e2e_cleanup() { e2e_reap_docker; rm -rf "$SBX"; }

echo "fixture ready: SBX=$SBX REPO=$REPO ROOT=$ROOT TREEBOX_HOME=$TREEBOX_HOME" >&2
echo "  cleanup: e2e_cleanup (sandbox + treebox-e2e-* docker artifacts)" >&2

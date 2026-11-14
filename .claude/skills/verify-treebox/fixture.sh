#!/usr/bin/env bash
# Stand up an isolated fixture for driving the treebox CLI by hand: a
# throwaway bare origin, a clone with a real uv project + an untracked .env, a
# scratch HOME, TREEBOX_HOME and TREEBOX_CONFIG, the offline docker template,
# and the controlled stub agent first on treebox's PATH, so nothing touches the
# user's ~/.treebox, ~/.claude, ~/.codex or this repo, and no real agent can
# launch.
#
# The fixture is built by tests/e2e/run.py (`fixture`), the same code the
# evidence command uses, so a manual drive and the automated one see the same
# world.
#
# Usage (from bash, in the treebox checkout):
#   source .claude/skills/verify-treebox/fixture.sh                  a fresh fixture
#   source .claude/skills/verify-treebox/fixture.sh --attach "$SBX"  this fixture again,
#                                                                    in a new shell
# After sourcing, `treebox` is this checkout's build, first on PATH
# ($SBX/bin/treebox): every command is the one a user types. Set:
#   $SBX $REPO $ROOT     sandbox root, fixture clone, worktree root
#   $TREEBOX_HOME $TREEBOX_CONFIG $E2E_HOME   scratch treebox home, config, HOME
#   $E2E_TEMPLATE        the offline docker template (installed in $TREEBOX_HOME)
#   $E2E_TREEBOX_BIN     the console script $SBX/bin/treebox runs
# Pass --repo "$REPO" --root "$ROOT" on every command that acts on worktrees.
# $SBX/bin/treebox passes through NO_COLOR, TERM, COLUMNS, TZ,
# TREEBOX_NON_INTERACTIVE, TREEBOX_TEMPLATE_DIR, TREEBOX_CONFIG, XDG_CACHE_HOME,
# UV_CACHE_DIR, DOCKER_HOST and the completion variables from the caller;
# E2E_PATH replaces the PATH treebox sees (start it with $SBX/safe-bin, the
# fake Keychain tool alone).
# Docker names: name every docker worktree `e2e-...`, so its container, image
# and volume are `treebox-e2e-*` and the reaper finds them.
# Helpers: e2e_doctor (read-only health check), e2e_reap_docker (remove
# `treebox-e2e-*` docker resources), e2e_cleanup (reap, then remove $SBX).
set -uo pipefail

# The treebox checkout this skill lives in (.claude/skills/verify-treebox/ -> repo root).
TB_PROJECT="${TB_PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../../.." && pwd)}"
export TB_PROJECT

_e2e() { (cd "$TB_PROJECT" && env -u VIRTUAL_ENV uv run --quiet python tests/e2e/run.py "$@"); }

if [ "${1:-}" = "--attach" ]; then
  if [ -z "${2:-}" ] || [ ! -f "$2/fixture.env" ]; then
    echo "fixture: --attach needs a fixture dir with a fixture.env (got '${2:-}')" >&2
    # shellcheck disable=SC2317
    return 1 2>/dev/null || exit 1
  fi
  # shellcheck disable=SC1091
  source "$2/fixture.env"
elif ! _e2e_exports="$(_e2e fixture)"; then
  echo "fixture: could not build the fixture (see above)" >&2
  # Sourced: return to the caller's shell. Run as a script: exit.
  # shellcheck disable=SC2317
  return 1 2>/dev/null || exit 1
else
  eval "$_e2e_exports"
  unset _e2e_exports
fi

# The fixture's treebox comes first on PATH.
case ":$PATH:" in
  *":$SBX/bin:"*) ;;
  *) PATH="$SBX/bin:$PATH" ;;
esac
export PATH
hash -r

# One read-only check: is this fixture worth driving? Prints one line per
# check and returns 1 when a hard check fails. The engine is reported, not
# required: without one, docker work is SKIP.
e2e_doctor() {
  local fail=0 py resolved src version doctor_json engine
  _e2e_ok() { printf 'ok    %s\n' "$*"; }
  _e2e_fail() { printf 'FAIL  %s\n' "$*"; fail=1; }
  _e2e_note() { printf 'note  %s\n' "$*"; }
  py="$(dirname "$E2E_TREEBOX_BIN")/python"

  resolved="$(type -P treebox)"
  if [ "$resolved" = "$SBX/bin/treebox" ]; then
    _e2e_ok "treebox on PATH is the fixture's: $resolved"
  else
    _e2e_fail "treebox on PATH is $resolved, not $SBX/bin/treebox"
  fi
  case "$E2E_TREEBOX_BIN" in
    "$TB_PROJECT"/*) _e2e_ok "it runs this checkout's build: $E2E_TREEBOX_BIN" ;;
    *) _e2e_fail "it runs $E2E_TREEBOX_BIN, outside $TB_PROJECT" ;;
  esac
  src="$("$py" -c 'import treebox; print(treebox.__file__)' 2>&1)"
  case "$src" in
    "$TB_PROJECT/src/"*) _e2e_ok "the package is imported from $src" ;;
    *) _e2e_fail "the package is imported from $src, not $TB_PROJECT/src" ;;
  esac
  version="$(treebox version 2>&1)"
  if [ "$version" = "$("$E2E_TREEBOX_BIN" version 2>&1)" ]; then
    _e2e_ok "treebox version: $version"
  else
    _e2e_fail "treebox version '$version' differs from $E2E_TREEBOX_BIN"
  fi
  while IFS= read -r other; do
    [ "$other" = "$SBX/bin/treebox" ] && continue
    _e2e_note "shadowed on PATH: $other ($("$other" version 2>/dev/null || echo '?'))"
  done < <(type -aP treebox | awk '!seen[$0]++')

  case "$TREEBOX_CONFIG" in
    "$SBX"/*) _e2e_ok "TREEBOX_CONFIG is scratch: $TREEBOX_CONFIG" ;;
    *) _e2e_fail "TREEBOX_CONFIG is $TREEBOX_CONFIG, outside $SBX" ;;
  esac
  for key in HOME TREEBOX_HOME; do
    if grep -Eq "(^| |')$key=$SBX/" "$SBX/bin/treebox"; then
      _e2e_ok "treebox runs with a scratch $key under $SBX"
    else
      _e2e_fail "treebox's $key is not under $SBX"
    fi
  done

  if doctor_json="$(treebox doctor --repo "$REPO" --json 2>&1)"; then
    _e2e_ok "treebox doctor: exit 0"
  else
    _e2e_fail "treebox doctor failed: $doctor_json"
  fi
  "$py" - "$doctor_json" "$SBX" <<'PY' || fail=1
import json, sys
rows = {c["name"]: c for c in json.loads(sys.argv[1]).get("checks", [])}
sbx = sys.argv[2]
for name, want in (("agent: claude", f"{sbx}/stub-bin/claude"), ("login: claude", f"{sbx}/home/")):
    detail = rows.get(name, {}).get("detail", "")
    ok = detail == want if name.startswith("agent") else detail.startswith(want)
    print(f"{'ok   ' if ok else 'FAIL '} {name}: {detail or 'no row'}")
    if not ok:
        sys.exit(1)
PY

  if engine="$(treebox doctor --repo "$REPO" --isolation docker --json 2>&1)"; then
    _e2e_ok "engine: reachable (docker work runs)"
  else
    _e2e_note "engine: not reachable (docker work is SKIP, never PASS):" \
      "$("$py" -c 'import json,sys; d=json.loads(sys.argv[1]); print("; ".join(c["detail"] for c in d.get("checks", []) if not c["ok"]))' "$engine" 2>/dev/null || echo "$engine")"
  fi
  for name in NO_COLOR TZ TREEBOX_NON_INTERACTIVE TREEBOX_TEMPLATE_DIR XDG_CACHE_HOME UV_CACHE_DIR DOCKER_HOST; do
    if [ -n "${!name+x}" ]; then
      _e2e_note "$name=${!name} is set in this shell and passes through to treebox"
    fi
  done
  unset -f _e2e_ok _e2e_fail _e2e_note
  return "$fail"
}

# Reap the docker resources a drive created. Safe to call anytime: it removes
# only containers, images and volumes whose names start with `treebox-e2e-`
# (or the bundled template's `treebox-shellhistory-e2e-`), never a user's
# real `treebox-<name>-*` worktrees.
e2e_reap_docker() { _e2e reap; }

# One-pass teardown: docker resources (global namespace) first, then the
# sandbox, then the fixture's entry on PATH (so a later `treebox` is never a
# dead fixture's). Scenarios make read-only dirs on purpose, so restore write
# access. Evidence you saved outside $SBX is not touched.
e2e_cleanup() {
  e2e_reap_docker
  chmod -R u+w "$SBX" 2>/dev/null
  rm -rf "$SBX"
  PATH="$(printf '%s' ":$PATH:" | sed "s#:$SBX/bin:#:#g; s#^:##; s#:\$##")"
  export PATH
  hash -r
  unset SBX REPO ROOT E2E_HOME TREEBOX_HOME TREEBOX_CONFIG E2E_TEMPLATE E2E_TREEBOX_BIN E2E_MARK
}

echo "fixture ready: SBX=$SBX REPO=$REPO ROOT=$ROOT TREEBOX_HOME=$TREEBOX_HOME" >&2
echo "  treebox: $SBX/bin/treebox (this checkout's build; run e2e_doctor before driving)" >&2
echo "  new shell: source $TB_PROJECT/.claude/skills/verify-treebox/fixture.sh --attach $SBX" >&2
echo "  cleanup: e2e_cleanup (sandbox + treebox-e2e-* docker resources)" >&2

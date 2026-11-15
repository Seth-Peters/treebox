#!/usr/bin/env bash
# The fixture guard (`e2e_guard` after sourcing fixture.sh): exits 0 only when
# this shell drives a verify-treebox fixture. A lost fixture (fixture.sh
# sourced in a pipeline or a subshell, or a new shell without --attach)
# leaves `treebox` resolving to the release on PATH with the real HOME, which
# writes into the real ~/.treebox. Run it at the top of every shell block or
# script that drives treebox: `e2e_guard || exit 1`.
#
# It checks, read-only:
#   - SBX is set and is a fixture (it has fixture.env);
#   - `type -P treebox` is $SBX/bin/treebox;
#   - that launcher gives treebox a HOME and a TREEBOX_HOME under $SBX;
#   - TREEBOX_HOME and TREEBOX_CONFIG in this shell are under $SBX
#     (TREEBOX_CONFIG passes through to treebox).
set -u

lost() {
  echo "e2e_guard: FIXTURE LOST: $*" >&2
  echo "  Do not run treebox in this shell: it is not the fixture's, and it would use your real HOME and ~/.treebox." >&2
  echo "  Attach to the fixture: source .claude/skills/verify-treebox/fixture.sh --attach \"\$SBX\" (the path is in its 'fixture ready' line)," >&2
  echo "  or build a new one: source .claude/skills/verify-treebox/fixture.sh (from the treebox checkout, in bash, not in a pipeline or a subshell)." >&2
  exit 1
}

[ -n "${SBX:-}" ] || lost "SBX is not set: fixture.sh was not sourced in this shell"
[ -f "$SBX/fixture.env" ] || lost "$SBX is not a fixture (no fixture.env): it was cleaned up or never built"
resolved="$(type -P treebox || true)"
[ "$resolved" = "$SBX/bin/treebox" ] || lost "treebox on PATH is '${resolved:-not found}', not $SBX/bin/treebox"
for key in HOME TREEBOX_HOME; do
  grep -Eq "(^| |')$key=$SBX/" "$SBX/bin/treebox" || lost "$SBX/bin/treebox does not give treebox a $key under $SBX"
done
for key in TREEBOX_HOME TREEBOX_CONFIG; do
  value="${!key:-}"
  case "$value" in
    "$SBX"/*) ;;
    *) lost "$key is '${value}', not under $SBX" ;;
  esac
done
echo "e2e_guard: ok: treebox is $SBX/bin/treebox; its HOME, TREEBOX_HOME and TREEBOX_CONFIG are under \$SBX" >&2

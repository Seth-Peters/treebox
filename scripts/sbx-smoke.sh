#!/usr/bin/env bash
# Manual smoke test for --isolation sbx against a REAL Docker Sandboxes install.
# Not part of validate.sh/CI (needs sbx, a Docker login and a microVM-capable
# host). Skips cleanly (exit 0) when sbx is absent or not usable.
#
#   scripts/sbx-smoke.sh
#
# Creates a throwaway repo + origin under a temp dir, runs create/enter/teardown
# through the real runner, and checks what the sandbox actually sees: identical
# absolute paths, working git, a read-only hooks dir, and a clean teardown.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v sbx >/dev/null 2>&1; then
  echo "skip: sbx not on PATH"; exit 0
fi
if ! sbx ls --json >/dev/null 2>&1; then
  echo "skip: sbx is installed but not usable (run 'sbx login' / 'sbx diagnose')"; exit 0
fi

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/treebox-sbx-smoke.XXXXXX")"
ROOT="$(cd "$ROOT" && pwd -P)"
TB=(uv run treebox)
NAME="smoke-$$"
export TREEBOX_HOME="$ROOT/tbhome" TREEBOX_CONFIG="$ROOT/config.toml"
: > "$TREEBOX_CONFIG"

cleanup() {
  "${TB[@]}" teardown "$NAME" --repo "$ROOT/repo" --root "$ROOT/wts" --force >/dev/null 2>&1 || true
  rm -rf "$ROOT"
}
trap cleanup EXIT

pass() { echo "ok   $*"; }
fail() { echo "FAIL $*" >&2; exit 1; }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@e GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@e
git -c init.defaultBranch=main init -q --bare "$ROOT/origin.git"
git -c init.defaultBranch=main clone -q "$ROOT/origin.git" "$ROOT/repo" 2>/dev/null
git -C "$ROOT/repo" commit -q --allow-empty -m init
git -C "$ROOT/repo" push -q origin main

OUT="$("${TB[@]}" create "$NAME" --repo "$ROOT/repo" --root "$ROOT/wts" --isolation sbx --print 2>"$ROOT/create.err")" \
  || { cat "$ROOT/create.err" >&2; fail "create"; }
WT="$ROOT/wts/$NAME"
SB="$(sed -n 's/^sbx run --name \(.*\)$/\1/p' <<<"$OUT")"
[[ -n "$SB" ]] || fail "create did not print an sbx run command: $OUT"
pass "create → $SB"

sbx ls --json | grep -q "\"$SB\"" || fail "sandbox not listed"
pass "sandbox listed"

[[ "$(sbx exec -w "$WT" "$SB" pwd)" == "$WT" ]] || fail "worktree not at identical path"
pass "worktree mounted at identical absolute path"

sbx exec -w "$WT" "$SB" git status -sb >/dev/null || fail "git status inside"
sbx exec -w "$WT" "$SB" git -c user.name=t -c user.email=t@e commit --allow-empty -qm inbox \
  || fail "git commit inside"
[[ "$(git -C "$WT" log -1 --format=%s)" == "inbox" ]] || fail "commit not visible on host"
pass "git works inside; commit visible on host"

COMMON="$(git -C "$WT" rev-parse --path-format=absolute --git-common-dir)"
if sbx exec "$SB" touch "$COMMON/hooks/planted" 2>/dev/null; then
  fail "hooks/ is writable inside the sandbox"
fi
pass "hooks/ is read-only inside"

"${TB[@]}" enter "$NAME" --repo "$ROOT/repo" --root "$ROOT/wts" --print >/dev/null 2>&1 \
  || fail "enter --print"
pass "enter --print"

sbx stop "$SB" >/dev/null
"${TB[@]}" enter "$NAME" --repo "$ROOT/repo" --root "$ROOT/wts" --print >/dev/null 2>&1 \
  || fail "enter --print with stopped sandbox"
pass "enter tolerates a stopped sandbox"

"${TB[@]}" teardown "$NAME" --repo "$ROOT/repo" --root "$ROOT/wts" --force 2>"$ROOT/teardown.err" \
  || { cat "$ROOT/teardown.err" >&2; fail "teardown"; }
if sbx ls --json | grep -q "\"$SB\""; then fail "sandbox survived teardown"; fi
pass "teardown removed the sandbox"

echo "sbx-smoke: all checks passed"

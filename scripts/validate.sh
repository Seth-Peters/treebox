#!/usr/bin/env bash
# Reproducible validation: lint, type checks, shell asset checks, unit +
# integration tests, golden CLI snapshots, and a live host-runner smoke test
# against a throwaway local git remote with a real uv project.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "== ruff =="
uv run --extra dev ruff check src tests
uv run --extra dev ruff format --check src tests

echo "== mypy =="
uv run --extra dev mypy

echo "== shell assets =="
shell_files=(src/treebox/assets/container/*.sh src/treebox/assets/pre-push scripts/*.sh)
for f in "${shell_files[@]}"; do bash -n "$f"; done
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "${shell_files[@]}"
else
  echo "shellcheck not installed; syntax check (bash -n) only" >&2
fi

echo "== pytest =="
uv run --extra dev python -m pytest

echo "== golden snapshots =="
scripts/golden-diff.sh

echo "== live host-runner smoke =="
SBX="$(mktemp -d)"
trap 'rm -rf "$SBX"' EXIT
git init -q --bare -b main "$SBX/origin.git"
git clone -q "$SBX/origin.git" "$SBX/repo"
(
  cd "$SBX/repo"
  git config user.email t@e && git config user.name t
  printf '[project]\nname="demo"\nversion="0.1.0"\nrequires-python=">=3.14"\ndependencies=[]\n' > pyproject.toml
  uv lock -q
  printf 'SECRET=canonical\n' > .env
  git add -A && git commit -qm init && git push -q origin main
)

unset TREEBOX_CONFIG
out="$(uv run treebox create feature-smoke --repo "$SBX/repo" --root "$SBX/wts" --isolation host --print)"
wt="$SBX/wts/feature-smoke"
[[ -d "$wt/.venv" ]]                         || { echo "FAIL: no .venv"; exit 1; }
[[ "$(cat "$wt/.env")" == "SECRET=canonical" ]] || { echo "FAIL: .env not copied"; exit 1; }
[[ "$out" == *claude* ]]                     || { echo "FAIL: no launch command"; exit 1; }
[[ "$(git -C "$wt" branch --show-current)" == "feature-smoke" ]] \
  || { echo "FAIL: explicit name is not the branch"; exit 1; }
uv run treebox list --repo "$SBX/repo" --root "$SBX/wts" --json | grep -q '"deps": "fresh"' \
  || { echo "FAIL: deps not fresh"; exit 1; }

echo "== pre-push guard =="
# The guard rides every worktree: a treebox/* ref must be rejected while the
# real branch pushes freely — the machine prefix can never reach the remote.
git -C "$wt" branch treebox/scratch
rc=0; git -C "$wt" push -q origin treebox/scratch 2>"$SBX/push.err" || rc=$?
[[ $rc -ne 0 ]]                              || { echo "FAIL: treebox/* push was allowed"; exit 1; }
grep -q "placeholder branch" "$SBX/push.err" || { echo "FAIL: guard message missing"; cat "$SBX/push.err"; exit 1; }
git -C "$wt" push -q origin feature-smoke    || { echo "FAIL: named branch could not push"; exit 1; }
# Re-creating an existing branch is a BRANCH_EXISTS conflict (exit 5).
rc=0; uv run treebox create feature-smoke --repo "$SBX/repo" --root "$SBX/wts2" 2>/dev/null || rc=$?
[[ $rc -eq 5 ]]                              || { echo "FAIL: existing branch != exit 5 (got $rc)"; exit 1; }
# Renames stay legal and tracked: teardown (below) resolves the NAME after one.
git -C "$wt" branch -m smoke-named

echo "== agent-facing behaviors =="
# --dry-run must change nothing and list the git command it would run.
uv run treebox create plan-x --base main --repo "$SBX/repo" --root "$SBX/wts" --dry-run --json \
  | grep -q '"dry_run": true' || { echo "FAIL: dry-run json"; exit 1; }
[[ ! -e "$SBX/wts/plan-x" ]]                 || { echo "FAIL: dry-run created a worktree"; exit 1; }
# Stable exit codes: usage (2) and not-found (3). (|| rc=$? keeps set -e happy.)
rc=0; uv run treebox create "bad name" --repo "$SBX/repo" --root "$SBX/wts" 2>/dev/null || rc=$?
[[ $rc -eq 2 ]]                              || { echo "FAIL: invalid name != exit 2 (got $rc)"; exit 1; }
rc=0; uv run treebox enter ghost-x --repo "$SBX/repo" --root "$SBX/wts" 2>/dev/null || rc=$?
[[ $rc -eq 3 ]]                              || { echo "FAIL: missing worktree != exit 3 (got $rc)"; exit 1; }
# Structured JSON error on stderr in --json mode.
uv run treebox enter ghost-x --repo "$SBX/repo" --root "$SBX/wts" --json \
  >/dev/null 2>"$SBX/err.json" || true
grep -q '"code": "NOT_FOUND"' "$SBX/err.json" \
  || { echo "FAIL: no structured json error"; cat "$SBX/err.json"; exit 1; }

# teardown resolves the name even after the branch was renamed.
uv run treebox teardown feature-smoke --repo "$SBX/repo" --root "$SBX/wts" --force >/dev/null
[[ ! -e "$wt" ]]                             || { echo "FAIL: teardown left worktree"; exit 1; }

echo "== all validation passed =="

#!/usr/bin/env bash
# Live end-to-end smoke test of the factory against Claude Managed Agents.
#
#   factory/scripts/smoke.sh <issue-number> [ref]
#
# Applies the factory definition, stores the worker's GitHub token, delegates
# the issue, waits for the session to settle, and exports the full transcript
# (every event and subagent thread) to tmp/factory-smoke/issue-<n>/ with a log.
# Needs: ant (logged in), gh (logged in), uv, and FACTORY_GITHUB_TOKEN.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

issue="${1:?usage: smoke.sh <issue-number> [ref]}"
ref="${2:-$(git branch --show-current)}"
out="tmp/factory-smoke/issue-${issue}"
mkdir -p "$out"
exec > >(tee "$out/smoke.log") 2>&1

factory() { uv run --quiet --project factory factory "$@"; }
step() { printf '\n== %s ==\n' "$*"; }

step "preflight"
for tool in ant gh uv git; do
  command -v "$tool" >/dev/null || { echo "missing: $tool"; exit 1; }
done
[[ -n "${FACTORY_GITHUB_TOKEN:-}" ]] || { echo "set FACTORY_GITHUB_TOKEN first"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not logged in"; exit 1; }
echo "ref: $ref · issue: #$issue · $(date -u +%FT%TZ)"

step "factory check"
factory check

step "ant apply"
(cd factory && ant apply .)
echo "lockfile:"; cat factory/claude-lock.json

step "store the worker's GitHub token in the vault"
factory vault set-github-token | grep -v -i token || true

step "delegate issue #$issue"
factory delegate --issue "$issue" --ref "$ref" | tee "$out/delegate.json"

step "wait for the session to settle (up to 90 min)"
factory status --issue "$issue" --wait --timeout 5400 --interval 30 | tee "$out/status.json"

step "export the full transcript"
factory transcript --issue "$issue" --out "$out/sessions" | tee "$out/transcript.json"

step "pull requests for issue #$issue"
gh pr list --state all --search "$issue in:body" \
  --json number,url,title,isDraft,baseRefName,headRefName,labels | tee "$out/prs.json"

step "done"
echo "Evidence in $out/. Commit factory/claude-lock.json when you're happy:"
echo "  git add factory/claude-lock.json && git commit -m 'chore(factory): apply' && git push"

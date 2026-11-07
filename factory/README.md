# treebox software factory

A small, code-defined factory: a developer hands over a spec, and the factory
implements it, verifies it end to end, has a fresh-context reviewer try to
break it, and opens a draft PR. An outer loop reads the full transcripts of
those runs and improves the factory itself.

```
developer (Claude Code, spec in hand)
   │  delegate skill → factory delegate
   ▼
GitHub issue (label `factory`) ──► Managed Agents worker session (cloud sandbox, repo mounted)
   ▲                                 implement → verify → review (reviewer subagent) → fix ≤2
   │  factory steer (mid-run)        └─► draft PR "Closes #N"
   │
improve-factory (manual, after many runs): full transcripts + issues/PRs → retro → one PR to factory code
```

Every task is one GitHub issue (the record); every attempt at it is one
session (`metadata.issue = N`); the PR closes the issue.

## What's in here

Everything here is the factory. The outer loop changes these files; a human
merges and applies them.

| Path | What it defines |
|---|---|
| `factory.yaml` | Manifest: name, **version**, repo, task label/branch/budget, what counts as factory code |
| `agents/engineer.md` | Worker agent (model, tools, GitHub MCP, reviewer subagent) and system prompt |
| `agents/reviewer.md` | Adversarial reviewer subagent: read-only, prompt only, no rubric |
| `environments/treebox.yaml` | The sandbox: packages and networking (the "Dockerfile") |
| `vaults/github-worker.yaml` | Vault holding the GitHub token the worker's MCP server uses |
| `claude-lock.json` | Written by `ant apply`: resource IDs and agent versions. Commit it; never hand-edit |
| `scorers/` | Outer-loop scorers. Empty for now |
| `retros/` | One file per outer-loop run; history, never edited |
| `src/factory_cli/` | The `factory` CLI |

Skills live in the repo's `.claude/skills/`, loaded by the cloud worker and by
your local Claude Code session: `delegate`, `implement`, `verify`, `review`,
`improve-factory`, and `grilling` (vendored from Matt Pocock's skills, MIT).

## One-time setup

1. **API access.** `brew install anthropics/tap/ant`, then `ant auth login`.
   The CLI uses that login; no API key needed.
2. **GitHub.** `gh auth login`, and a fine-grained PAT for this repo only
   (Contents and Pull requests: read/write) exported as `FACTORY_GITHUB_TOKEN`.
   The worker clones and pushes with it; never commit it.
3. **Apply the factory** and commit the lockfile:
   ```bash
   cd factory && ant apply . && cd ..
   uv run --project factory factory vault set-github-token
   git add factory/claude-lock.json && git commit -m "chore(factory): apply" && git push
   ```

## Use it

From Claude Code, say "delegate this" with your spec. The `delegate` skill runs:

```bash
uv run --project factory factory delegate --title "<title>" --brief-file spec.md
uv run --project factory factory delegate --issue 61            # an existing issue
uv run --project factory factory steer --issue 61 "also cover --json"
uv run --project factory factory status --issue 61 [--wait]
uv run --project factory factory transcript --issue 61 --out /tmp/runs
```

The worker runs the factory code on `origin/main`, so `delegate` refuses while
your local factory files differ from it (`--allow-unpushed` overrides).
`--ref <branch>` runs against another branch instead, which is how an
unmerged factory change is tried before it lands. `--dry-run` prints the exact
request and first message.

## Improve it

After enough runs (the skill wants at least 5), ask Claude Code to "improve
the factory". `improve-factory` reads every run's full transcript (`factory
transcript`) and its issue and PR (`gh`), writes `retros/<date>.md`, makes one
change to factory code, bumps `version`, checks it stays in bounds (`factory
check --boundary`), and opens a PR. After you merge it, run `ant apply .` here
and commit the lockfile.

## Develop the CLI

```bash
make factory-test     # ruff, mypy --strict, pytest (in factory/)
make factory-check    # validate the factory definition
make factory-plan     # ant apply --dry-run
```

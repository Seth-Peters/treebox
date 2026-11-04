# treebox software factory

A small, code-defined factory that turns a task brief into a reviewed draft PR,
plus an outer loop that improves the factory itself.

```
anyone (dev plan, Slack request, bug report)
   │  `delegate` skill shapes a brief
   ▼
factory run ──► Claude Managed Agents session (fresh cloud sandbox, repo mounted)
                 engineer: implement ─► reviewer subagent (fresh context) ─► fix ≤2 rounds
                 ─► draft PR, label `factory`
   ▼
human reviews + merges

… after enough runs …

improve-factory skill: transcripts (ant) + PRs (gh) ─► retro ─► one PR changing factory code
```

## What's in here

Everything here is the factory. The outer loop changes these files; a human
merges and applies them.

| Path | What it defines |
|---|---|
| `factory.yaml` | Manifest: name, **version**, repo, session defaults, what counts as factory code |
| `agents/engineer.md` | Worker agent (model, tools, GitHub MCP, reviewer subagent) and system prompt |
| `agents/reviewer.md` | Adversarial reviewer subagent: read-only, prompt only, no rubric |
| `environments/treebox.yaml` | Cloud sandbox: packages and networking |
| `vaults/github.yaml` | Vault holding the GitHub token the MCP server uses |
| `prompts/task.md` | First message each session receives (wraps the brief) |
| `claude-lock.json` | Written by `ant apply`: resource IDs and agent versions. Commit it; never hand-edit |
| `specs/` | Optional home for longer briefs worth keeping |
| `scorers/` | Outer-loop scorers. **Empty for now**; scoring is a planned addition, separate from the adversarial review |
| `retros/` | One file per outer-loop run; history, never edited |
| `src/factory_cli/` | The `factory` CLI: `run`, `check`, `vault set-github-token`, `version` |

The skills live in the repo's `.claude/skills/`, where both the cloud worker
and your local Claude Code session load them: `delegate`, `implement`,
`review`, `improve-factory`, and `grilling` (vendored from Matt Pocock's
skills, MIT).

## One-time setup

1. **API credits.** Max, Ultra, and Team plans include monthly Claude API
   credits that cover Managed Agents: claude.ai → Settings → Billing → API
   credits → link a Console organization. Create a `treebox-factory` workspace
   with a spend limit.
2. **CLI.** `brew install anthropics/tap/ant` (v1.40+), then `ant auth login`.
3. **GitHub token.** A fine-grained PAT for `Seth-Peters/treebox` only, with
   Contents and Pull requests set to read/write. Keep it in your shell, e.g.
   `export FACTORY_GITHUB_TOKEN=...` (never commit it).
4. **Apply the factory**, from this directory, and commit the lockfile:
   ```bash
   cd factory && ant apply . && cd ..
   uv run --project factory factory vault set-github-token
   git add factory/claude-lock.json && git commit -m "chore(factory): apply v0.1.0"
   git push
   ```

## Use it

From a Claude Code session in this repo, say "delegate this" with any brief:
a plan, a pasted Slack message, a one-liner. The `delegate` skill shapes it and
runs:

```bash
uv run --project factory factory run --slug <slug> --requested-by <who> \
  --source <dev|slack|po|...> --brief-file <path>     # or --brief, or stdin
```

`--dry-run` prints the exact session request and first message. The worker
always runs the factory code on `origin/main`, so `factory run` refuses to
dispatch while your local factory files differ from it (`--allow-unpushed`
overrides).

Watch a run with `ant beta:sessions:events stream --session-id <id> --format jsonl`,
or in the Claude Console.

## Improve it

When enough runs have accumulated (the skill wants at least 5), ask Claude
Code to "improve the factory". The `improve-factory` skill reads transcripts
with `ant` and PRs with `gh`, writes `retros/<date>.md`, makes one change to
factory code, bumps `version`, checks the change stays in bounds
(`factory check --boundary`), and opens a PR. After you merge it, re-run
`ant apply .` here and commit the lockfile.

## Develop the CLI

```bash
make factory-test     # ruff, mypy --strict, pytest (in factory/)
make factory-check    # validate the factory definition
make factory-plan     # ant apply --dry-run
```

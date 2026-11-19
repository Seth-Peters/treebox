# Implement

Build the change, or revise it. You do not certify it: the verify stage does.

## A new change

1. Read the contract: the approved spec and its claims, or the request and
   the triage findings. Read the code and tests you will touch.
2. For a bug, start from the triage reproduction. Add a regression test that
   fails before the fix and passes after it.
3. Make the smallest change that meets the contract. Follow the existing
   patterns: the registries, the typed dataclasses and the module boundaries
   in `CLAUDE.md`.
4. Add or update the tests that would fail without your change.
5. For each new or changed route (a command, flag, error code, config key or
   environment variable), update the feature map in the same change:
   `.claude/skills/verify-treebox/features/`, `references/routes.md`, and a
   scenario in `tests/e2e/scenarios.py`. `tests/test_e2e_map.py` fails until
   you do.
6. Run `./scripts/validate.sh` and fix what it finds. If a golden snapshot
   changed because the contract changes CLI output, update it with
   `scripts/golden-diff.sh --update` and say so in the PR. Never update a
   snapshot to make an unintended change pass.
7. Push to the branch and open a draft PR, or update the spec PR when one
   exists. Report the pushed head commit SHA.

## A revision

Verify failures and review findings arrive verbatim. For each one, make the
change, or say in your report why you did not. Work on the same branch and
PR. Add 1 to `fix_rounds`. Push, and report the new head commit SHA: the
earlier evidence is now stale, so the revision goes to verify again.

## The PR body

```markdown
Closes #<issue>

## What changed
<2-5 bullets>

## Assumptions
<anything you decided that the contract did not say, or "None">

## Verification
<filled from the verify stage outputs: verdict, commit SHA, evidence block, skips>

## Review
<filled from the review stage outputs>
```

Leave `Verification` as "pending" until the verify stage reports. Fill it
only with the verifier's outputs, word for word, for the commit it names.

## Outputs

Set `review_decision` to `skip` only when the full effect of the change is
obvious on sight, or the user asked to skip review. When in doubt, `review`.
Set `needs_triage` when trivial work turned out not to be trivial.

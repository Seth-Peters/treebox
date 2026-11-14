---
name: verify-treebox
description: Prove a factory change works end to end and breaks nothing, with evidence. Use when you are the factory engineer and your change is implemented, and again after every fix round.
---

# Verify the change

A change isn't done because the code looks right. It's done when the real
application was exercised and the evidence says so. Never claim a check passed
that you didn't run and see pass.

## 1. The deterministic gate

```bash
./scripts/validate.sh
```

Lint, strict types, the full test suite, golden CLI snapshots, shell asset
checks, and a live host-runner smoke test. All of it must pass. If a golden
snapshot changed because the task intentionally changes CLI output, update it
with `scripts/golden-diff.sh --update` and say so in the PR; never update
snapshots to make an unintended change pass.

## 2. Exercise the changed behavior for real

Follow the `treebox-e2e` skill against its throwaway fixture (never the real
repo or `~/.treebox`, and never launch a real agent). Scope it to the scenario
groups your change touches plus the cross-cutting invariants X1 to X5; run the
full matrix when you changed shared code (`cli.py`, `provision.py`, `output.py`,
runners). If the change adds behavior no scenario covers, run it by hand in the
fixture and record the command and result.

This sandbox has no docker daemon, so group H is **SKIP (no docker daemon)**.
Skipped is not passed: say so in the evidence.

## 3. Record the evidence

Keep it for the PR body, in this shape:

```markdown
## Verification
- `./scripts/validate.sh`: pass (N tests)
- treebox-e2e groups: B, E, X1-X5: N pass / 0 fail · H skipped (no docker daemon)
- Changed behavior, exercised:
  - `tb teardown ghost --json` → exit 3, `error.code: NOT_FOUND` (was exit 1)
```

If anything fails and you can't fix it, stop iterating on it after a genuine
attempt, and put the failure verbatim in the PR body. A failed check reported
honestly is useful; a hidden one isn't.

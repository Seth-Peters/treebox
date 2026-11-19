# Verify

Prove the change at the target commit, on the real CLI, the way a user
drives it. The `verify-treebox` skill is the procedure; read it in full
before the first command. This prompt adds only what the stage needs.

## Procedure

1. **Check out the target.** Check out the head commit SHA from your inputs
   and confirm `git rev-parse HEAD` matches it. This SHA is your `commit`
   output. If the branch moved, verify the SHA you were given and say so.
2. **Read the contract, not the diff, first.** List the claims you must
   prove. With no spec, derive them from the request and the triage
   reproduction. Then read the diff to find what else the change can break.
3. **Run the deterministic gate:** `./scripts/validate.sh`.
4. **Choose the depth** from the skill's "When to run what" section: the
   feature files the change touches and their scenarios, plus X1 to X5; the
   full sweep when shared code changed. With a docker engine, a change to
   the runners or the sandbox assets runs with `--require docker`.
5. **Drive each claim by hand** in the fixture, one command per claim. Keep
   the command, stdout, stderr, the exit code and the resulting state (the
   read-only second view). For a bug, run the triage reproduction drive: it
   must now pass.
6. **Check the feature map.** A new or changed route with no row in
   `references/routes.md` or no scenario is a failure of class harness gap.
7. **Clean up** as the skill says, after you saved the evidence outside the
   fixture.

## Verdict

- `verified`: the gate passed, no scenario failed, and you saw every claim
  hold. A claim you could not exercise is not verified: see `blocked`.
- `failed`: a claim did not hold, or a scenario or the gate failed. Report
  per failure the scenario id or claim, the command, expected, actual, and
  one class: product bug, doc drift, harness gap, or environment.
- `blocked`: the environment stopped you (for example the fixture cannot be
  built), or a claim needs a prerequisite this sandbox does not have (for
  example a docker engine for a change to the docker runner). Say exactly
  what is missing and what went unexercised.

A known-bug verdict from the sweep is not a failure of this change. List it.

## Evidence

Return the evidence in the skill's PR-body shape:

```markdown
## Verification
Verdict: <verified | failed | blocked> at <commit SHA>
- `./scripts/validate.sh`: <pass (N tests) | fail>
- e2e evidence (`run.py evidence <flags>`): N pass / N fail / N skip
- Skipped: <each skip and what went unexercised, or "None">
- Features driven (verify-treebox): <features>
- Claims, exercised:
  - `<command>` -> <exit code, JSON field, state> (<was ...>)
```

Do not fix anything. Do not edit product code, tests, snapshots or the
feature map, and do not push. If you see how to fix a failure, put that in
the failure's report.

# Verification and CI

treebox depends on docker behaviour that unit tests cannot see, and its
contributors are mostly agents. These decisions say what must run before a
merge or a release, what verification may never do, and which machine's
answer counts.

## Decisions

### The docker sweep runs on every product change, behind one always-reporting check

Any product module can break docker behaviour, not only the runners, so the
end-to-end sweep runs whenever a pull request touches the product, its
dependencies, the e2e suite or the verify skill. Running it only for runner
and asset paths plus nightly was the earlier plan and was rejected: a docker
regression must be known before merge. Requiring the sweep jobs directly was
also rejected, because a skipped job never reports and a docs-only pull
request could then never merge. So a small `scope` job decides, and one
`e2e` check always reports: green when the sweep passed or was out of scope.
`e2e` is the check to require on `main`.

Decided: 20 November 2026, PR #106.
Where: `.github/workflows/e2e.yml` · jobs `scope` and `e2e`

### Routine verification never launches a real agent, and a missing prerequisite is a skip

The end-to-end scenarios drive the real CLI and, where Docker is reachable,
the real engine, with an in-repo stub agent and an offline template. No
routine run starts an autonomous agent or needs real credentials. A scenario
whose prerequisite is missing reports SKIP with the reason, never pass.
Evidence bundles are written outside the repo and never committed.

Decided: 12 November 2026, issue #65, PR #82.
Where: `tests/e2e/run.py`, `tests/e2e/stub-agent`,
`tests/e2e/offline-template/`

### A manual release run never reaches PyPI by default

A manually started release publishes to TestPyPI unless `index = pypi` is
chosen on a `v*` tag; an empty or missing input can never select PyPI, and
one run publishes to one index. The release run validates the exact tagged
commit itself, because a tag push does not run CI and can point anywhere;
that costs a full CI matrix per release. Open: a tag push still publishes to
PyPI automatically, and whether it should was never decided.

Decided: 13 November 2026, PR #80.
Where: `.github/workflows/release.yml`

### Verification uses the CLI itself and deterministic checks

treebox is already a CLI with help text, so verification drives `treebox`
directly: no wrapper CLI and no shell alias. Where an agent kept making the
same mistake (the verify fixture was lost three times and wrote to the home
directory), the fix is a check that fails (`e2e_guard`), not more
instruction text in the skill.

Decided: 14 and 15 November 2026, PRs #97 and #102.
Where: `.claude/skills/verify-treebox/guard.sh`, `fixture.sh`

### A Warp-hosted engine is not an authority for docker tests

The Warp factory's sandbox runs a Docker engine that ignores `readonly` on
bind mounts: a container wrote into a mount declared read-only. treebox's
security checks cannot mean anything there, so the attempt to run them in
the Warp runner (PR #108) was closed. GitHub's Linux runner is the authority
for docker behaviour. Revisit if Warp's engine changes.

Decided: 20 November 2026, PR #108 (closed).
Where: `.warp-factory/`; `.github/workflows/e2e.yml`

# Contributing

treebox is a small, one-maintainer project. Contributions are welcome, but the scope is intentionally modest.

## Good contributions

- Bug fixes with a clear reproduction.
- Documentation fixes that make install, usage, or runner behavior clearer.
- Small compatibility fixes that preserve the existing CLI and scripting behavior.

For larger feature ideas, open an issue first so we can decide whether they fit the project.

A good contribution has also had a fair round of review before it lands — by you, by an AI reviewer, or preferably both.

## Local checks

Install the pre-commit hooks once so every commit is auto-formatted, lint-fixed,
and type-checked in strict mode (`ruff format`, `ruff check --fix`, and `mypy`):

```bash
uv run --extra dev pre-commit install
```

Note: `pre-commit install` has no effect inside a treebox worktree, because
treebox sets `core.hooksPath` for its own pre-push guard, so git ignores the
hook pre-commit writes to `.git/hooks`. Run the hooks manually there with
`uv run --extra dev pre-commit run --all-files`.

If the hooks are skipped, the Autofix CI workflow applies the same `ruff --fix`
and `ruff format` to same-repo PRs and pushes the result back. Note that pushes
made with the default `GITHUB_TOKEN` do not re-trigger CI, so if this repo ever
requires the CI status checks under branch protection, the auto-fixed commit
will have no reported checks and the PR will stall; the remedy is to push from
the workflow using a PAT or GitHub App token stored as a repo secret (e.g.
`AUTOFIX_TOKEN`) instead of `GITHUB_TOKEN`.

For code changes, run the relevant checks before opening a PR. Mypy runs in
strict mode (configured in `pyproject.toml`):

```bash
uv run --extra dev python -m pytest
uv run --extra dev ruff check src tests
uv run --extra dev ruff format --check src tests
uv run --extra dev mypy
```

For changes that touch provisioning, runners, git handling, or CLI output, run
the full validation gate: lint, type checks, shell asset checks, tests, the
golden CLI-output snapshots in `tests/golden/`, and a live host-runner smoke
test against a throwaway local remote:

```bash
./scripts/validate.sh
```

For changes to the runners or the sandbox assets, also run the end-to-end
scenarios. One command drives every scenario through the real CLI (and, when
Docker is reachable, the real engine, with an offline template and a stub
agent: no real agent ever launches) and writes an evidence bundle outside the
repository. The `E2E evidence` workflow runs the same command on such pull
requests and uploads the bundle; link that run in your PR. See the
`verify-treebox` skill (`.claude/skills/verify-treebox/SKILL.md`), whose
feature map (`features/`) also shows how to drive each feature by hand.

```bash
uv run python tests/e2e/run.py evidence --out "$TMPDIR/treebox-evidence"
```

For bundled sandbox changes, also use the opt-in Docker checks described in
[tests/test_sandbox_image.py](tests/test_sandbox_image.py). The normal validation
gate skips these image checks unless you enable them.

For behavior-preserving refactors, `scripts/golden-diff.sh` is the focused
snapshot-only gate. Refresh snapshots only after you've deliberately accepted a
behavior change.

```bash
scripts/golden-diff.sh
```

For docs-only changes, this is enough:

```bash
uv run --extra docs mkdocs build --strict
```

## Commit messages

treebox uses [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary`, imperative and lowercase. Common types are `feat`,
`fix`, `docs`, `refactor`, `test`, and `chore`.

```
fix: copy .env before the runner setup step
feat(runners): add a podman isolation backend
docs: document the template command
```

**Breaking changes** take a `!` (`feat!:`, `fix!:`) or a `BREAKING CHANGE:`
footer. treebox's public contract is its CLI surface, its `--json` payloads
(which carry a `schemaVersion`), and its exit codes: if you change that
observable output, bump `SCHEMA_VERSION` when the JSON shape changes and
regenerate the golden snapshots with `scripts/golden-diff.sh --update`. A new
`error.code` also needs its row in the error-code table in `docs/usage.md`;
`tests/test_contract.py` fails until the table and the code agree.

The PR title becomes the squash-commit title, and that title is what the
changelog is written from, so make it describe the user-facing change.

## Changelog

Do not edit `CHANGELOG.md` in a pull request. Its entries are written at
release time, from the squash-commit titles of everything merged since the
last tag, so it has no `[Unreleased]` section.

## Releasing

First write the `CHANGELOG.md` entry for everything merged since the last tag:
list it with `git log --oneline <last-tag>..origin/main`, add a
`## [X.Y.Z] - YYYY-MM-DD` section with one item per user-facing change
(cite the pull request number from the squash-commit title), and merge it
to `main`. Then release that commit as described below.

The `Release` workflow (`.github/workflows/release.yml`) publishes treebox.
It first runs the full CI validation (`ci.yml`: the test matrix and
`./scripts/validate.sh`) on the exact commit it releases. It publishes only if
all of that passes.

- **Release to PyPI:** push a tag such as `v0.4.0` on the commit to release.
  The workflow validates that commit, builds version `0.4.0`, publishes it to
  PyPI, and then creates the GitHub Release.
- **Dev build to TestPyPI:** in the Actions tab, open "Release", select "Run
  workflow", select the branch in "Use workflow from", and keep "Package
  index" at its default, `testpypi`. The workflow validates the head commit of
  that branch, builds it as a dev version, and publishes it to TestPyPI only.
  With the GitHub CLI: `gh workflow run release.yml --ref <branch>` (no `-f`
  needed: the default is `testpypi`).
- **Manual release to PyPI (on purpose only):** a manual run publishes to PyPI
  only if you select it in that run. Select the `v*` tag in "Use workflow
  from" and set "Package index" to `pypi`. The workflow validates the tagged
  commit, publishes it to PyPI only (not to TestPyPI), and creates the GitHub
  Release. With the GitHub CLI: `gh workflow run release.yml --ref v0.4.0 -f
  index=pypi`. If you select `pypi` on a ref that is not a `v*` tag, the
  `build` job fails and nothing is published. A version on PyPI cannot be
  withdrawn or uploaded again, so make this choice with care (for example, to
  retry a tag release whose tag push run did not publish).

If the validation fails, nothing is published. Fix the problem, then tag the
fixed commit.

Every action in `.github/workflows/` is pinned to a full commit SHA, with its
version in a trailing comment. Dependabot (`.github/dependabot.yml`) opens a
pull request when a pinned action has a new version. Pin a new action the same
way.

## Boundaries

Please preserve the basics: fresh refs by default, no trust in target-repo sandbox config, and stable CLI output for scripts.

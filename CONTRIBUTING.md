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
regenerate the golden snapshots with `scripts/golden-diff.sh --update`. Note
user-facing changes under `[Unreleased]` in `CHANGELOG.md`.

## Boundaries

Please preserve the basics: fresh refs by default, no trust in target-repo sandbox config, and stable CLI output for scripts.

# Factory shortcuts. treebox's own checks live in scripts/validate.sh (see CLAUDE.md).
FACTORY_UV := uv run --project .factory --extra dev

.PHONY: factory-test factory-check factory-plan factory-apply factory-smoke

factory-test: ## lint, strict types, and tests for the factory CLI
	cd .factory && uv run --extra dev ruff check src tests
	cd .factory && uv run --extra dev ruff format --check src tests
	cd .factory && uv run --extra dev mypy
	cd .factory && uv run --extra dev python -m pytest

factory-check: ## validate the factory definition (manifest, agents, prompt)
	$(FACTORY_UV) factory check

factory-plan: ## show what `ant apply` would change
	cd .factory && ant apply --dry-run .

factory-apply: ## apply agents, environment, and vault; then commit .factory/claude-lock.json
	cd .factory && ant apply .

factory-smoke: ## live end-to-end run: make factory-smoke ISSUE=38 [REF=branch]
	.factory/scripts/smoke.sh $(ISSUE) $(REF)

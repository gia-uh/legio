# legio developer commands (Makefile launcher over `uv run`).
#
# The canonical gate is `make ci`, which mirrors `.github/workflows/ci.yml`
# exactly: lint (ruff check) + format check (ruff format --check) + typecheck
# (pyright) + full pytest. Everything is English and domain-free (AGENTS.md
# rules 1 and 7). Formatting debt: the tree is not yet `ruff format`-clean;
# that triage is tracked in docs/JOURNALS (Session 108) and stays separate
# from per-issue work.

VERSION := 0.1.0

.DEFAULT_GOAL := help

.PHONY: help
help: ## List every command
	@printf '%s\n' 'legio commands:' && grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  %-14s %s\n", $$1, $$2}'

.PHONY: sync
sync: ## Install dependencies from the lockfile (uv sync)
	uv sync

.PHONY: lint
lint: ## Lint: ruff check
	uv run ruff check .

.PHONY: format
format: ## Format: apply ruff format to the tree
	uv run ruff format .

.PHONY: format-check
format-check: ## Format gate: ruff format --check (note: known debt, Session 108)
	uv run ruff format --check .

.PHONY: typecheck
typecheck: ## Typecheck: pyright
	uv run pyright

.PHONY: test
test: ## Full test suite: pytest
	uv run pytest

.PHONY: ci
ci: lint format-check typecheck test ## CI gate (exact parity with .github/workflows/ci.yml)

.PHONY: build
build: ## Build the wheel/archive (LEG-101): uv build
	uv build

.PHONY: clean
clean: ## Remove build/test artifacts (never the venv or user data)
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: tag
tag: ## Tag the current HEAD as v$(VERSION) (LEG-101; maintainer only, after approval)
	git tag "v$(VERSION)"

.PHONY: release
release: build tag ## Release: build + tag v$(VERSION) (maintainer only, after LEG-101 approval)

.PHONY: status
status: ## One-line weekly status from the git log
	git log --oneline -10
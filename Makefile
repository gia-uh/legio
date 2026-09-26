# legio developer commands (Makefile launcher over `uv run`).
#
# The canonical gate is `make ci`, which mirrors `.github/workflows/ci.yml`
# exactly: lint (ruff check) + format check (ruff format --check) + typecheck
# (pyright) + full pytest. Everything is English and domain-free (AGENTS.md
# rules 1 and 7). The release track is `make build` → `make validate-release`
# (LEG-101/LEG-102) → `make tag`.

VERSION := 0.1.1

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

.PHONY: release-guard
release-guard: ## Refuse to release a dirty tree or a stale validation record (LEG-107)
	@test -z "$$(git status --porcelain)" || { echo "release-guard: working tree is dirty (commit first)"; git status --short; exit 1; }
	@record="docs/VALIDATIONS/release-artifact-$(VERSION).md"; \
	 test -f "$$record" || { echo "release-guard: missing validation record $$record"; exit 1; }; \
	 stamp=$$(sed -n 's/^- Run at: //p' "$$record"); \
	 [ -n "$$stamp" ] || { echo "release-guard: no 'Run at' stamp in $$record"; exit 1; }; \
	 head_epoch=$$(git show -s --format=%ct HEAD); \
	 rec_epoch=$$(date -u -d "$$stamp" +%s 2>/dev/null || true); \
	 [ -n "$$rec_epoch" ] || { echo "release-guard: unparseable stamp '$$stamp'"; exit 1; }; \
	 [ "$$rec_epoch" -ge "$$head_epoch" ] || { echo "release-guard: validation record ($$stamp) predates HEAD; re-run make validate-release"; exit 1; }; \
	 echo "release-guard: clean tree, validation record fresh ($$stamp)"

.PHONY: validate-release
validate-release: ## Validate the release artifact (LEG-102): build, install into a throwaway venv, headless consumer smoke
	chmod +x scripts/validate_release.sh && scripts/validate_release.sh ${VERSION}

.PHONY: clean
clean: ## Remove build/test artifacts (never the venv or user data)
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

.PHONY: tag
tag: ## Tag the current HEAD as v$(VERSION) (LEG-101; maintainer only, after approval)
	git tag "v$(VERSION)"

.PHONY: release
release: release-guard build tag ## Release: guard + build + tag v$(VERSION) (maintainer only)

.PHONY: status
status: ## One-line weekly status from the git log
	git log --oneline -10
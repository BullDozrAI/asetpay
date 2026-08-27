.PHONY: help sync fixture gate0 test lint types purity imports check db rebuild clean
PY := PYTHONPATH=packages/contracts/src:packages/core/src:packages/snapshotter/src python3

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

sync:      ## Install all four environments
	uv sync --all-packages

fixture:   ## Generate the synthetic market with planted alpha
	$(PY) -m asetpay_core.synthetic.generator data/synthetic

gate0:     ## THE WEEK-1 EXIT CRITERION: harness recovers planted alpha
	$(PY) scripts/gate0.py

test:      ## Everything
	$(PY) -m pytest -q

lint:      ## ruff
	uv run ruff check . && uv run ruff format --check .

types:     ## mypy, strict on contracts + core
	uv run mypy

purity:    ## Features must be pure functions (P1-19)
	$(PY) scripts/check_feature_purity.py

imports:   ## Architectural boundaries (P1-13)
	uv run lint-imports

check: lint types purity imports test   ## Everything CI runs

db:        ## Apply migrations to the local Postgres
	psql "$${DATABASE_URL:-postgresql://localhost:5432/asetpay}" -f migrations/001_security_master.sql

rebuild:   ## P1-02b — regenerate Postgres from snapshots + migrations
	@echo "Drop and rebuild. Nothing in Postgres may be un-reconstructible."
	psql "$${DATABASE_URL:-postgresql://localhost:5432/asetpay}" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	$(MAKE) db

clean:
	rm -rf data/ .pytest_cache .mypy_cache .ruff_cache

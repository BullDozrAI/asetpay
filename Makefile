.PHONY: help sync fixture gate0 test testdb lint types purity imports check db rebuild clean universe
# Everything runs through `uv run`, not a bare python3.
#
# uv.lock exists so that every machine resolves the same versions. A target
# that shells out to whatever python is on PATH — a conda base env, the system
# python — silently opts out of that, and the failure mode is the worst kind:
# `make testdb` reported "1 skipped" on a machine without psycopg, which reads
# as a pass to anyone not looking closely. The lock is worthless if half the
# targets ignore it.
#
# PYTHONPATH is unnecessary here: pyproject's [tool.pytest.ini_options] sets
# pythonpath, and `uv sync --all-packages` installs the workspace packages.
PY := uv run python

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

sync:      ## Install all four environments
	uv sync --all-packages

fixture:   ## Generate the synthetic market with planted alpha
	$(PY) -m asetpay_core.synthetic.generator data/synthetic

gate0:     ## THE WEEK-1 EXIT CRITERION: harness recovers planted alpha
	$(PY) scripts/gate0.py

test:      ## Everything (the 10 schema tests SKIP without a database)
	uv run pytest -q

testdb:    ## The 10 P1-03 schema tests, against a real Postgres
	@echo "Skipped tests are not passed tests. This is the target that runs them."
	@env ASETPAY_TEST_DSN="$${ASETPAY_TEST_DSN:-postgresql://localhost:5432/asetpay_test}" \
	    uv run pytest tests/test_migrations.py -q; \
	  rc=$$?; \
	  if [ $$rc -eq 5 ]; then \
	    echo ""; \
	    echo "NOTHING RAN. pytest collected zero tests, which is not a pass."; \
	    echo "  - psycopg missing?     uv sync --all-packages"; \
	    echo "  - database missing?    createdb asetpay_test && make db"; \
	    echo "  - wrong DSN?           ASETPAY_TEST_DSN=... make testdb"; \
	    exit 1; \
	  fi; \
	  exit $$rc

lint:      ## ruff
	uv run ruff check . && uv run ruff format --check .

types:     ## mypy, strict on contracts + core
	uv run mypy

purity:    ## Features must be pure functions (P1-19)
	$(PY) scripts/check_feature_purity.py

imports:   ## Architectural boundaries (P1-13)
	uv run lint-imports

# testdb is included on purpose. CI sets ASETPAY_TEST_DSN and runs the schema
# tests; a local `check` that skips them can pass while CI fails, which makes
# the target a liar in the one direction that costs you a push.
check: lint types purity imports test testdb   ## Everything CI runs, including the DB tests

db:        ## Apply migrations to the local Postgres (refuses to re-apply)
	@DSN="$${DATABASE_URL:-postgresql://localhost:5432/asetpay}"; \
	 applied=$$(psql "$$DSN" -tAc \
	   "SELECT 1 FROM schema_migrations WHERE version='001_security_master'" 2>/dev/null); \
	 if [ "$$applied" = "1" ]; then \
	   echo "001_security_master is already applied. A change to an applied"; \
	   echo "migration is a NEW FILE, never an edit. Use 'make rebuild' to start over."; \
	 else \
	   psql -v ON_ERROR_STOP=1 "$$DSN" -f migrations/001_security_master.sql; \
	 fi

rebuild:   ## P1-02b — regenerate Postgres from snapshots + migrations
	@echo "Drop and rebuild. Nothing in Postgres may be un-reconstructible."
	psql -v ON_ERROR_STOP=1 "$${DATABASE_URL:-postgresql://localhost:5432/asetpay}" \
	  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	$(MAKE) db

universe:  ## Validate universe.txt against Alpaca's live asset list (needs keys)
	$(PY) scripts/build_universe.py --check

clean:
	rm -rf data/ .pytest_cache .mypy_cache .ruff_cache

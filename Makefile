# Mirrors CI. `make ci` is the full gate; `make matrix` runs the tests on every
# supported interpreter (installed on demand by uv).

UV          ?= uv
PYTESTFLAGS ?=
PYTHONS     ?= 3.11 3.12 3.13 3.14 3.14t 3.15

# pytest exits 5 when a marker selects nothing (e.g. before tests/e2e exists).
ALLOW_EMPTY = || [ $$? -eq 5 ]

.PHONY: help sync fmt fmt-check lint typecheck test test-e2e test-unit cov compat matrix stress ci build report report-check

help:
	@echo "targets: sync fmt fmt-check lint typecheck test test-e2e test-unit cov compat matrix stress ci build report report-check"

sync:
	$(UV) sync --group dev

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

fmt-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy --strict src

test:
	$(UV) run pytest $(PYTESTFLAGS)

test-e2e:
	$(UV) run pytest -m e2e $(PYTESTFLAGS) $(ALLOW_EMPTY)

test-unit:
	$(UV) run pytest -m unit $(PYTESTFLAGS) $(ALLOW_EMPTY)

# Coverage is enforced (fail_under = 80 in pyproject.toml) only here and in `ci`.
# Coverage runs in parallel mode (forked children and subprocesses write their
# own data files), so the data is combined before reporting.
cov:
	$(UV) run coverage erase
	$(UV) run coverage run -m pytest $(PYTESTFLAGS)
	$(UV) run coverage combine -q
	$(UV) run coverage report

compat:
	$(UV) run --group dev --group compat pytest -m compat $(PYTESTFLAGS) $(ALLOW_EMPTY)

# Each interpreter runs in a throwaway environment so the project .venv is untouched.
# Free-threaded builds (suffix "t") run with the GIL disabled.
matrix:
	@set -e; for py in $(PYTHONS); do \
		echo "==> Python $$py"; \
		case $$py in *t) gil=0 ;; *) gil= ;; esac; \
		env $${gil:+PYTHON_GIL=$$gil} $(UV) run --isolated --python $$py --group dev \
			pytest -q $(PYTESTFLAGS); \
	done

# The concurrency-sensitive tests, repeated (CI runs this on 3.14t with PYTHON_GIL=0).
STRESS_RUNS  ?= 10
STRESS_TESTS ?= tests/e2e/test_fork.py tests/e2e/test_context.py tests/unit/trace_otlp/test_batch.py tests/unit/server

stress:
	@set -e; for i in $$(seq 1 $(STRESS_RUNS)); do \
		echo "==> stress run $$i/$(STRESS_RUNS)"; \
		$(UV) run pytest -x -q -p no:cacheprovider $(STRESS_TESTS) $(PYTESTFLAGS); \
	done

ci: fmt-check lint typecheck cov

build:
	$(UV) build

# The PR test report (.github/workflows/test-report.yml), reproduced locally.
REPORT_DIR ?= build/test-report

report-check:
	$(UV) run mypy --strict scripts
	$(UV) run pytest scripts/tests -q -p no:cacheprovider

report:
	@mkdir -p $(REPORT_DIR); d=$(REPORT_DIR); \
	$(UV) run coverage erase --data-file=$$d/.coverage.unit; \
	$(UV) run coverage erase --data-file=$$d/.coverage.e2e; \
	$(UV) run coverage run --branch --data-file=$$d/.coverage.unit -m pytest tests/unit tests/structure tests/support \
		-q --junitxml=$$d/unit.xml > $$d/unit.log 2>&1; u=$$?; \
	$(UV) run coverage combine -q --data-file=$$d/.coverage.unit $$d/.coverage.unit.*; \
	$(UV) run coverage run --branch --data-file=$$d/.coverage.e2e -m pytest tests/e2e \
		-q --junitxml=$$d/e2e.xml > $$d/e2e.log 2>&1; e=$$?; \
	$(UV) run coverage combine -q --data-file=$$d/.coverage.e2e $$d/.coverage.e2e.*; \
	$(UV) run coverage json -q --fail-under=0 --data-file=$$d/.coverage.unit -o $$d/coverage-unit.json; \
	$(UV) run coverage json -q --fail-under=0 --data-file=$$d/.coverage.e2e -o $$d/coverage-e2e.json; \
	$(UV) run coverage combine -q --keep --data-file=$$d/.coverage $$d/.coverage.unit $$d/.coverage.e2e \
		&& $(UV) run coverage report --data-file=$$d/.coverage > $$d/coverage.txt 2>&1; c=$$?; \
	$(UV) run coverage json -q --fail-under=0 --data-file=$$d/.coverage -o $$d/coverage.json; \
	$(UV) run python scripts/test_report.py \
		--unit $$d/unit.xml --unit-cov $$d/coverage-unit.json --unit-exit $$u --unit-log $$d/unit.log \
		--e2e $$d/e2e.xml --e2e-cov $$d/coverage-e2e.json --e2e-exit $$e --e2e-log $$d/e2e.log \
		--total-cov $$d/coverage.json --coverage-exit $$c -o $$d/report.md; \
	echo "wrote $$d/report.md (unit=$$u e2e=$$e coverage=$$c)"; [ $$u -eq 0 ] && [ $$e -eq 0 ] && [ $$c -eq 0 ]

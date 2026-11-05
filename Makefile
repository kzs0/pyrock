# Mirrors CI. `make ci` is the full gate; `make matrix` runs the tests on every
# supported interpreter (installed on demand by uv).

UV          ?= uv
PYTESTFLAGS ?=
PYTHONS     ?= 3.11 3.12 3.13 3.14 3.14t 3.15

# pytest exits 5 when a marker selects nothing (e.g. before tests/e2e exists).
ALLOW_EMPTY = || [ $$? -eq 5 ]

.PHONY: help sync fmt fmt-check lint typecheck test test-e2e test-unit cov compat matrix ci build

help:
	@echo "targets: sync fmt fmt-check lint typecheck test test-e2e test-unit cov compat matrix ci build"

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
cov:
	$(UV) run coverage erase
	$(UV) run coverage run -m pytest $(PYTESTFLAGS)
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

ci: fmt-check lint typecheck cov

build:
	$(UV) build

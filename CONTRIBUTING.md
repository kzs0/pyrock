# Contributing to pyrock

Thanks for helping improve pyrock. Small, focused changes with tests and clear
compatibility notes are easiest to review.

## Before starting

Search the [issue tracker](https://github.com/kzs0/pyrock/issues) for related
work. Open an issue before a large feature, public API change or behavior
change, so the scope can be agreed before implementation. Security reports
must follow [SECURITY.md](SECURITY.md), not the public issue tracker.

You need [uv](https://docs.astral.sh/uv/). `uv sync` creates `.venv` with the
dev tools (pytest, coverage, ruff, mypy); uv installs any interpreter the
targets ask for. Clone your fork, create a topic branch, and keep unrelated
changes in separate pull requests.

## Rules for every change

- **Zero runtime dependencies.** Everything under `src/` imports only the
  standard library; `[project.dependencies]` stays empty. A structural test
  enforces this. Test-only packages belong in a dependency group (`compat`),
  and their tests must skip cleanly when the packages are missing. Do not add
  or update tool dependencies unless the change requires it and the pull
  request explains the tradeoff.
- **Python 3.11 baseline.** Code must stay valid on 3.11: use
  `from __future__ import annotations`, PEP 604 unions, `typing.Self` and
  `dataclass(slots=True)`, and no 3.12+ syntax (`type X = ...`, PEP 695
  generics). Newer stdlib APIs (for example `sys._is_gil_enabled`) are allowed
  only behind feature detection in `pyrock._compat`.
- **Free-threaded safety.** Never rely on the GIL. Shared mutable state is
  guarded by a `threading.Lock`; CI runs the suite on 3.14t with
  `PYTHON_GIL=0` and repeats the concurrency-sensitive tests (`make stress`).
- **Keep the design.** No process-wide globals (state lives in
  `contextvars`), no monkey-patching, controlled metric cardinality, and the
  dependency boundaries in `tests/structure/test_structure.py`. Heavy modules
  stay lazy, so `import pyrock` remains cheap.
- **Tests are the contract.** Test observable behavior (OTLP bytes, exposition
  text, log JSON, HTTP responses), not internals. Do not weaken or rewrite an
  existing test to make a change pass; if you believe a test is wrong, say so
  in the pull request and change it deliberately, with a changelog entry.
  Cover success, failure, boundary, cancellation and concurrency where
  relevant.
- **Deterministic tests.** Wait on events, barriers and test servers, not
  `time.sleep`. Bind every test server to `127.0.0.1:0`. Use the fakes in
  `tests/support/`.
- **Docs.** Update `README.md` (and `DESIGN.md` when the design changes) and
  the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) when behavior,
  configuration or API changes.

## Validate

```bash
make fmt                # ruff format + ruff check --fix
make ci                 # what CI's lint and coverage jobs run
```

`make ci` runs `ruff format --check`, `ruff check`, `mypy --strict src`, and
the full test suite under coverage with an 80% branch-coverage gate. Also run,
as relevant:

- `make matrix` for every supported interpreter (3.11 to 3.15 and 3.14t with
  the GIL disabled);
- `make stress` after touching threads, fork handling, the batch processor or
  the server;
- `make compat` after touching OTLP encoding or gRPC (official protos, a real
  grpcio collector);
- `make build` after packaging changes (the wheel must stay pure Python with no
  `Requires-Dist`).

CI runs the same commands on Linux for every interpreter, a 3.14t stress job,
3.14 on macOS and Windows, the compat suite and a wheel check.

## Compatibility

pyrock is below 1.0. Minor releases may contain intentional API or behavior
changes, but changes should still minimize disruption. A breaking change
needs:

- a clear reason and migration path;
- tests for the new contract;
- documentation updates; and
- an explicit changelog entry under `Changed`.

Wire compatibility with Go Bedrock (metric names, span attributes, log shape,
`BEDROCK_*` variables) is a feature. A deliberate difference must be listed in
the README's "Differences from Go Bedrock" section.

Bug fixes may reject input that was previously accepted incorrectly, or make
shutdown and network behavior stricter. Call these effects out so operators
can test their configuration before upgrading.

## Pull requests

Describe the problem, the chosen behavior, compatibility or security impact,
and the commands used to validate the change. Link related issues. Keep
commits reviewable; maintainers may squash or rebase when merging.

## Release process

Releases are maintainer-driven tags. For a release, a maintainer:

1. sets the version in `src/pyrock/_version.py` and moves the relevant
   `Unreleased` entries to a dated `X.Y.Z` section;
2. verifies `make ci`, `make matrix` and `make compat` on the release commit;
3. merges the release-ready change to `main`;
4. creates and pushes a `vX.Y.Z` tag (or a prerelease tag) on that commit;
5. builds the distribution from the tag (`make build`) and publishes it to
   PyPI (there is no automated release workflow yet); and
6. starts a fresh `Unreleased` section.

Release tags must point to `main`. Never move or reuse an existing tag;
publish a new patch version for corrections.

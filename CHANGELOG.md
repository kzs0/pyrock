# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break).

## Unreleased

### Added

- Project scaffold: packaging (`pyproject.toml`, hatchling, no runtime
  dependencies), the typed public API contract for every module in
  `DESIGN.md`, shared test fakes in `tests/support/`, structural tests
  (stdlib-only imports, dependency boundaries, lazy imports), `Makefile` and CI.

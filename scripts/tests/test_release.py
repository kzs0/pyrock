"""Tests for scripts/release.py, the tagged-release checks."""

from __future__ import annotations

from pathlib import Path

import pytest

import release

CHANGELOG = """\
# Changelog

## Unreleased

- Not yet released.

## [0.2.0rc1] - 2026-12-01

- A release candidate.

## [0.1.0] - 2026-11-08

### Added

- The first release.
"""


@pytest.mark.parametrize(
    ("version", "prerelease"),
    [("0.1.0", False), ("1.20.3", False), ("0.2.0a1", True), ("0.2.0b2", True), ("1.0.0rc1", True)],
)
def test_is_prerelease(version: str, prerelease: bool) -> None:
    assert release.is_prerelease(version) is prerelease


@pytest.mark.parametrize("version", ["0.1", "0.1.0.dev0", "0.1.0-rc.1", "0.1.0rc", "v0.1.0", ""])
def test_non_release_versions_are_rejected(version: str) -> None:
    with pytest.raises(release.ReleaseError, match="not a release version"):
        release.is_prerelease(version)


def test_changelog_section_returns_only_that_section() -> None:
    notes = release.changelog_section("0.1.0", CHANGELOG)
    assert notes == "### Added\n\n- The first release.\n"


def test_changelog_section_stops_at_the_next_heading() -> None:
    assert release.changelog_section("0.2.0rc1", CHANGELOG) == "- A release candidate.\n"


def test_changelog_section_requires_a_dated_heading() -> None:
    with pytest.raises(release.ReleaseError, match="no dated"):
        release.changelog_section("0.3.0", CHANGELOG)
    with pytest.raises(release.ReleaseError, match="no dated"):
        release.changelog_section("0.1.0", "## [0.1.0]\n\n- undated\n")


def test_changelog_section_rejects_an_empty_section() -> None:
    with pytest.raises(release.ReleaseError, match="empty"):
        release.changelog_section("0.1.0", "## [0.1.0] - 2026-11-08\n\n## [0.0.9] - 2026-01-01\n")


def test_check_accepts_a_matching_release_tag() -> None:
    assert release.check("v0.1.0", version="0.1.0", changelog=CHANGELOG) == {
        "version": "0.1.0",
        "prerelease": "false",
    }


def test_check_flags_prerelease_tags() -> None:
    outputs = release.check("v0.2.0rc1", version="0.2.0rc1", changelog=CHANGELOG)
    assert outputs["prerelease"] == "true"


@pytest.mark.parametrize(
    ("tag", "version", "message"),
    [
        ("0.1.0", "0.1.0", "must start with 'v'"),
        ("v0.1.1", "0.1.0", "does not match"),
        ("v0.1.0.dev0", "0.1.0.dev0", "not a release version"),
        ("v0.3.0", "0.3.0", "no dated"),
    ],
)
def test_check_rejects_bad_tags(tag: str, version: str, message: str) -> None:
    with pytest.raises(release.ReleaseError, match=message):
        release.check(tag, version=version, changelog=CHANGELOG)


def test_read_version_reads_the_literal_without_importing(tmp_path: Path) -> None:
    path = tmp_path / "_version.py"
    path.write_text('import sys\nsys.exit(1)\n__version__ = "1.2.3"\n', encoding="utf-8")
    assert release.read_version(path) == "1.2.3"


def test_read_version_requires_a_literal(tmp_path: Path) -> None:
    path = tmp_path / "_version.py"
    path.write_text("__version__ = compute()\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="no literal"):
        release.read_version(path)


def test_package_version_and_changelog_agree() -> None:
    """The committed version is either a dev version or a released, documented one."""
    version = release.read_version()
    if ".dev" in version:
        return
    release.check(f"v{version}", version=version, changelog=release.CHANGELOG.read_text())


def test_main_prints_workflow_outputs(capsys: pytest.CaptureFixture[str]) -> None:
    version = release.read_version()
    if ".dev" in version:
        pytest.skip("the committed version is a development version")
    assert release.main(["check", f"v{version}"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"version={version}"
    assert out[1] in {"prerelease=true", "prerelease=false"}


def test_main_reports_failures(capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["check", "v99.0.0"]) == 1
    assert "release check failed" in capsys.readouterr().err


def test_main_writes_notes(tmp_path: Path) -> None:
    version = release.read_version()
    if ".dev" in version:
        pytest.skip("the committed version is a development version")
    out = tmp_path / "notes.md"
    assert release.main(["notes", version, "--output", str(out)]) == 0
    assert out.read_text().strip()

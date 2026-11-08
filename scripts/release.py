"""Release helpers for the tagged-release workflow (standard library only).

``check`` validates a release tag against the package version and changelog
and prints ``key=value`` lines suitable for ``$GITHUB_OUTPUT``::

    python scripts/release.py check v0.1.0

``notes`` writes the changelog section for a version, used as the GitHub
Release body::

    python scripts/release.py notes 0.1.0 --output notes.md

Tags are ``v`` followed by the exact PEP 440 version in
``src/pyrock/_version.py``: ``X.Y.Z`` for a release, or ``X.Y.Z`` with an
``aN``, ``bN`` or ``rcN`` suffix for a prerelease.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "src" / "pyrock" / "_version.py"
CHANGELOG = ROOT / "CHANGELOG.md"

VERSION = re.compile(r"(?P<release>\d+\.\d+\.\d+)(?P<pre>(?:a|b|rc)\d+)?")
_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\] - (?P<date>\d{4}-\d{2}-\d{2})\s*$")
_ANY_HEADING = re.compile(r"^## ")


class ReleaseError(Exception):
    """A release check failed; the message says why."""


def read_version(path: Path = VERSION_FILE) -> str:
    """Return the literal ``__version__`` assigned in ``path`` (never imports it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise ReleaseError(f"{path}: no literal __version__ assignment")


def is_prerelease(version: str) -> bool:
    """Whether ``version`` is a valid release version with a prerelease suffix."""
    match = VERSION.fullmatch(version)
    if match is None:
        raise ReleaseError(
            f"version {version!r} is not a release version (X.Y.Z, optionally with aN, bN or rcN)"
        )
    return match.group("pre") is not None


def changelog_section(version: str, text: str) -> str:
    """Return the body of the dated ``## [version] - YYYY-MM-DD`` changelog section."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group("version") == version:
            body: list[str] = []
            for following in lines[index + 1 :]:
                if _ANY_HEADING.match(following):
                    break
                body.append(following)
            notes = "\n".join(body).strip()
            if not notes:
                raise ReleaseError(f"CHANGELOG section for {version} is empty")
            return notes + "\n"
    raise ReleaseError(f"CHANGELOG has no dated '## [{version}] - YYYY-MM-DD' section")


def check(tag: str, *, version: str, changelog: str) -> dict[str, str]:
    """Validate ``tag`` and return the workflow outputs (``version``, ``prerelease``)."""
    if not tag.startswith("v"):
        raise ReleaseError(f"tag {tag!r} must start with 'v'")
    if tag[1:] != version:
        raise ReleaseError(f"tag {tag!r} does not match the package version {version!r}")
    prerelease = is_prerelease(version)
    changelog_section(version, changelog)
    return {"version": version, "prerelease": "true" if prerelease else "false"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check_cmd = commands.add_parser("check", help="validate a release tag")
    check_cmd.add_argument("tag")
    notes_cmd = commands.add_parser("notes", help="write a version's changelog section")
    notes_cmd.add_argument("version")
    notes_cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        changelog = CHANGELOG.read_text(encoding="utf-8")
        if args.command == "check":
            outputs = check(args.tag, version=read_version(), changelog=changelog)
            for key, value in outputs.items():
                sys.stdout.write(f"{key}={value}\n")
        else:
            args.output.write_text(changelog_section(args.version, changelog), encoding="utf-8")
    except ReleaseError as exc:
        sys.stderr.write(f"release check failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

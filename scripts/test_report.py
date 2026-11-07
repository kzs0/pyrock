#!/usr/bin/env python3
"""Render the pull-request test report from JUnit XML and coverage.py JSON.

Stdlib only. Every input is optional: a missing or unreadable file renders as
"no results" instead of crashing, so the report is still posted when a suite
dies before writing its output. See ``.github/workflows/test-report.yml``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MARKER = "<!-- pyrock-test-report -->"
FAILURE_DETAIL_CHARS = 3000
DIAGNOSTIC_CHARS = 12000
MAX_FAILURE_SECTIONS = 25
MAX_COVERAGE_ROWS = 150
# GitHub rejects comment bodies over 65536 characters.
MAX_BODY_CHARS = 60000
DEFAULT_FAIL_UNDER = 80.0
DEFAULT_PARTIAL_BELOW = 90.0


# ---------------------------------------------------------------- JUnit


@dataclass(frozen=True)
class Case:
    """One ``<testcase>``: where it lives, how it ended, and why it failed."""

    group: str
    name: str
    outcome: str  # "pass" | "fail" | "error" | "skip"
    seconds: float
    detail: str = ""


@dataclass
class Group:
    """Per-area (unit) or per-file (e2e) totals."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped


@dataclass
class Suite:
    """A parsed suite. ``found`` is False when the JUnit file was missing or invalid."""

    title: str
    found: bool = False
    cases: list[Case] = field(default_factory=list)
    problem: str = ""

    def count(self, *outcomes: str) -> int:
        return sum(1 for case in self.cases if case.outcome in outcomes)

    @property
    def passed(self) -> int:
        return self.count("pass")

    @property
    def failed(self) -> int:
        return self.count("fail", "error")

    @property
    def skipped(self) -> int:
        return self.count("skip")

    @property
    def failures(self) -> list[Case]:
        return [case for case in self.cases if case.outcome in ("fail", "error")]

    def groups(self) -> dict[str, Group]:
        out: dict[str, Group] = {}
        for case in self.cases:
            group = out.setdefault(case.group, Group())
            group.seconds += case.seconds
            if case.outcome == "pass":
                group.passed += 1
            elif case.outcome == "skip":
                group.skipped += 1
            else:
                group.failed += 1
        return dict(sorted(out.items()))


def group_for(classname: str, name: str) -> str:
    """Report bucket for a test: ``tests/unit/<area>``, ``tests/<dir>`` or the e2e file.

    Collection errors have an empty classname and the module path as the name.
    """
    parts = (classname or name).split(".")
    if parts and parts[0] == "tests":
        parts = parts[1:]
    if not parts or not parts[0]:
        return "(unknown)"
    if parts[0] == "unit" and len(parts) > 1:
        return f"unit/{parts[1]}"
    if parts[0] == "e2e" and len(parts) > 1:
        return f"{parts[1]}.py"
    return parts[0]


def _seconds(raw: str | None) -> float:
    try:
        return max(float(raw or 0), 0.0)
    except ValueError:
        return 0.0


def _detail(element: ET.Element) -> str:
    """The traceback when pytest recorded one (it repeats the message), else the message."""
    return (element.text or "").strip() or element.get("message", "").strip()


def parse_case(element: ET.Element) -> Case:
    classname = element.get("classname", "")
    name = element.get("name", "")
    group = group_for(classname, name)
    seconds = _seconds(element.get("time"))
    # An error (setup/teardown/collection) outranks a failure, which outranks a skip.
    for tag, outcome in (("error", "error"), ("failure", "fail"), ("skipped", "skip")):
        found = element.find(tag)
        if found is not None:
            detail = _detail(found) if outcome != "skip" else ""
            return Case(group, name, outcome, seconds, detail)
    return Case(group, name, "pass", seconds)


def parse_junit(path: Path | None, title: str) -> Suite:
    """Parse a pytest JUnit file (one ``<testsuite>`` or a ``<testsuites>`` of many)."""
    suite = Suite(title)
    if path is None:
        suite.problem = "no JUnit file was given"
        return suite
    try:
        # Trusted input: produced by pytest in the same job.
        root = ET.parse(path).getroot()  # noqa: S314
    except FileNotFoundError:
        suite.problem = f"`{path}` was not produced"
        return suite
    except (ET.ParseError, OSError) as exc:
        suite.problem = f"`{path}` could not be parsed: {exc}"
        return suite
    suite.found = True
    suite.cases = [parse_case(case) for case in root.iter("testcase")]
    return suite


# ---------------------------------------------------------------- coverage


@dataclass(frozen=True)
class FileCoverage:
    path: str
    percent: float
    statements: int
    missing_lines: int
    branches: int
    missing_branches: int
    partial_branches: int


@dataclass
class Coverage:
    found: bool = False
    percent: float | None = None
    files: list[FileCoverage] = field(default_factory=list)


def _int(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    return value if isinstance(value, int) else 0


def parse_coverage(path: Path | None) -> Coverage:
    """Read ``coverage json`` output; anything unexpected yields ``found=False``."""
    if path is None:
        return Coverage()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        totals = data["totals"]["percent_covered"]
        files: dict[str, Any] = data.get("files", {})
    except (OSError, ValueError, KeyError, TypeError):
        return Coverage()
    if not isinstance(totals, int | float) or not isinstance(files, dict):
        return Coverage()
    out = Coverage(found=True, percent=float(totals))
    for name, entry in sorted(files.items()):
        summary = entry.get("summary", {}) if isinstance(entry, dict) else {}
        percent = summary.get("percent_covered")
        if not isinstance(percent, int | float):
            continue
        out.files.append(
            FileCoverage(
                path=name.removeprefix("src/"),
                percent=float(percent),
                statements=_int(summary, "num_statements"),
                missing_lines=_int(summary, "missing_lines"),
                branches=_int(summary, "num_branches"),
                missing_branches=_int(summary, "missing_branches"),
                partial_branches=_int(summary, "num_partial_branches"),
            )
        )
    return out


def read_fail_under(pyproject: Path) -> float:
    """``[tool.coverage.report] fail_under`` from pyproject.toml, else the default."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        value = data["tool"]["coverage"]["report"]["fail_under"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return DEFAULT_FAIL_UNDER
    return float(value) if isinstance(value, int | float) else DEFAULT_FAIL_UNDER


# ---------------------------------------------------------------- markdown helpers


def cell(text: str) -> str:
    """Make text safe inside a markdown table cell."""
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def code(text: str) -> str:
    """Inline code span that cannot be broken by backticks in the text."""
    return "`" + cell(text).replace("`", "'") + "`"


def fenced(text: str, limit: int, *, tail: bool = False) -> list[str]:
    """A fenced block with backticks neutralised and length capped."""
    body = text.strip().replace("`", "'")
    if len(body) > limit:
        body = ("…" + body[-limit:]) if tail else (body[:limit] + "…")
    return ["```text", body, "```"]


def percent(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1f}%"


def duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.2f} s"


def details(summary: str, body: Iterable[str], *, open_: bool = False) -> list[str]:
    return [
        "<details open>" if open_ else "<details>",
        f"<summary>{summary}</summary>",
        "",
        *body,
        "",
        "</details>",
    ]


# ---------------------------------------------------------------- sections


def suite_ok(suite: Suite, exit_code: int | None) -> bool:
    """Passed only with a zero exit, a parsed JUnit file, at least one test and no failures."""
    exit_ok = exit_code is None or exit_code == 0
    return exit_ok and suite.found and bool(suite.cases) and suite.failed == 0


def result_text(suite: Suite, exit_code: int | None) -> str:
    if not suite.found:
        return "No results" + (f" (exit {exit_code})" if exit_code not in (None, 0) else "")
    parts = [f"{suite.passed} passed"]
    if suite.failed:
        parts.append(f"{suite.failed} failed")
    if suite.skipped:
        parts.append(f"{suite.skipped} skipped")
    text = " · ".join(parts)
    if exit_code not in (None, 0) and not suite.failed:
        text += f" · exit {exit_code}"
    return ("✅ " if suite_ok(suite, exit_code) else "❌ ") + text


def group_table(suites: Sequence[Suite]) -> list[str]:
    rows = [
        "| Suite | Area | | Tests | ✅ Pass | ❌ Fail | ⏭ Skip | Duration |",
        "|---|---|:-:|---:|---:|---:|---:|---:|",
    ]
    for suite in suites:
        groups = suite.groups()
        if not groups:
            rows.append(f"| {suite.title} | _no results_ | ⚠️ | 0 | 0 | 0 | 0 | — |")
        for name, group in groups.items():
            icon = "❌" if group.failed else ("⏭" if group.passed == 0 else "✅")
            rows.append(
                f"| {suite.title} | {code(name)} | {icon} | {group.total} | {group.passed} "
                f"| {group.failed} | {group.skipped} | {duration(group.seconds)} |"
            )
    return rows


def failure_sections(suites: Sequence[Suite]) -> list[str]:
    failures = [(suite, case) for suite in suites for case in suite.failures]
    if not failures:
        return []
    out = ["", "### Failures", ""]
    for suite, case in failures[:MAX_FAILURE_SECTIONS]:
        label = f"{suite.title} · {case.group} · {case.name}"
        if case.outcome == "error":
            label += " (error)"
        summary = "<code>" + label.replace("<", "&lt;").replace(">", "&gt;") + "</code>"
        detail = case.detail or "No failure message was recorded."
        out += details(summary, fenced(detail, FAILURE_DETAIL_CHARS))
    hidden = len(failures) - MAX_FAILURE_SECTIONS
    if hidden > 0:
        out += ["", f"_…and {hidden} more failures; see the uploaded JUnit artifacts._"]
    return out


def diagnostic_sections(entries: Sequence[tuple[Suite, int | None, Path | None]]) -> list[str]:
    """Raw logs for suites that exited non-zero or produced no JUnit results."""
    out: list[str] = []
    for suite, exit_code, log in entries:
        if suite_ok(suite, exit_code) or (suite.found and suite.failures):
            continue  # passed, or its failures are already itemised above
        text = ""
        if log is not None:
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        reasons = [suite.problem] if suite.problem else []
        if exit_code not in (None, 0):
            reasons.append(f"exit code {exit_code}")
        header = f"{suite.title}: " + ("; ".join(reasons) or "no tests ran")
        body = [
            header,
            "",
            *fenced(text or "No diagnostics were produced.", DIAGNOSTIC_CHARS, tail=True),
        ]
        out += ["", *details(f"Raw {suite.title.lower()} diagnostics", body)]
    return out


def coverage_sections(coverage: Coverage, scope: str, partial_below: float) -> list[str]:
    zero = [f for f in coverage.files if f.statements and f.percent == 0]
    partial = sorted(
        (f for f in coverage.files if 0 < f.percent < partial_below),
        key=lambda f: (f.percent, f.path),
    )
    out: list[str] = []
    if zero:
        rows = [f"| {code(f.path)} | {f.statements} | {f.branches} |" for f in zero]
        out += [
            "",
            *details(
                f"{len(zero)} modules not covered by {scope}",
                [
                    "| Module | Statements | Branches |",
                    "|---|---:|---:|",
                    *_capped(rows),
                ],
            ),
        ]
    if partial:
        rows = [
            f"| {code(f.path)} | {f.percent:.1f}% | {f.missing_lines} / {f.statements} "
            f"| {f.missing_branches} / {f.branches} | {f.partial_branches} |"
            for f in partial
        ]
        out += [
            "",
            *details(
                f"{len(partial)} modules below {partial_below:g}% coverage ({scope})",
                [
                    "| Module | Coverage | Missing lines | Missing branches | Partial branches |",
                    "|---|---:|---:|---:|---:|",
                    *_capped(rows),
                ],
            ),
        ]
    return out


def _capped(rows: list[str]) -> list[str]:
    if len(rows) <= MAX_COVERAGE_ROWS:
        return rows
    return [*rows[:MAX_COVERAGE_ROWS], f"| _…{len(rows) - MAX_COVERAGE_ROWS} more_ | | | | |"]


# ---------------------------------------------------------------- report


@dataclass
class Inputs:
    unit: Suite
    e2e: Suite
    unit_cov: Coverage
    e2e_cov: Coverage
    total_cov: Coverage
    unit_exit: int | None = None
    e2e_exit: int | None = None
    coverage_exit: int | None = None
    unit_log: Path | None = None
    e2e_log: Path | None = None
    fail_under: float = DEFAULT_FAIL_UNDER
    partial_below: float = DEFAULT_PARTIAL_BELOW
    run_url: str = ""
    checks_url: str = ""
    sha: str = ""
    python: str = ""


SCOPE_NOTE = (
    "<sub>Unit: tests/unit, tests/structure, tests/support. E2E: tests/e2e. Branch coverage "
    "of src/pyrock; the aggregate combines both suites and is the CI gate.</sub>"
)


def suite_row(suite: Suite, exit_code: int | None, coverage: Coverage) -> str:
    return f"| {suite.title} | {result_text(suite, exit_code)} | {percent(coverage.percent)} |"


def gate_ok(inputs: Inputs) -> bool:
    if inputs.coverage_exit is not None:
        return inputs.coverage_exit == 0
    pct = inputs.total_cov.percent
    return pct is None or pct >= inputs.fail_under


def all_passed(inputs: Inputs) -> bool:
    return (
        suite_ok(inputs.unit, inputs.unit_exit)
        and suite_ok(inputs.e2e, inputs.e2e_exit)
        and gate_ok(inputs)
    )


def render(inputs: Inputs) -> str:
    passed = all_passed(inputs)
    suites = (inputs.unit, inputs.e2e)
    lines = [
        MARKER,
        "### All tests passed" if passed else "### Tests need attention",
        "",
        "| Suite | Result | Coverage |",
        "|---|---|---:|",
        suite_row(inputs.unit, inputs.unit_exit, inputs.unit_cov),
        suite_row(inputs.e2e, inputs.e2e_exit, inputs.e2e_cov),
    ]
    if inputs.total_cov.found or inputs.coverage_exit is not None:
        icon = "✅" if gate_ok(inputs) else "❌"
        lines.append(
            f"| Aggregate | {icon} gate: ≥ {inputs.fail_under:g}% branch coverage "
            f"| {percent(inputs.total_cov.percent)} |"
        )
    lines += [
        "",
        SCOPE_NOTE,
        "",
    ]
    links = []
    if inputs.checks_url:
        links.append(
            f"[CI matrix (Python 3.11 to 3.15, 3.14t, macOS, Windows)]({inputs.checks_url})"
        )
    if inputs.run_url:
        links.append(f"[Logs, JUnit & coverage artifacts]({inputs.run_url})")
    if inputs.python:
        links.append(f"Report run: Python {inputs.python}")
    if links:
        lines += [" · ".join(links), ""]

    any_suite_failed = not (
        suite_ok(inputs.unit, inputs.unit_exit) and suite_ok(inputs.e2e, inputs.e2e_exit)
    )
    lines += [
        "<details open>" if any_suite_failed else "<details>",
        "<summary>Test details by area</summary>",
        "",
        *group_table(suites),
        *failure_sections(suites),
        *diagnostic_sections(
            [
                (inputs.unit, inputs.unit_exit, inputs.unit_log),
                (inputs.e2e, inputs.e2e_exit, inputs.e2e_log),
            ]
        ),
    ]
    cov, scope = (
        (inputs.total_cov, "either suite")
        if inputs.total_cov.found
        else (inputs.unit_cov, "unit tests")
    )
    lines += coverage_sections(cov, scope, inputs.partial_below)
    lines += ["", "</details>"]
    if inputs.sha:
        lines += ["", f"<sub>Updated for {inputs.sha[:7]}</sub>"]
    return _fit("\n".join(lines) + "\n")


def _fit(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    note = "\n\n_Report truncated to fit a GitHub comment; see the run artifacts._\n</details>\n"
    return body[: MAX_BODY_CHARS - len(note)] + note


# ---------------------------------------------------------------- CLI


def _path(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


def _exit(raw: str | None) -> int | None:
    """Exit codes from step outputs; an empty or garbled value counts as failure (1)."""
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add = parser.add_argument
    add("--unit", help="JUnit XML from the unit suite")
    add("--unit-cov", help="coverage json from the unit suite")
    add("--unit-exit", help="pytest exit code of the unit suite")
    add("--unit-log", help="console log of the unit suite (shown when it fails)")
    add("--e2e", help="JUnit XML from the e2e suite")
    add("--e2e-cov", help="coverage json from the e2e suite")
    add("--e2e-exit", help="pytest exit code of the e2e suite")
    add("--e2e-log", help="console log of the e2e suite (shown when it fails)")
    add("--total-cov", help="combined coverage json (the aggregate gate)")
    add("--coverage-exit", help="exit code of `coverage report` (fail_under gate)")
    add("--fail-under", type=float, help="gate threshold; default: pyproject.toml")
    add("--partial-below", type=float, default=DEFAULT_PARTIAL_BELOW)
    add("--pyproject", default="pyproject.toml")
    add("--run-url", default="")
    add("--checks-url", default="")
    add("--sha", default="")
    add("--python", default="")
    add("-o", "--output", help="write here instead of stdout")
    return parser


def inputs_from_args(args: argparse.Namespace) -> Inputs:
    fail_under = args.fail_under
    if fail_under is None:
        fail_under = read_fail_under(Path(args.pyproject))
    return Inputs(
        unit=parse_junit(_path(args.unit), "Unit"),
        e2e=parse_junit(_path(args.e2e), "E2E"),
        unit_cov=parse_coverage(_path(args.unit_cov)),
        e2e_cov=parse_coverage(_path(args.e2e_cov)),
        total_cov=parse_coverage(_path(args.total_cov)),
        unit_exit=_exit(args.unit_exit),
        e2e_exit=_exit(args.e2e_exit),
        coverage_exit=_exit(args.coverage_exit),
        unit_log=_path(args.unit_log),
        e2e_log=_path(args.e2e_log),
        fail_under=fail_under,
        partial_below=args.partial_below,
        run_url=args.run_url,
        checks_url=args.checks_url,
        sha=args.sha,
        python=args.python,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = render(inputs_from_args(args))
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

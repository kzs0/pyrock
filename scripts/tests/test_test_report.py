"""Contract tests for the PR test-report renderer (``scripts/test_report.py``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_report as tr

UNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5" time="1.5">
    <testcase classname="tests.unit.attr.test_values" name="test_ok" time="0.25" />
    <testcase classname="tests.unit.attr.test_values" name="test_bad" time="0.5">
      <failure message="AssertionError: assert 1 == 2">def test_bad():
&gt;       assert `1` == 2
E       AssertionError: assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.unit.metric.test_registry" name="test_skip[a|b]" time="0">
      <skipped type="pytest.skip" message="needs grpc">skip reason</skipped>
    </testcase>
    <testcase classname="tests.structure.test_structure" name="test_parses" time="0.1" />
  </testsuite>
  <testsuite name="second" tests="1">
    <testcase classname="" name="tests.support.test_support" time="0">
      <error message="collection failure">ImportError: boom</error>
    </testcase>
  </testsuite>
</testsuites>
"""

E2E_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="pytest" tests="2">
  <testcase classname="tests.e2e.test_asgi.TestContract" name="test_span" time="0.75" />
  <testcase classname="tests.e2e.test_wsgi" name="test_route" time="2.5" />
</testsuite>
"""


def coverage_json(total: float, files: dict[str, dict[str, float]]) -> str:
    return json.dumps(
        {
            "meta": {"format": 3},
            "totals": {"percent_covered": total},
            "files": {
                name: {
                    "summary": {
                        "percent_covered": f["pct"],
                        "num_statements": int(f.get("stmts", 10)),
                        "missing_lines": int(f.get("miss", 0)),
                        "num_branches": int(f.get("br", 4)),
                        "missing_branches": int(f.get("mbr", 0)),
                        "num_partial_branches": int(f.get("pbr", 0)),
                    }
                }
                for name, f in files.items()
            },
        }
    )


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "unit": tmp_path / "unit.xml",
        "e2e": tmp_path / "e2e.xml",
        "unit_cov": tmp_path / "coverage-unit.json",
        "e2e_cov": tmp_path / "coverage-e2e.json",
        "total_cov": tmp_path / "coverage.json",
    }
    paths["unit"].write_text(UNIT_XML, encoding="utf-8")
    paths["e2e"].write_text(E2E_XML, encoding="utf-8")
    paths["unit_cov"].write_text(
        coverage_json(70.0, {"src/pyrock/a.py": {"pct": 100}}), encoding="utf-8"
    )
    paths["e2e_cov"].write_text(
        coverage_json(60.0, {"src/pyrock/a.py": {"pct": 50}}), encoding="utf-8"
    )
    paths["total_cov"].write_text(
        coverage_json(
            85.0,
            {
                "src/pyrock/a.py": {"pct": 100},
                "src/pyrock/dead.py": {"pct": 0, "stmts": 12, "br": 2},
                "src/pyrock/empty.py": {"pct": 0, "stmts": 0, "br": 0},
                "src/pyrock/half.py": {"pct": 50, "miss": 5, "mbr": 2, "pbr": 1},
                "src/pyrock/most.py": {"pct": 89.5},
            },
        ),
        encoding="utf-8",
    )
    return paths


def render(files: dict[str, Path], *extra: str) -> str:
    args = [f"--{key.replace('_', '-')}={path}" for key, path in files.items()]
    return tr.render(tr.inputs_from_args(tr.build_parser().parse_args([*args, *extra])))


# ---------------------------------------------------------------- parsing


def test_junit_outcomes_across_multiple_testsuites(files: dict[str, Path]) -> None:
    suite = tr.parse_junit(files["unit"], "Unit")
    assert suite.found
    assert (suite.passed, suite.failed, suite.skipped) == (2, 2, 1)
    assert [c.outcome for c in suite.failures] == ["fail", "error"]
    assert "AssertionError: assert 1 == 2" in suite.failures[0].detail
    assert "ImportError: boom" in suite.failures[1].detail


def test_failure_detail_falls_back_to_message(tmp_path: Path) -> None:
    junit = tmp_path / "msg.xml"
    junit.write_text(
        '<testsuite><testcase classname="tests.e2e.test_x" name="t">'
        '<failure message="only a message"/></testcase></testsuite>',
        encoding="utf-8",
    )
    assert tr.parse_junit(junit, "E2E").failures[0].detail == "only a message"


def test_groups_unit_by_area_and_e2e_by_file(files: dict[str, Path]) -> None:
    unit = tr.parse_junit(files["unit"], "Unit").groups()
    assert list(unit) == ["structure", "support", "unit/attr", "unit/metric"]
    assert (unit["unit/attr"].passed, unit["unit/attr"].failed) == (1, 1)
    assert unit["unit/attr"].seconds == pytest.approx(0.75)
    e2e = tr.parse_junit(files["e2e"], "E2E").groups()
    assert list(e2e) == ["test_asgi.py", "test_wsgi.py"]


@pytest.mark.parametrize(
    ("classname", "name", "group"),
    [
        ("tests.unit.trace_otlp.test_x.TestY", "t", "unit/trace_otlp"),
        ("tests.e2e.test_fork", "t", "test_fork.py"),
        ("", "tests.e2e.test_fork", "test_fork.py"),
        ("tests.structure.test_structure", "t", "structure"),
        ("", "", "(unknown)"),
    ],
)
def test_group_for(classname: str, name: str, group: str) -> None:
    assert tr.group_for(classname, name) == group


def test_missing_and_invalid_junit_render_no_results(tmp_path: Path) -> None:
    missing = tr.parse_junit(tmp_path / "nope.xml", "Unit")
    assert not missing.found
    assert "was not produced" in missing.problem
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite", encoding="utf-8")
    assert not tr.parse_junit(bad, "Unit").found
    assert not tr.parse_junit(None, "Unit").found


def test_coverage_parsing_is_robust(tmp_path: Path, files: dict[str, Path]) -> None:
    cov = tr.parse_coverage(files["total_cov"])
    assert cov.percent == 85.0
    assert [f.path for f in cov.files][:2] == ["pyrock/a.py", "pyrock/dead.py"]
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    assert not tr.parse_coverage(garbage).found
    garbage.write_text('{"totals": {}}', encoding="utf-8")
    assert not tr.parse_coverage(garbage).found
    assert not tr.parse_coverage(tmp_path / "absent.json").found


def test_fail_under_is_read_from_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[tool.coverage.report]\nfail_under = 72.5\n", encoding="utf-8")
    assert tr.read_fail_under(pyproject) == 72.5
    pyproject.write_text("[tool.other]\n", encoding="utf-8")
    assert tr.read_fail_under(pyproject) == tr.DEFAULT_FAIL_UNDER
    assert tr.read_fail_under(tmp_path / "missing.toml") == tr.DEFAULT_FAIL_UNDER


# ---------------------------------------------------------------- rendering


def test_failing_report(files: dict[str, Path]) -> None:
    body = render(files, "--unit-exit=1", "--e2e-exit=0", "--coverage-exit=0", "--sha=abcdef0123")
    assert body.startswith(tr.MARKER + "\n### Tests need attention\n")
    assert "| Unit | ❌ 2 passed · 2 failed · 1 skipped | 70.0% |" in body
    assert "| E2E | ✅ 2 passed | 60.0% |" in body
    assert "| Aggregate | ✅ gate: ≥ 80% branch coverage | 85.0% |" in body
    assert "<details open>\n<summary>Test details by area</summary>" in body
    assert "### Failures" in body
    assert "Unit · unit/attr · test_bad" in body
    assert "(error)" in body
    # backticks in failure output cannot close the fence early
    assert "assert '1' == 2" in body
    assert "`unit/metric` | ⏭ | 1 | 0 | 0 | 1 |" in body
    assert body.rstrip().endswith("<sub>Updated for abcdef0</sub>")


def test_passing_report_collapses_details(files: dict[str, Path]) -> None:
    unit = files["unit"]
    unit.write_text(
        '<testsuite><testcase classname="tests.unit.attr.t" name="a" time="0.002"/></testsuite>',
        encoding="utf-8",
    )
    body = render(
        files,
        "--unit-exit=0",
        "--e2e-exit=0",
        "--coverage-exit=0",
        "--run-url=https://example.test/run/1",
        "--checks-url=https://example.test/pull/2/checks",
        "--python=3.14.6",
    )
    assert "### All tests passed" in body
    assert "<details>\n<summary>Test details by area</summary>" in body
    assert "### Failures" not in body
    assert "diagnostics" not in body
    assert "(https://example.test/pull/2/checks)" in body
    assert "(https://example.test/run/1)" in body
    assert "Report run: Python 3.14.6" in body
    assert "| `unit/attr` | ✅ | 1 | 1 | 0 | 0 | 2 ms |" in body


def test_coverage_lists(files: dict[str, Path]) -> None:
    body = render(files, "--unit-exit=0", "--e2e-exit=0", "--coverage-exit=0")
    assert "<summary>1 modules not covered by either suite</summary>" in body
    assert "| `pyrock/dead.py` | 12 | 2 |" in body
    assert "empty.py" not in body  # nothing to cover
    assert "<summary>2 modules below 90% coverage (either suite)</summary>" in body
    half = body.index("pyrock/half.py")
    assert half < body.index("pyrock/most.py")  # lowest coverage first
    assert "| `pyrock/half.py` | 50.0% | 5 / 10 | 2 / 4 | 1 |" in body


def test_coverage_gate_failure_alone_needs_attention(files: dict[str, Path]) -> None:
    body = render(files, "--unit-exit=0", "--e2e-exit=0", "--coverage-exit=2")
    assert "### Tests need attention" in body
    assert "| Aggregate | ❌ gate" in body


def test_gate_falls_back_to_threshold_without_exit_code(files: dict[str, Path]) -> None:
    del files["unit"], files["e2e"]
    inputs = tr.inputs_from_args(
        tr.build_parser().parse_args(
            [f"--total-cov={files['total_cov']}", "--fail-under=90"],
        )
    )
    assert not tr.gate_ok(inputs)


def test_suite_without_junit_shows_raw_diagnostics(tmp_path: Path, files: dict[str, Path]) -> None:
    files["e2e"] = tmp_path / "missing.xml"
    log = tmp_path / "e2e.log"
    log.write_text("ERROR: `conftest` exploded\n" + "x" * 20000, encoding="utf-8")
    body = render(files, "--unit-exit=1", "--e2e-exit=4", f"--e2e-log={log}")
    assert "| E2E | No results (exit 4) | 60.0% |" in body
    assert "<summary>Raw e2e diagnostics</summary>" in body
    assert "E2E: `" in body
    assert "was not produced; exit code 4" in body
    # unit failures are itemised, so no raw unit log is repeated
    assert "Raw unit diagnostics" not in body
    # the tail of a long log is kept
    assert "exploded" not in body
    assert "x" * 100 in body


def test_nonzero_exit_with_clean_junit_is_a_failure(files: dict[str, Path]) -> None:
    body = render(files, "--unit-exit=0", "--e2e-exit=1", "--coverage-exit=0")
    assert "| E2E | ❌ 2 passed · exit 1 | 60.0% |" in body
    assert "<summary>Raw e2e diagnostics</summary>" in body


def test_empty_suite_is_not_a_pass(tmp_path: Path) -> None:
    empty = tmp_path / "empty.xml"
    empty.write_text('<testsuite tests="0"/>', encoding="utf-8")
    suite = tr.parse_junit(empty, "E2E")
    assert not tr.suite_ok(suite, 0)
    assert "_no results_" in "\n".join(tr.group_table([suite]))


def test_everything_missing_still_renders(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    assert tr.main(["--unit-exit=", "--e2e-exit=", "--sha=0123456789", f"-o={out}"]) == 0
    body = out.read_text(encoding="utf-8")
    assert body.startswith(tr.MARKER)
    assert "### Tests need attention" in body
    assert "| Unit | No results (exit 1) | Unavailable |" in body


def test_body_is_capped_for_github(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tr, "MAX_BODY_CHARS", 500)
    body = render(files, "--unit-exit=1")
    assert len(body) <= 500
    assert body.startswith(tr.MARKER)
    assert "Report truncated" in body


def test_cell_and_code_escaping() -> None:
    assert tr.cell("a|b\nc") == "a\\|b c"
    assert tr.code("x`y") == "`x'y`"
    assert tr.duration(0) == "—"
    assert tr.duration(2.0) == "2.00 s"

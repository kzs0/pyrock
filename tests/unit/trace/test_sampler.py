"""Samplers (DESIGN §7.2 and contract decision 26).

``Ratio`` is deterministic on the trace ID's low 8 bytes; ``ParentBased``
follows the parent and drops children of unsampled parents.
"""

from __future__ import annotations

import math
import random

import pytest

from pyrock.trace import sampler
from pyrock.trace.sampler import (
    AlwaysOff,
    AlwaysOn,
    Decision,
    ParentBased,
    Ratio,
    Sampler,
    SamplingResult,
)

PARENTS = [None, True, False]
LOW_ID = bytes.fromhex("0123456789abcdef" + "0000000000000001")
HIGH_ID = bytes.fromhex("0123456789abcdef" + "ffffffffffffffff")
ZERO_LOW_ID = bytes.fromhex("ffffffffffffffff" + "0000000000000000")


def _ids(n: int, seed: int = 1234) -> list[bytes]:
    rng = random.Random(seed)
    return [rng.randbytes(16) for _ in range(n)]


# --------------------------------------------------------- SamplingResult


def test_sampling_result_flags() -> None:
    assert SamplingResult(Decision.RECORD_AND_SAMPLE).sampled is True
    assert SamplingResult(Decision.RECORD_AND_SAMPLE).recording is True
    assert SamplingResult(Decision.RECORD_ONLY).sampled is False
    assert SamplingResult(Decision.RECORD_ONLY).recording is True
    assert SamplingResult(Decision.DROP).sampled is False
    assert SamplingResult(Decision.DROP).recording is False
    assert dict(SamplingResult(Decision.DROP).attrs) == {}


def test_decision_values_match_go() -> None:
    assert (int(Decision.DROP), int(Decision.RECORD_ONLY), int(Decision.RECORD_AND_SAMPLE)) == (
        0,
        1,
        2,
    )


def test_all_samplers_satisfy_protocol() -> None:
    for s in (AlwaysOn(), AlwaysOff(), Ratio(0.5), ParentBased(AlwaysOn())):
        assert isinstance(s, Sampler)


def test_module_exports() -> None:
    assert set(sampler.__all__) >= {"AlwaysOn", "AlwaysOff", "Ratio", "ParentBased"}


# --------------------------------------------------------- AlwaysOn / Off


@pytest.mark.parametrize("parent", PARENTS)
def test_always_on_samples_everything(parent: bool | None) -> None:
    for trace_id in (LOW_ID, HIGH_ID, *_ids(50)):
        result = AlwaysOn().should_sample(trace_id, "op", parent)
        assert isinstance(result, SamplingResult)
        assert result.decision is Decision.RECORD_AND_SAMPLE


@pytest.mark.parametrize("parent", PARENTS)
def test_always_off_drops_everything(parent: bool | None) -> None:
    for trace_id in (LOW_ID, HIGH_ID, *_ids(50)):
        result = AlwaysOff().should_sample(trace_id, "op", parent)
        assert result.decision is Decision.DROP
        assert result.sampled is False


# ------------------------------------------------------------------ Ratio


@pytest.mark.parametrize("ratio", [-0.0001, -1.0, 1.0001, 2.0, math.nan, math.inf, -math.inf])
def test_ratio_rejects_out_of_range(ratio: float) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message is not part of the contract
        Ratio(ratio)


@pytest.mark.parametrize("ratio", [0.0, 0.25, 0.5, 1.0, 0, 1])
def test_ratio_accepts_closed_unit_interval(ratio: float) -> None:
    assert Ratio(ratio).ratio == ratio


def test_ratio_zero_never_samples() -> None:
    s = Ratio(0.0)
    for trace_id in (LOW_ID, HIGH_ID, ZERO_LOW_ID, *_ids(500)):
        assert s.should_sample(trace_id, "op", None).sampled is False


def test_ratio_one_always_samples() -> None:
    s = Ratio(1.0)
    for trace_id in (LOW_ID, HIGH_ID, ZERO_LOW_ID, *_ids(500)):
        assert s.should_sample(trace_id, "op", None).sampled is True


def test_ratio_half_splits_low_and_high_ids() -> None:
    s = Ratio(0.5)
    assert s.should_sample(LOW_ID, "op", None).sampled is True
    assert s.should_sample(HIGH_ID, "op", None).sampled is False


def test_ratio_decision_is_drop_or_sample() -> None:
    s = Ratio(0.5)
    assert s.should_sample(LOW_ID, "op", None).decision is Decision.RECORD_AND_SAMPLE
    assert s.should_sample(HIGH_ID, "op", None).decision is Decision.DROP


def test_ratio_is_deterministic_across_calls_and_instances() -> None:
    ids = _ids(500)
    first = [Ratio(0.3).should_sample(t, "a", None).sampled for t in ids]
    again = [Ratio(0.3).should_sample(t, "a", None).sampled for t in ids]
    same = Ratio(0.3)
    repeated = [same.should_sample(t, "a", None).sampled for t in ids]
    assert first == again == repeated


def test_ratio_ignores_name_and_parent() -> None:
    s = Ratio(0.4)
    for trace_id in _ids(300):
        decisions = {
            s.should_sample(trace_id, name, parent).sampled
            for name in ("a", "b", "")
            for parent in PARENTS
        }
        assert len(decisions) == 1


def test_ratio_depends_only_on_low_8_bytes() -> None:
    s = Ratio(0.5)
    rng = random.Random(99)
    for _ in range(200):
        low = rng.randbytes(8)
        decisions = {s.should_sample(rng.randbytes(8) + low, "op", None).sampled for _ in range(8)}
        assert len(decisions) == 1


def test_ratio_is_monotonic_in_ratio() -> None:
    ratios = [0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]
    samplers = [Ratio(r) for r in ratios]
    for trace_id in _ids(500):
        decisions = [s.should_sample(trace_id, "op", None).sampled for s in samplers]
        # Once sampled at some ratio, sampled at every larger ratio.
        assert decisions == sorted(decisions)


@pytest.mark.parametrize("ratio", [0.01, 0.1, 0.25, 0.5, 0.9])
def test_ratio_statistics_on_random_ids(ratio: float) -> None:
    n = 20_000
    s = Ratio(ratio)
    sampled = sum(s.should_sample(t, "op", None).sampled for t in _ids(n, seed=42))
    # 5 standard deviations of a binomial: never flaky with a fixed seed.
    tolerance = 5 * math.sqrt(n * ratio * (1 - ratio))
    assert abs(sampled - n * ratio) <= tolerance


# ------------------------------------------------------------ ParentBased


@pytest.mark.parametrize(
    ("root", "parent", "expected"),
    [
        (AlwaysOn(), None, True),
        (AlwaysOff(), None, False),
        (AlwaysOn(), True, True),
        (AlwaysOff(), True, True),
        (AlwaysOn(), False, False),
        (AlwaysOff(), False, False),
    ],
)
def test_parent_based_matrix(root: Sampler, parent: bool | None, expected: bool) -> None:
    result = ParentBased(root).should_sample(LOW_ID, "op", parent)
    assert result.sampled is expected


def test_parent_based_unsampled_parent_is_dropped_not_recorded() -> None:
    result = ParentBased(AlwaysOn()).should_sample(LOW_ID, "op", False)
    assert result.decision is Decision.DROP


def test_parent_based_sampled_parent_records_and_samples() -> None:
    result = ParentBased(AlwaysOff()).should_sample(LOW_ID, "op", True)
    assert result.decision is Decision.RECORD_AND_SAMPLE


def test_parent_based_root_property() -> None:
    root = Ratio(0.2)
    assert ParentBased(root).root is root


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, bool | None]] = []

    def should_sample(
        self, trace_id: bytes, name: str, parent_sampled: bool | None
    ) -> SamplingResult:
        self.calls.append((trace_id, name, parent_sampled))
        return SamplingResult(Decision.RECORD_AND_SAMPLE)


def test_parent_based_delegates_roots_with_arguments() -> None:
    root = _Recording()
    ParentBased(root).should_sample(LOW_ID, "root-op", None)
    assert root.calls == [(LOW_ID, "root-op", None)]


@pytest.mark.parametrize("parent", [True, False])
def test_parent_based_does_not_consult_root_with_parent(parent: bool) -> None:
    root = _Recording()
    ParentBased(root).should_sample(LOW_ID, "child", parent)
    assert root.calls == []


def test_parent_based_with_ratio_root_is_deterministic() -> None:
    s = ParentBased(Ratio(0.5))
    assert s.should_sample(LOW_ID, "op", None).sampled is True
    assert s.should_sample(HIGH_ID, "op", None).sampled is False
    assert s.should_sample(HIGH_ID, "op", True).sampled is True

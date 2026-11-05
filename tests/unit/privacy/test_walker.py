"""The policy walker: cycles, aliases, the node budget, determinism and thread-safety."""

from __future__ import annotations

import copy
import random
import threading
from typing import Any

import pytest

from pyrock.privacy import ALIAS_MARKER, CYCLE_MARKER, MAX_NODES, Policy
from tests.unit.privacy._helpers import SECRET, count_nodes, leaks


class _Counter:
    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, path: str, value: Any) -> tuple[Any, bool]:
        with self.lock:
            self.calls += 1
        return value, True


# ----------------------------------------------------------------- cycles


def test_dict_self_cycle() -> None:
    loop: dict[str, Any] = {"name": "root"}
    loop["self"] = loop
    out = Policy().apply({"payload": loop})
    assert out["payload"]["name"] == "root"
    assert out["payload"]["self"] == CYCLE_MARKER


def test_list_self_cycle() -> None:
    items: list[Any] = [1]
    items.append(items)
    assert Policy().apply({"items": items}) == {"items": [1, CYCLE_MARKER]}


def test_indirect_cycle() -> None:
    a: dict[str, Any] = {"k": "a"}
    b: dict[str, Any] = {"k": "b", "a": a}
    a["b"] = b
    out = Policy().apply({"root": a})
    assert out["root"]["k"] == "a"
    assert out["root"]["b"]["k"] == "b"
    assert out["root"]["b"]["a"] == CYCLE_MARKER


def test_top_level_mapping_cycle() -> None:
    attrs: dict[str, Any] = {"x": 1}
    attrs["me"] = attrs
    out = Policy().apply(attrs)
    assert out["x"] == 1
    me = out["me"]
    assert me == CYCLE_MARKER or (isinstance(me, dict) and me.get("me") == CYCLE_MARKER)


def test_four_node_cyclic_graph_is_bounded() -> None:
    """Go ``TestPolicyBoundsCyclicGraphWork``."""
    counter = _Counter()
    value: dict[str, Any] = {}
    for key in ("a", "b", "c", "d"):
        value[key] = value
    out = Policy(redact=counter).apply({"payload": value})
    assert counter.calls <= MAX_NODES
    assert out["payload"] == dict.fromkeys(("a", "b", "c", "d"), CYCLE_MARKER)


# ---------------------------------------------------------------- aliases


def test_repeated_alias_in_dict() -> None:
    shared = {"x": 1}
    out = Policy().apply({"payload": {"a": shared, "b": shared}})
    assert out["payload"]["a"] == {"x": 1}
    assert out["payload"]["b"] == ALIAS_MARKER


def test_repeated_alias_in_list() -> None:
    shared = [1, 2]
    out = Policy().apply({"items": [shared, shared, shared]})
    assert out["items"] == [[1, 2], ALIAS_MARKER, ALIAS_MARKER]


def test_alias_across_top_level_keys() -> None:
    shared = {"v": "s"}
    out = Policy().apply({"a": shared, "b": shared})
    assert out["a"] == {"v": "s"}
    assert out["b"] == ALIAS_MARKER


def test_equal_but_distinct_containers_are_not_aliases() -> None:
    out = Policy().apply({"a": {"x": 1}, "b": {"x": 1}})
    assert out == {"a": {"x": 1}, "b": {"x": 1}}


def test_repeated_immutable_scalars_are_not_aliases() -> None:
    s = "repeated-" * 3
    big = 10**20
    out = Policy().apply({"a": s, "b": s, "items": [s, s, big, big], "c": big})
    assert out == {"a": s, "b": s, "items": [s, s, big, big], "c": big}


def test_shared_dag_keeps_first_occurrence_and_input() -> None:
    """Go ``TestPolicyBoundsSharedDAGAndPreservesInput``."""
    counter = _Counter()
    leaf = {"visible": "retained"}
    root = {"a": leaf, "b": leaf}
    snapshot = copy.deepcopy(root)
    out = Policy(redact=counter).apply({"payload": root})
    assert counter.calls <= MAX_NODES
    assert out["payload"]["a"] == {"visible": "retained"}
    assert out["payload"]["b"] == ALIAS_MARKER
    assert root == snapshot
    assert root["a"] is root["b"]


def test_alias_tracking_is_per_call() -> None:
    shared = {"x": 1}
    policy = Policy()
    first = policy.apply({"a": shared})
    second = policy.apply({"a": shared})
    assert first == second == {"a": {"x": 1}}


# ------------------------------------------------------------ node budget


def _tree(depth: int, fanout: int = 4) -> Any:
    if depth == 0:
        return "leaf"
    return {f"k{i}": _tree(depth - 1, fanout) for i in range(fanout)}


def test_tree_traversal_is_bounded() -> None:
    """Go ``TestPolicyHasStrictTotalTraversalBudget``: ~21k nodes in, at most 4096 visited."""
    counter = _Counter()
    out = Policy(redact=counter).apply({"payload": _tree(7)})
    assert counter.calls <= MAX_NODES
    assert count_nodes(out) <= MAX_NODES + 64


def test_list_tree_traversal_is_bounded() -> None:
    def tree(depth: int) -> Any:
        return "leaf" if depth == 0 else [tree(depth - 1) for _ in range(4)]

    counter = _Counter()
    out = Policy(redact=counter).apply({"payload": tree(7)})
    assert counter.calls <= MAX_NODES
    assert count_nodes(out) <= MAX_NODES + 64


def test_wide_mapping_is_bounded() -> None:
    counter = _Counter()
    attrs = {f"key{i}": i for i in range(3 * MAX_NODES)}
    out = Policy(redact=counter).apply(attrs)
    assert counter.calls <= MAX_NODES
    assert len(out) <= MAX_NODES
    assert list(out) == list(attrs)[: len(out)]


def test_wide_list_is_bounded() -> None:
    counter = _Counter()
    out = Policy(redact=counter).apply({"items": list(range(3 * MAX_NODES))})
    assert counter.calls <= MAX_NODES
    assert count_nodes(out) <= MAX_NODES + 64


def test_small_values_are_not_truncated_by_the_budget() -> None:
    attrs = {f"k{i}": {"v": i, "w": [i, i + 1]} for i in range(200)}
    assert Policy().apply(attrs) == attrs


def test_deep_nesting_does_not_crash() -> None:
    deep: dict[str, Any] = {"leaf": "v"}
    for _ in range(5000):
        deep = {"n": deep}
    out = Policy().apply({"deep": deep})
    assert isinstance(out, dict)
    assert count_nodes(out) <= MAX_NODES + 64


def test_deep_list_nesting_does_not_crash() -> None:
    deep: Any = "leaf"
    for _ in range(5000):
        deep = [deep]
    out = Policy().apply({"deep": deep})
    assert isinstance(out, dict)


def test_budget_never_leaks_secrets() -> None:
    attrs = {f"k{i}": {"password": SECRET, "v": i} for i in range(3 * MAX_NODES)}
    assert not leaks(Policy().apply(attrs))


# ------------------------------------------------------------ determinism


def _random_value(rng: random.Random, depth: int) -> Any:
    kind = rng.randrange(6 if depth < 4 else 3)
    if kind == 0:
        return rng.choice(["a", "bb", "password-ish", "x" * rng.randrange(50)])
    if kind == 1:
        return rng.randint(-1000, 1000)
    if kind == 2:
        return rng.choice([True, False, 1.5, -0.25])
    if kind == 3:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(5))]
    keys = ["id", "name", "password", "error", "message", "user", "token", "http.url", "z"]
    return {
        rng.choice(keys) + str(i): _random_value(rng, depth + 1) for i in range(rng.randrange(5))
    }


def _random_attrs(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    keys = ["user.id", "user.password", "error.message", "http.user_agent", "payload", "a", "b"]
    return {f"{rng.choice(keys)}.{i}": _random_value(rng, 0) for i in range(rng.randrange(1, 8))}


POLICIES = [
    Policy(),
    Policy(attribute_keys=["user.*", "payload"]),  # type: ignore[arg-type]
    Policy(allow_error_details=True, max_string_length=8),
]


@pytest.mark.parametrize("policy", POLICIES, ids=["default", "allowlist", "details"])
def test_apply_is_deterministic_property(policy: Policy) -> None:
    for seed in range(150):
        attrs = _random_attrs(seed)
        snapshot = copy.deepcopy(attrs)
        first = policy.apply(attrs)
        assert policy.apply(attrs) == first
        assert policy.apply(copy.deepcopy(attrs)) == first
        assert attrs == snapshot
        keys = [k for k in attrs if k in first]
        assert list(first) == keys


@pytest.mark.parametrize(
    "policy",
    [Policy(), POLICIES[1], Policy(allow_error_details=True)],
    ids=["default", "allowlist", "details"],
)
def test_apply_is_idempotent_property(policy: Policy) -> None:
    for seed in range(150):
        once = policy.apply(_random_attrs(seed))
        assert policy.apply(once) == once


def test_output_never_contains_sensitive_values_property() -> None:
    rng = random.Random(99)
    sensitive = ["password", "authorization", "cookie", "api_key", "secret", "refresh_token"]
    for _ in range(200):
        key = rng.choice(sensitive)
        attrs: dict[str, Any] = {key: SECRET}
        for _ in range(rng.randrange(4)):
            attrs = {rng.choice(["a", "b", "req", "payload"]): attrs}
        assert not leaks(Policy().apply(attrs))


# ---------------------------------------------------------- thread-safety


def test_shared_policy_under_concurrent_use() -> None:
    lock = threading.Lock()
    seen_paths: set[str] = set()

    def redact(path: str, value: Any) -> tuple[Any, bool]:
        with lock:
            seen_paths.add(path)
        if path.endswith("email"):
            return "[email]", True
        return value, True

    policy = Policy(
        attribute_keys=["user.*", "req", "payload"], redact=redact, max_string_length=32
    )  # type: ignore[arg-type]
    shared_leaf = {"v": "shared"}
    loop: dict[str, Any] = {"name": "loop"}
    loop["self"] = loop
    shared_input = {
        "payload": {"a": shared_leaf, "b": shared_leaf, "loop": loop},
        "user.email": "a@b.c",
        "user.password": SECRET,
        "req": {"headers": {"x-request-id": "r", "cookie": SECRET}},
        "other": "dropped",
    }
    inputs = [_random_attrs(seed) for seed in range(20)]
    expected_shared = policy.apply(shared_input)
    expected = [policy.apply(a) for a in inputs]
    assert expected_shared["payload"]["b"] == ALIAS_MARKER
    assert expected_shared["payload"]["loop"]["self"] == CYCLE_MARKER

    n_threads = 32
    barrier = threading.Barrier(n_threads)
    failures: list[str] = []

    def work(index: int) -> None:
        barrier.wait()
        try:
            for round_ in range(25):
                if policy.apply(shared_input) != expected_shared:
                    failures.append(f"thread {index} round {round_}: shared input differed")
                j = (index + round_) % len(inputs)
                if policy.apply(inputs[j]) != expected[j]:
                    failures.append(f"thread {index} round {round_}: input {j} differed")
        except Exception as exc:
            failures.append(f"thread {index}: {type(exc).__name__}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert failures == []
    assert "req.headers.x-request-id" in seen_paths
    assert not leaks(expected_shared)


def test_concurrent_label_and_message_cleaning() -> None:
    policy = Policy(max_string_length=8)
    expected = (policy.label_value("k", "abcdefghijkl"), policy.message("m" * 40))
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    errors: list[str] = []

    def work() -> None:
        barrier.wait()
        try:
            for _ in range(50):
                pair = (policy.label_value("k", "abcdefghijkl"), policy.message("m" * 40))
                with lock:
                    results.append(pair)
        except Exception as exc:
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=work) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(results) == n_threads * 50
    assert set(results) == {expected}

"""The OTLP Profiles encoder (``v1development`` v0.2.0, Go's dictionary layout).

Bytes are decoded with the test-side reader in ``_proto.py``, which follows
the field numbers Go's encoder writes. The official-schema check only looks at
fields that are stable across opentelemetry-proto releases.
"""

from __future__ import annotations

import collections
from collections.abc import Mapping
from typing import Any

import pytest

from pyrock.profile._otlp import CONTENT_TYPE, encode_export_request
from pyrock.profile._pprof import Frame, Profile, Sample, SampleType
from tests.unit.profile._helpers import DURATION_NS, START_NS, make_profile
from tests.unit.profile._proto import decode_otlp_profiles, value_depth


def encode(profile: Profile | None = None, **kw: Any) -> bytes:
    options: dict[str, Any] = {"service": "unit-svc", "profile_type": "cpu"}
    options.update(kw)
    return encode_export_request(profile if profile is not None else make_profile(), **options)


def keep_all(labels: Mapping[str, str | int]) -> Mapping[str, Any]:
    return dict(labels)


def with_labels(labels: Mapping[str, str | int]) -> Profile:
    return make_profile(
        samples=(Sample((Frame("leaf_fn", "/a.py", 1),), (1, 1), labels),),
    )


def test_content_type() -> None:
    assert CONTENT_TYPE == "application/x-protobuf"


# ---------------------------------------------------------------- resource


def test_resource_carries_service_name_and_resource_attributes() -> None:
    resource = {"deployment.environment": "prod", "host.cores": 4, "flag": True, "ratio": 0.5}
    request = decode_otlp_profiles(encode(service="checkout", resource=resource))
    assert request.resource_profiles_count == 1
    assert request.resource["service.name"] == "checkout"
    for key, value in resource.items():
        assert request.resource[key] == value


def test_resource_defaults_to_service_name_only() -> None:
    request = decode_otlp_profiles(encode(service="only-svc"))
    assert request.resource == {"service.name": "only-svc"}


# ---------------------------------------------------------------- profiles


def test_one_profile_per_sample_type() -> None:
    request = decode_otlp_profiles(encode())
    assert sorted(p.sample_type for p in request.profiles) == [
        ("cpu", "nanoseconds"),
        ("samples", "count"),
    ]


def test_each_profile_holds_its_column_of_values() -> None:
    request = decode_otlp_profiles(encode())
    counts = collections.Counter(
        (tuple(s.stack), tuple(s.values)) for s in request.profile("samples").samples
    )
    assert counts == collections.Counter(
        {
            (("leaf_fn", "mid_fn", "root_fn"), (3,)): 1,
            (("leaf_fn", "mid_fn", "root_fn"), (1,)): 1,
            (("mid_fn", "root_fn"), (2,)): 1,
        }
    )
    cpu = collections.Counter(
        (tuple(s.stack), tuple(s.values)) for s in request.profile("cpu").samples
    )
    assert cpu == collections.Counter(
        {
            (("leaf_fn", "mid_fn", "root_fn"), (30_000_000,)): 1,
            (("leaf_fn", "mid_fn", "root_fn"), (10_000_000,)): 1,
            (("mid_fn", "root_fn"), (20_000_000,)): 1,
        }
    )


def test_timing_and_period() -> None:
    request = decode_otlp_profiles(encode())
    for profile in request.profiles:
        assert profile.time_unix_nano == START_NS
        assert profile.duration_nano == DURATION_NS
        assert profile.period_type == ("cpu", "nanoseconds")
        assert profile.period == 10_000_000


def test_profile_id_is_sixteen_nonzero_bytes_when_set() -> None:
    for profile in decode_otlp_profiles(encode()).profiles:
        assert profile.profile_id == b"" or (
            len(profile.profile_id) == 16 and any(profile.profile_id)
        )


@pytest.mark.parametrize("kind", ["cpu", "wall", "heap", "goroutine", "mutex", "block"])
def test_profile_type_attribute(kind: str) -> None:
    request = decode_otlp_profiles(encode(profile_type=kind))
    assert {p.attributes["profile.type"] for p in request.profiles} == {kind}


def test_profile_level_attrs_are_profile_attributes() -> None:
    profile = make_profile(samples=(), attrs={"profile.note": "no instrumented locks"})
    request = decode_otlp_profiles(encode(profile, profile_type="mutex"))
    assert request.profiles
    for decoded in request.profiles:
        assert decoded.attributes["profile.note"] == "no instrumented locks"
        assert decoded.samples == []


# ---------------------------------------------------------------- dictionary


def test_dictionary_tables_start_with_a_zero_entry() -> None:
    dictionary = decode_otlp_profiles(encode()).dictionary
    assert dictionary.strings[0] == ""
    assert dictionary.functions[0] == b""
    assert dictionary.locations[0] == b""
    assert dictionary.stacks[0] == b""
    assert dictionary.mappings[0] == b""
    assert dictionary.attributes[0].key == ""


def test_dictionary_tables_are_deduplicated() -> None:
    dictionary = decode_otlp_profiles(encode()).dictionary
    assert len(dictionary.strings) == len(set(dictionary.strings))
    assert sorted(dictionary.function_names[1:]) == ["leaf_fn", "mid_fn", "root_fn"]
    assert len(dictionary.functions[1:]) == len(set(dictionary.functions[1:]))
    assert len(dictionary.locations[1:]) == 4  # leaf:10, leaf:12, mid:20, root:30
    assert len(dictionary.locations[1:]) == len(set(dictionary.locations[1:]))
    assert len(dictionary.mappings[1:]) == len(set(dictionary.mappings[1:]))
    type_attrs = [a for a in dictionary.attributes if a.key == "profile.type"]
    assert len(type_attrs) == 1


def test_dictionary_is_shared_by_every_profile() -> None:
    request = decode_otlp_profiles(encode())
    stacks = {s.stack_index for p in request.profiles for s in p.samples}
    assert stacks
    assert all(0 < i < len(request.dictionary.stacks) for i in stacks)


def test_empty_profile_encodes() -> None:
    request = decode_otlp_profiles(encode(make_profile(samples=())))
    assert len(request.profiles) == 2
    assert all(p.samples == [] for p in request.profiles)


# ---------------------------------------------------------------- labels


def test_labels_are_dropped_without_a_filter() -> None:
    request = decode_otlp_profiles(encode())
    for profile in request.profiles:
        for sample in profile.samples:
            assert sample.attribute_indices == []
    keys = {a.key for a in request.dictionary.attributes}
    assert "thread.name" not in keys
    assert "worker" not in request.dictionary.strings


def test_label_filter_output_becomes_sample_attributes() -> None:
    def drop_task_id(labels: Mapping[str, str | int]) -> Mapping[str, Any]:
        return {k: v for k, v in labels.items() if k != "task.id"}

    request = decode_otlp_profiles(encode(label_filter=drop_task_id))
    labelled = [s for p in request.profiles for s in p.samples if s.attributes]
    assert len(labelled) == len(request.profiles)  # the one labelled sample, per profile
    assert all(s.attributes == {"thread.name": "worker"} for s in labelled)
    assert "task.id" not in {a.key for a in request.dictionary.attributes}


def test_label_filter_sees_the_sample_labels() -> None:
    seen: list[dict[str, str | int]] = []

    def spy(labels: Mapping[str, str | int]) -> Mapping[str, Any]:
        seen.append(dict(labels))
        return {}

    request = decode_otlp_profiles(encode(label_filter=spy))
    assert {"thread.name": "worker", "task.id": 7} in seen
    assert all(not s.attributes for p in request.profiles for s in p.samples)


def test_numeric_labels_stay_numeric() -> None:
    request = decode_otlp_profiles(encode(with_labels({"task.id": 7}), label_filter=keep_all))
    values = {s.attributes.get("task.id") for p in request.profiles for s in p.samples}
    assert values == {7}


def test_filter_output_may_be_a_shallow_list() -> None:
    request = decode_otlp_profiles(
        encode(with_labels({"k": "v"}), label_filter=lambda _labels: {"tags": ["a", "b"]})
    )
    values = [s.attributes["tags"] for p in request.profiles for s in p.samples]
    assert values
    assert all(v == ["a", "b"] for v in values)


def test_sixty_four_labels_are_accepted() -> None:
    labels = {f"k{i}": f"v{i}" for i in range(64)}
    request = decode_otlp_profiles(encode(with_labels(labels), label_filter=keep_all))
    sample = request.profiles[0].samples[0]
    assert len(sample.attributes) == 64


def test_more_than_sixty_four_labels_are_rejected() -> None:
    labels = {f"k{i}": f"v{i}" for i in range(65)}
    with pytest.raises(ValueError, match=r"."):
        encode(with_labels(labels), label_filter=keep_all)


def _nested(depth: int) -> Any:
    value: Any = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def test_label_values_deeper_than_eight_are_rejected() -> None:
    assert value_depth(_nested(20)) == 20
    with pytest.raises(ValueError, match=r"."):
        encode(with_labels({"k": "v"}), label_filter=lambda _labels: {"deep": _nested(20)})


def test_label_values_over_the_node_budget_are_rejected() -> None:
    with pytest.raises(ValueError, match=r"."):
        encode(with_labels({"k": "v"}), label_filter=lambda _labels: {"many": list(range(5000))})


def test_rejection_never_truncates_silently() -> None:
    # A rejected label set produces no bytes at all rather than a shortened set.
    labels = {f"k{i}": "v" for i in range(100)}
    result: bytes | None = None
    with pytest.raises(ValueError, match=r"."):
        result = encode(with_labels(labels), label_filter=keep_all)
    assert result is None


# ---------------------------------------------------------------- max_bytes


def test_max_bytes_is_enforced() -> None:
    full = encode()
    assert encode(max_bytes=len(full) + 1024)
    with pytest.raises(ValueError, match=r"."):
        encode(max_bytes=64)


def test_max_bytes_boundary() -> None:
    full = encode()
    assert len(encode(max_bytes=len(full))) == len(full)
    with pytest.raises(ValueError, match=r"."):
        encode(max_bytes=len(full) - 1)


# ---------------------------------------------------------------- official schema


@pytest.mark.compat
def test_official_schema_reads_resource_and_dictionary() -> None:
    pb = pytest.importorskip(
        "opentelemetry.proto.collector.profiles.v1development.profiles_service_pb2"
    )
    message = pb.ExportProfilesServiceRequest()
    message.ParseFromString(encode(service="decoded-svc"))
    resource = {
        kv.key: kv.value.string_value for kv in message.resource_profiles[0].resource.attributes
    }
    assert resource["service.name"] == "decoded-svc"
    strings = list(message.dictionary.string_table)
    assert strings[0] == ""
    names = {strings[f.name_strindex] for f in message.dictionary.function_table if f.name_strindex}
    assert {"leaf_fn", "mid_fn", "root_fn"} <= names


def test_profile_samples_cover_whole_input() -> None:
    profile = make_profile(
        sample_types=(SampleType("alloc_space", "bytes"),),
        samples=tuple(Sample((Frame(f"fn_{i}", "/m.py", i + 1),), (i + 1,)) for i in range(20)),
    )
    request = decode_otlp_profiles(encode(profile, profile_type="heap"))
    decoded = request.profile("alloc_space")
    assert sorted(s.values[0] for s in decoded.samples) == list(range(1, 21))
    assert sorted(s.stack[0] for s in decoded.samples) == sorted(f"fn_{i}" for i in range(20))

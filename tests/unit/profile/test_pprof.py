"""The intermediate profile model and the pprof (``profile.proto``) encoder.

Bytes are decoded with the test-side reader in ``_proto.py``; when a Go
toolchain is installed, ``go tool pprof`` reads the gzipped output too.
"""

from __future__ import annotations

import collections
import dataclasses
import gzip
import os
import shutil
import subprocess

import pytest

from pyrock.profile._pprof import Frame, Sample, SampleType, encode, encode_gzip
from tests.unit.profile._helpers import (
    DURATION_NS,
    LEAF,
    LEAF_OTHER_LINE,
    MID,
    ROOT,
    START_NS,
    make_profile,
)
from tests.unit.profile._proto import decode_pprof

# ---------------------------------------------------------------- the model


def test_model_types_are_frozen_values() -> None:
    assert Frame("f") == Frame("f", "", 0, "")
    assert SampleType("cpu", "nanoseconds") == SampleType("cpu", "nanoseconds")
    with pytest.raises(dataclasses.FrozenInstanceError):
        LEAF.line = 3  # type: ignore[misc]
    profile = make_profile()
    assert profile.attrs == {}
    assert Sample((LEAF,), (1,)).labels == {}


# ---------------------------------------------------------------- header fields


def test_string_table_starts_with_empty_string_and_has_no_duplicates() -> None:
    decoded = decode_pprof(encode(make_profile()))
    assert decoded.strings[0] == ""
    assert len(decoded.strings) == len(set(decoded.strings))


def test_sample_types_period_and_timing() -> None:
    decoded = decode_pprof(encode(make_profile()))
    assert decoded.sample_types == [("samples", "count"), ("cpu", "nanoseconds")]
    assert decoded.period_type == ("cpu", "nanoseconds")
    assert decoded.period == 10_000_000
    assert decoded.time_nanos == START_NS
    assert decoded.duration_nanos == DURATION_NS


def test_comments_are_string_table_references() -> None:
    decoded = decode_pprof(encode(make_profile(comments=("first", "second"))))
    assert decoded.comments == ["first", "second"]


def test_profile_without_period_type() -> None:
    decoded = decode_pprof(encode(make_profile(period_type=None, period=0)))
    assert decoded.period_type is None
    assert decoded.period == 0


# ---------------------------------------------------------------- samples


def test_samples_keep_leaf_first_stacks_and_values() -> None:
    decoded = decode_pprof(encode(make_profile()))
    got = collections.Counter(
        (tuple(s.stack), tuple(s.lines), tuple(s.values)) for s in decoded.samples
    )
    assert got == collections.Counter(
        {
            (("leaf_fn", "mid_fn", "root_fn"), (10, 20, 30), (3, 30_000_000)): 1,
            (("leaf_fn", "mid_fn", "root_fn"), (12, 20, 30), (1, 10_000_000)): 1,
            (("mid_fn", "root_fn"), (20, 30), (2, 20_000_000)): 1,
        }
    )


def test_every_sample_has_one_value_per_sample_type() -> None:
    decoded = decode_pprof(encode(make_profile()))
    assert decoded.samples
    assert all(len(s.values) == len(decoded.sample_types) for s in decoded.samples)


def test_negative_values_round_trip_as_int64() -> None:
    profile = make_profile(
        sample_types=(SampleType("inuse_space", "bytes"),),
        samples=(Sample((LEAF,), (-4096,)),),
    )
    decoded = decode_pprof(encode(profile))
    assert [s.values for s in decoded.samples] == [[-4096]]


def test_string_and_numeric_labels() -> None:
    decoded = decode_pprof(encode(make_profile()))
    labelled = [s for s in decoded.samples if s.labels]
    assert len(labelled) == 1
    assert labelled[0].labels == {"thread.name": ["worker"], "task.id": [7]}


def test_empty_profile_still_declares_sample_types() -> None:
    decoded = decode_pprof(encode(make_profile(samples=())))
    assert decoded.samples == []
    assert decoded.sample_types == [("samples", "count"), ("cpu", "nanoseconds")]
    assert decoded.strings[0] == ""


# ---------------------------------------------------------------- dictionaries


def test_functions_are_deduplicated() -> None:
    decoded = decode_pprof(encode(make_profile()))
    names = [decoded.functions[i].name for i in decoded.function_order]
    assert sorted(names) == ["leaf_fn", "mid_fn", "root_fn"]
    assert 0 not in decoded.functions
    assert len(set(decoded.function_order)) == len(decoded.function_order)
    files = {decoded.functions[i].name: decoded.functions[i].filename for i in decoded.functions}
    assert files == {
        "leaf_fn": "/srv/app/leaf.py",
        "mid_fn": "/srv/app/mid.py",
        "root_fn": "/srv/app/root.py",
    }


def test_locations_are_deduplicated_per_function_and_line() -> None:
    decoded = decode_pprof(encode(make_profile()))
    assert 0 not in decoded.locations
    assert len(set(decoded.location_order)) == len(decoded.location_order)
    keys = [
        tuple((decoded.functions[f].name, line) for f, line in decoded.locations[i].lines)
        for i in decoded.location_order
    ]
    assert sorted(keys) == [
        (("leaf_fn", 10),),
        (("leaf_fn", 12),),
        (("mid_fn", 20),),
        (("root_fn", 30),),
    ]
    referenced = {i for s in decoded.samples for i in s.location_ids}
    assert referenced == set(decoded.location_order)


def test_mappings_are_deduplicated_and_referenced_consistently() -> None:
    decoded = decode_pprof(encode(make_profile()))
    assert 0 not in decoded.mappings
    assert len(set(decoded.mapping_order)) == len(decoded.mapping_order)
    files = [decoded.mappings[i].filename for i in decoded.mapping_order]
    assert len(files) == len(set(files))
    for loc in decoded.locations.values():
        assert loc.mapping_id == 0 or loc.mapping_id in decoded.mappings


def test_repeated_frames_share_dictionary_entries() -> None:
    sample = Sample((LEAF, MID, ROOT), (1, 1))
    small = decode_pprof(encode(make_profile(samples=(sample,))))
    large = decode_pprof(encode(make_profile(samples=(sample,) * 50)))
    assert len(large.functions) == len(small.functions) == 3
    assert len(large.locations) == len(small.locations) == 3
    assert len(large.strings) == len(small.strings)
    assert sum(s.values[0] for s in large.samples) == 50


def test_encoding_is_deterministic() -> None:
    assert encode(make_profile()) == encode(make_profile())


# ---------------------------------------------------------------- gzip


def test_encode_gzip_is_gzip_of_encode() -> None:
    profile = make_profile()
    data = encode_gzip(profile)
    assert data[:2] == b"\x1f\x8b"
    assert gzip.decompress(data) == encode(profile)


# ---------------------------------------------------------------- go tool pprof


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("go") is None, reason="needs a Go toolchain for go tool pprof")
def test_go_tool_pprof_reads_the_output(tmp_path: os.PathLike[str]) -> None:
    path = os.path.join(tmp_path, "cpu.pb.gz")
    with open(path, "wb") as fh:
        fh.write(encode_gzip(make_profile()))
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOFLAGS": ""}
    result = subprocess.run(
        ["go", "tool", "pprof", "-raw", path],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "samples/count" in result.stdout
    assert "cpu/nanoseconds" in result.stdout
    assert "leaf_fn" in result.stdout
    assert "root_fn" in result.stdout

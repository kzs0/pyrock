"""OTLP Profiles encoder (``v1development``, proto v0.2.0, same dictionaries as Go).

Produces ``ExportProfilesServiceRequest`` bytes. Post-policy label sets above
64 items, 4,096 nodes or depth 8 are rejected (``ValueError``), never silently
truncated.

Field numbers (as Go's encoder writes them)::

    ExportProfilesServiceRequest: 1 resource_profiles, 2 dictionary
    ResourceProfiles: 1 resource, 2 scope_profiles
    ScopeProfiles:    1 scope, 2 profiles
    Profile: 1 sample_type, 2 sample, 3 time_unix_nano (fixed64), 4 duration_nano,
             5 period_type, 6 period, 7 profile_id, 11 attribute_indices
    Sample:  1 stack_index, 2 values, 3 attribute_indices
    ProfilesDictionary: 1 mapping_table, 2 location_table, 3 function_table,
             4 link_table, 5 string_table, 6 attribute_table, 7 stack_table
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pyrock._version import __version__
from pyrock.attr import AttrValue
from pyrock.profile._pprof import (
    MAPPING_NAME,
    Frame,
    Profile,
    SampleType,
    StringTable,
    system_name,
)
from pyrock.trace.otlp._pb import Writer, encode_any_value, encode_key_value, resource_attributes

__all__ = ["CONTENT_TYPE", "LabelFilter", "encode_export_request"]

CONTENT_TYPE = "application/x-protobuf"

LabelFilter = Callable[[Mapping[str, str | int]], Mapping[str, Any]]
"""Cleans one sample's labels (``Policy.profile_labels`` when ``allow_profile_labels``)."""

MAX_LABELS = 64
MAX_LABEL_NODES = 4096
MAX_LABEL_DEPTH = 8
_SCOPE_NAME = "pyrock"


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


class _Budget:
    """Remaining value nodes for one sample's label set."""

    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = MAX_LABEL_NODES

    def take(self, depth: int) -> None:
        if depth > MAX_LABEL_DEPTH:
            raise ValueError(f"profile label values nest deeper than {MAX_LABEL_DEPTH}")
        self.nodes -= 1
        if self.nodes < 0:
            raise ValueError(f"profile labels exceed {MAX_LABEL_NODES} nodes")


def _any_value(value: Any, depth: int, budget: _Budget) -> bytes | None:
    """An ``AnyValue`` for a label value; ``None`` drops an unsupported scalar."""
    budget.take(depth)
    if isinstance(value, list | tuple):
        if len(value) > MAX_LABELS:
            raise ValueError(f"profile label arrays exceed {MAX_LABELS} items")
        array = Writer()
        for item in value:
            encoded = _any_value(item, depth + 1, budget)
            array.message(1, encoded if encoded is not None else b"")
        w = Writer()
        w.message(5, array)
        return w.getvalue()
    if isinstance(value, Mapping):
        if len(value) > MAX_LABELS or not all(isinstance(k, str) for k in value):
            raise ValueError(f"profile label maps need at most {MAX_LABELS} str keys")
        kvlist = Writer()
        for key in sorted(value):
            encoded = _any_value(value[key], depth + 1, budget)
            kv = Writer()
            kv.string(1, key)
            kv.message(2, encoded if encoded is not None else b"")
            kvlist.message(1, kv)
        w = Writer()
        w.message(6, kvlist)
        return w.getvalue()
    return encode_any_value(value)


class _Dictionary:
    """The request-level tables; each starts with an empty entry at index 0."""

    def __init__(self, *, source_paths: bool) -> None:
        self.strings = StringTable()
        self.functions: dict[tuple[str, str, str], int] = {}
        self.locations: dict[tuple[int, int], int] = {}
        self.stacks: dict[tuple[int, ...], int] = {}
        self.attributes: dict[bytes, int] = {}
        self._source_paths = source_paths
        self._frames: dict[Frame, int] = {}

    def location(self, frame: Frame) -> int:
        index = self._frames.get(frame)
        if index is None:
            filename = frame.filename if self._source_paths else _basename(frame.filename)
            key = (frame.function, system_name(frame), filename)
            fn = self.functions.setdefault(key, len(self.functions) + 1)
            index = self.locations.setdefault((fn, frame.line), len(self.locations) + 1)
            self._frames[frame] = index
        return index

    def stack(self, frames: Sequence[Frame]) -> int:
        key = tuple(self.location(f) for f in frames)
        return self.stacks.setdefault(key, len(self.stacks) + 1)

    def attribute(self, key: str, any_value: bytes) -> int:
        w = Writer()
        w.varint(1, self.strings(key))
        w.message(2, any_value)
        entry = w.getvalue()
        return self.attributes.setdefault(entry, len(self.attributes) + 1)

    def labels(self, cleaned: Mapping[str, Any]) -> list[int]:
        """Attribute indices for one sample's filtered labels (validated first)."""
        if len(cleaned) > MAX_LABELS:
            raise ValueError(f"profile labels exceed {MAX_LABELS} items")
        budget = _Budget()
        encoded: list[tuple[str, bytes]] = []
        for key in sorted(cleaned):
            value = _any_value(cleaned[key], 0, budget)
            if value is not None:
                encoded.append((key, value))
        return [self.attribute(key, value) for key, value in encoded]

    def value_type(self, sample_type: SampleType) -> Writer:
        w = Writer()
        w.varint(1, self.strings(sample_type.type))
        w.varint(2, self.strings(sample_type.unit))
        return w

    def encode(self) -> Writer:
        w = Writer()
        mapping = Writer()
        mapping.varint(4, self.strings(MAPPING_NAME))
        for entry in (b"", mapping.getvalue()):
            w.message(1, entry)
        w.message(2, b"")
        for fn, line in self.locations:
            location, line_w = Writer(), Writer()
            location.varint(1, 1)  # the synthetic mapping
            line_w.varint(1, fn)
            line_w.varint(2, line)
            location.message(3, line_w)
            w.message(2, location)
        w.message(3, b"")
        for name, system, filename in self.functions:
            function = Writer()
            function.varint(1, self.strings(name))
            function.varint(2, self.strings(system))
            function.varint(3, self.strings(filename))
            w.message(3, function)
        w.message(4, b"")  # link_table
        for text in self.strings.strings:
            w.string(5, text)
        w.message(6, b"")
        for entry in self.attributes:
            w.message(6, entry)
        w.message(7, b"")
        for locations in self.stacks:
            stack = Writer()
            for index in locations:
                stack.varint(1, index)
            w.message(7, stack)
        return w


def _resource(service: str, resource: Mapping[str, AttrValue] | None) -> Writer:
    w = Writer()
    for key, value in resource_attributes(service, resource).items():
        kv = encode_key_value(key, value)
        if kv is not None:
            w.message(1, kv)
    return w


def _scope() -> Writer:
    w = Writer()
    w.string(1, _SCOPE_NAME)
    w.string(2, __version__)
    return w


def encode_export_request(
    profile: Profile,
    /,
    *,
    service: str,
    profile_type: str,
    resource: Mapping[str, AttrValue] | None = None,
    max_bytes: int | None = None,
    label_filter: LabelFilter | None = None,
    source_paths: bool = True,
) -> bytes:
    """Encode one profile; ``ValueError`` if the result would exceed ``max_bytes``.

    Layout follows Go's encoder: one ``Profile`` per sample type (each with a
    single ``sample_type``), a shared request-level dictionary whose tables
    start with an empty/zero entry at index 0, and a ``profile.type`` profile
    attribute. Sample labels are emitted only through ``label_filter``
    (``None`` drops every label); its output is checked against the 64 item /
    4,096 node / depth 8 limits and rejected with ``ValueError`` above them.
    Without ``source_paths`` file names are reduced to their base name.
    """
    dictionary = _Dictionary(source_paths=source_paths)
    stacks = [dictionary.stack(sample.stack) for sample in profile.samples]
    labels = [
        dictionary.labels(label_filter(sample.labels))
        if label_filter is not None and sample.labels
        else []
        for sample in profile.samples
    ]
    profile_attrs = [dictionary.attribute("profile.type", _string_value(profile_type))]
    profile_attrs += [dictionary.attribute(k, _string_value(v)) for k, v in profile.attrs.items()]

    scope_profiles = Writer()
    scope_profiles.message(1, _scope())
    for column, sample_type in enumerate(profile.sample_types):
        out = Writer()
        out.message(1, dictionary.value_type(sample_type))
        for sample, stack, attribute_indices in zip(profile.samples, stacks, labels, strict=True):
            s = Writer()
            s.varint(1, stack)
            s.varint(2, sample.values[column])
            for index in attribute_indices:
                s.varint(3, index)
            out.message(2, s)
        out.fixed64(3, profile.start_time_ns)
        out.varint(4, max(profile.duration_ns, 0))
        if profile.period_type is not None:
            out.message(5, dictionary.value_type(profile.period_type))
        out.varint(6, profile.period)
        out.bytes_(7, os.urandom(16))
        for index in profile_attrs:
            out.varint(11, index)
        scope_profiles.message(2, out)

    resource_profiles = Writer()
    resource_profiles.message(1, _resource(service, resource))
    resource_profiles.message(2, scope_profiles)
    request = Writer()
    request.message(1, resource_profiles)
    request.message(2, dictionary.encode())
    data = request.getvalue()
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"encoded profile is {len(data)} bytes, above max_bytes={max_bytes}")
    return data


def _string_value(text: str) -> bytes:
    w = Writer()
    w.string(1, text)
    return w.getvalue()

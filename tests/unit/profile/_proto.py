"""A minimal, test-side protobuf reader for pprof and OTLP Profiles bytes.

It knows nothing about pyrock: it parses the wire format and resolves the two
schemas the encoders target, so the tests do not need the compat group.

* pprof ``perftools.profiles.Profile`` (``profile.proto``).
* OTLP ``ExportProfilesServiceRequest`` from ``v1development`` proto v0.2.0
  (the layout Go's encoder writes: ``Sample.values`` is field 2,
  ``Sample.attribute_indices`` field 3).

Repeated scalar fields are accepted packed or unpacked.
"""

from __future__ import annotations

import struct
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

VARINT, FIXED64, LEN, FIXED32 = 0, 1, 2, 5


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift >= 70:
            raise ValueError("varint too long")


def fields(data: bytes) -> list[tuple[int, int, Any]]:
    """``(field_number, wire_type, value)`` triples; LEN values are ``bytes``."""
    out: list[tuple[int, int, Any]] = []
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError("field number 0")
        if wire == VARINT:
            value, pos = _varint(data, pos)
        elif wire == FIXED64:
            if pos + 8 > len(data):
                raise ValueError("truncated fixed64")
            value = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        elif wire == LEN:
            size, pos = _varint(data, pos)
            if pos + size > len(data):
                raise ValueError("truncated length-delimited field")
            value = bytes(data[pos : pos + size])
            pos += size
        elif wire == FIXED32:
            if pos + 4 > len(data):
                raise ValueError("truncated fixed32")
            value = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((number, wire, value))
    return out


def group(data: bytes) -> dict[int, list[tuple[int, Any]]]:
    """Field number -> list of ``(wire_type, value)`` in order."""
    out: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for number, wire, value in fields(data):
        out[number].append((wire, value))
    return out


def ints(entries: list[tuple[int, Any]]) -> list[int]:
    """A repeated integer field, packed or not."""
    values: list[int] = []
    for wire, value in entries:
        if wire == LEN:
            pos = 0
            while pos < len(value):
                n, pos = _varint(value, pos)
                values.append(n)
        else:
            values.append(value)
    return values


def signed(n: int) -> int:
    """Two's-complement int64 from a decoded varint."""
    return n - (1 << 64) if n >= 1 << 63 else n


def scalar(g: dict[int, list[tuple[int, Any]]], number: int, default: Any = 0) -> Any:
    entries = g.get(number)
    if not entries:
        return default
    return entries[-1][1]


def messages(g: dict[int, list[tuple[int, Any]]], number: int) -> list[bytes]:
    out = []
    for wire, value in g.get(number, []):
        assert wire == LEN, f"field {number} is not length-delimited"
        out.append(value)
    return out


# ------------------------------------------------------------------ pprof


@dataclass
class PprofFunction:
    id: int
    name: str
    system_name: str
    filename: str
    start_line: int


@dataclass
class PprofLocation:
    id: int
    mapping_id: int
    address: int
    lines: list[tuple[int, int]]
    """``(function_id, line)`` pairs."""


@dataclass
class PprofMapping:
    id: int
    filename: str


@dataclass
class PprofSample:
    location_ids: list[int]
    values: list[int]
    labels: dict[str, list[Any]]
    stack: list[str] = field(default_factory=list)
    """Function names, leaf first (first line of each location)."""
    lines: list[int] = field(default_factory=list)


@dataclass
class Pprof:
    strings: list[str]
    sample_types: list[tuple[str, str]]
    samples: list[PprofSample]
    functions: dict[int, PprofFunction]
    locations: dict[int, PprofLocation]
    mappings: dict[int, PprofMapping]
    location_order: list[int]
    function_order: list[int]
    mapping_order: list[int]
    time_nanos: int
    duration_nanos: int
    period_type: tuple[str, str] | None
    period: int
    comments: list[str]

    def samples_with(self, function_substring: str) -> list[PprofSample]:
        return [s for s in self.samples if any(function_substring in n for n in s.stack)]

    def column(self, sample_type: str) -> int:
        names = [t for t, _ in self.sample_types]
        assert sample_type in names, f"no sample type {sample_type!r} in {names}"
        return names.index(sample_type)


def decode_pprof(data: bytes) -> Pprof:
    g = group(data)
    strings = [v.decode("utf-8") for v in messages(g, 6)]

    def s(i: int) -> str:
        assert 0 <= i < len(strings), f"string index {i} out of range"
        return strings[i]

    def value_type(raw: bytes) -> tuple[str, str]:
        vg = group(raw)
        return s(scalar(vg, 1)), s(scalar(vg, 2))

    functions: dict[int, PprofFunction] = {}
    function_order: list[int] = []
    for raw in messages(g, 5):
        fg = group(raw)
        fn = PprofFunction(
            id=scalar(fg, 1),
            name=s(scalar(fg, 2)),
            system_name=s(scalar(fg, 3)),
            filename=s(scalar(fg, 4)),
            start_line=signed(scalar(fg, 5)),
        )
        function_order.append(fn.id)
        functions[fn.id] = fn
    mappings: dict[int, PprofMapping] = {}
    mapping_order: list[int] = []
    for raw in messages(g, 3):
        mg = group(raw)
        m = PprofMapping(id=scalar(mg, 1), filename=s(scalar(mg, 5)))
        mapping_order.append(m.id)
        mappings[m.id] = m
    locations: dict[int, PprofLocation] = {}
    location_order: list[int] = []
    for raw in messages(g, 4):
        lg = group(raw)
        lines = []
        for line_raw in messages(lg, 4):
            lng = group(line_raw)
            lines.append((scalar(lng, 1), signed(scalar(lng, 2))))
        loc = PprofLocation(
            id=scalar(lg, 1), mapping_id=scalar(lg, 2), address=scalar(lg, 3), lines=lines
        )
        location_order.append(loc.id)
        locations[loc.id] = loc
    samples = []
    for raw in messages(g, 2):
        sg = group(raw)
        labels: dict[str, list[Any]] = defaultdict(list)
        for label_raw in messages(sg, 3):
            lbg = group(label_raw)
            key = s(scalar(lbg, 1))
            if 2 in lbg and scalar(lbg, 2):
                labels[key].append(s(scalar(lbg, 2)))
            else:
                labels[key].append(signed(scalar(lbg, 3)))
        sample = PprofSample(
            location_ids=ints(sg.get(1, [])),
            values=[signed(v) for v in ints(sg.get(2, []))],
            labels=dict(labels),
        )
        for loc_id in sample.location_ids:
            loc = locations[loc_id]
            assert loc.lines, f"location {loc_id} has no line"
            fn_id, line = loc.lines[0]
            sample.stack.append(functions[fn_id].name)
            sample.lines.append(line)
        samples.append(sample)
    period_raw = messages(g, 11)
    return Pprof(
        strings=strings,
        sample_types=[value_type(raw) for raw in messages(g, 1)],
        samples=samples,
        functions=functions,
        locations=locations,
        mappings=mappings,
        location_order=location_order,
        function_order=function_order,
        mapping_order=mapping_order,
        time_nanos=signed(scalar(g, 9)),
        duration_nanos=signed(scalar(g, 10)),
        period_type=value_type(period_raw[-1]) if period_raw else None,
        period=signed(scalar(g, 12)),
        comments=[s(signed(i)) for i in ints(g.get(13, []))],
    )


# ------------------------------------------------------------------ OTLP


def decode_any_value(raw: bytes) -> Any:
    g = group(raw)
    if 1 in g:
        return scalar(g, 1).decode("utf-8")
    if 2 in g:
        return bool(scalar(g, 2))
    if 3 in g:
        return signed(scalar(g, 3))
    if 4 in g:
        return struct.unpack("<d", struct.pack("<Q", scalar(g, 4)))[0]
    if 5 in g:
        return [decode_any_value(v) for v in messages(group(scalar(g, 5)), 1)]
    if 6 in g:
        return dict(decode_key_value(kv) for kv in messages(group(scalar(g, 6)), 1))
    if 7 in g:
        return bytes(scalar(g, 7))
    return None


def decode_key_value(raw: bytes) -> tuple[str, Any]:
    g = group(raw)
    key = scalar(g, 1, b"").decode("utf-8")
    value_raw = messages(g, 2)
    return key, decode_any_value(value_raw[-1]) if value_raw else None


def value_depth(value: Any) -> int:
    if isinstance(value, list):
        return 1 + max((value_depth(v) for v in value), default=0)
    if isinstance(value, dict):
        return 1 + max((value_depth(v) for v in value.values()), default=0)
    return 0


@dataclass
class OtlpAttribute:
    key: str
    value: Any
    unit: str


@dataclass
class OtlpSample:
    stack_index: int
    values: list[int]
    attribute_indices: list[int]
    stack: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class OtlpProfile:
    sample_type: tuple[str, str]
    samples: list[OtlpSample]
    time_unix_nano: int
    duration_nano: int
    period_type: tuple[str, str] | None
    period: int
    profile_id: bytes
    attribute_indices: list[int]
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class OtlpDictionary:
    strings: list[str]
    mappings: list[bytes]
    locations: list[bytes]
    functions: list[bytes]
    stacks: list[bytes]
    attributes: list[OtlpAttribute]
    function_names: list[str]
    """Name of each function table entry (index 0 is the zero entry)."""


@dataclass
class OtlpRequest:
    resource: dict[str, Any]
    profiles: list[OtlpProfile]
    dictionary: OtlpDictionary
    resource_profiles_count: int

    def profile(self, sample_type: str) -> OtlpProfile:
        found = [p for p in self.profiles if p.sample_type[0] == sample_type]
        names = [p.sample_type for p in self.profiles]
        assert len(found) == 1, f"expected one {sample_type!r} profile, got {names}"
        return found[0]


def decode_otlp_profiles(data: bytes) -> OtlpRequest:
    g = group(data)
    dictionary_raw = messages(g, 2)
    dg = group(dictionary_raw[-1]) if dictionary_raw else {}
    strings = [v.decode("utf-8") for v in messages(dg, 5)]

    def s(i: int) -> str:
        assert 0 <= i < len(strings), f"string index {i} out of range"
        return strings[i]

    def value_type(raw: bytes) -> tuple[str, str]:
        vg = group(raw)
        return s(scalar(vg, 1)), s(scalar(vg, 2))

    attributes: list[OtlpAttribute] = []
    for raw in messages(dg, 6):
        ag = group(raw)
        value_raw = messages(ag, 2)
        attributes.append(
            OtlpAttribute(
                key=s(scalar(ag, 1)),
                value=decode_any_value(value_raw[-1]) if value_raw else None,
                unit=s(scalar(ag, 3)),
            )
        )
    functions = messages(dg, 3)
    function_names = [s(scalar(group(raw), 1)) for raw in functions]
    locations = messages(dg, 2)
    stacks = messages(dg, 7)

    def stack_names(index: int) -> list[str]:
        assert 0 <= index < len(stacks), f"stack index {index} out of range"
        names = []
        for loc_index in ints(group(stacks[index]).get(1, [])):
            assert 0 < loc_index < len(locations), f"location index {loc_index} out of range"
            lines = messages(group(locations[loc_index]), 3)
            assert lines, "location without lines"
            fn_index = scalar(group(lines[0]), 1)
            assert 0 < fn_index < len(function_names), f"function index {fn_index} out of range"
            names.append(function_names[fn_index])
        return names

    def resolve(indices: list[int]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for i in indices:
            assert 0 < i < len(attributes), f"attribute index {i} out of range"
            out[attributes[i].key] = attributes[i].value
        return out

    resource: dict[str, Any] = {}
    profiles: list[OtlpProfile] = []
    rps = messages(g, 1)
    for rp_raw in rps:
        rpg = group(rp_raw)
        for res_raw in messages(rpg, 1):
            for kv in messages(group(res_raw), 1):
                key, value = decode_key_value(kv)
                resource[key] = value
        for sp_raw in messages(rpg, 2):
            for p_raw in messages(group(sp_raw), 2):
                pg = group(p_raw)
                sample_types = messages(pg, 1)
                assert len(sample_types) == 1, "each OTLP profile has exactly one sample_type"
                samples = []
                for sraw in messages(pg, 2):
                    sg = group(sraw)
                    sample = OtlpSample(
                        stack_index=scalar(sg, 1),
                        values=[signed(v) for v in ints(sg.get(2, []))],
                        attribute_indices=ints(sg.get(3, [])),
                    )
                    sample.stack = stack_names(sample.stack_index)
                    sample.attributes = resolve(sample.attribute_indices)
                    samples.append(sample)
                period_raw = messages(pg, 5)
                profile = OtlpProfile(
                    sample_type=value_type(sample_types[0]),
                    samples=samples,
                    time_unix_nano=scalar(pg, 3),
                    duration_nano=scalar(pg, 4),
                    period_type=value_type(period_raw[-1]) if period_raw else None,
                    period=signed(scalar(pg, 6)),
                    profile_id=scalar(pg, 7, b""),
                    attribute_indices=ints(pg.get(11, [])),
                )
                profile.attributes = resolve(profile.attribute_indices)
                profiles.append(profile)
    return OtlpRequest(
        resource=resource,
        profiles=profiles,
        dictionary=OtlpDictionary(
            strings=strings,
            mappings=messages(dg, 1),
            locations=locations,
            functions=functions,
            stacks=stacks,
            attributes=attributes,
            function_names=function_names,
        ),
        resource_profiles_count=len(rps),
    )

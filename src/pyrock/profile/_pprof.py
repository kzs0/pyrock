"""The intermediate profile model and a ``profile.proto`` (pprof) encoder.

Collectors produce a :class:`Profile`; :func:`encode` writes pprof bytes (what
``go tool pprof`` and Pyroscope read) and :mod:`._otlp` converts the same model
to OTLP Profiles.

Encoding rules shared with :mod:`._otlp`: functions are deduplicated by
``(name, system_name, filename)``, locations by ``(function, line)``, and every
location points at one synthetic ``python`` mapping (Python frames have no
addresses; the mapping only tells tools that symbols are already resolved).
"""

from __future__ import annotations

import gzip
from collections.abc import Mapping
from dataclasses import dataclass, field

from pyrock.trace.otlp._pb import Writer

__all__ = [
    "MAPPING_NAME",
    "Frame",
    "Profile",
    "Sample",
    "SampleType",
    "StringTable",
    "encode",
    "encode_gzip",
    "system_name",
]

MAPPING_NAME = "python"
"""File name of the single synthetic mapping every location refers to."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One stack frame. ``filename`` is trimmed by the privacy policy before encoding."""

    function: str
    filename: str = ""
    line: int = 0
    module: str = ""


@dataclass(frozen=True, slots=True)
class SampleType:
    """A ``(type, unit)`` pair such as ``("cpu", "nanoseconds")``."""

    type: str
    unit: str


@dataclass(frozen=True, slots=True)
class Sample:
    """A stack (leaf first) with one value per sample type, plus labels."""

    stack: tuple[Frame, ...]
    values: tuple[int, ...]
    labels: Mapping[str, str | int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Profile:
    """A collected profile."""

    sample_types: tuple[SampleType, ...]
    samples: tuple[Sample, ...]
    start_time_ns: int
    duration_ns: int
    period_type: SampleType | None = None
    period: int = 0
    comments: tuple[str, ...] = ()
    attrs: Mapping[str, str] = field(default_factory=dict)
    """Profile-level attributes such as ``profile.note``."""


def system_name(frame: Frame, /) -> str:
    """The module-qualified function name (``Function.system_name``)."""
    return f"{frame.module}.{frame.function}" if frame.module else frame.function


class StringTable:
    """A deduplicated string table whose index 0 is ``""``."""

    __slots__ = ("_index", "strings")

    def __init__(self) -> None:
        self.strings: list[str] = [""]
        self._index: dict[str, int] = {"": 0}

    def __call__(self, text: str, /) -> int:
        """The index of ``text``, adding it if new."""
        index = self._index.get(text)
        if index is None:
            index = self._index[text] = len(self.strings)
            self.strings.append(text)
        return index


class _Dictionary:
    """pprof's function/location tables; ids start at 1 (0 means "none")."""

    __slots__ = ("functions", "locations", "strings")

    def __init__(self, strings: StringTable) -> None:
        self.strings = strings
        self.functions: dict[tuple[str, str, str], int] = {}
        self.locations: dict[tuple[int, int], int] = {}

    def location(self, frame: Frame) -> int:
        fn_key = (frame.function, system_name(frame), frame.filename)
        fn_id = self.functions.setdefault(fn_key, len(self.functions) + 1)
        return self.locations.setdefault((fn_id, frame.line), len(self.locations) + 1)


def _value_type(strings: StringTable, sample_type: SampleType) -> Writer:
    w = Writer()
    w.varint(1, strings(sample_type.type))
    w.varint(2, strings(sample_type.unit))
    return w


def _label(strings: StringTable, key: str, value: str | int) -> Writer:
    w = Writer()
    w.varint(1, strings(key))
    if isinstance(value, str):
        w.varint(2, strings(value))
    else:
        w.varint(3, int(value))
    return w


def _sample(dictionary: _Dictionary, sample: Sample) -> Writer:
    w = Writer()
    w.packed_varints(1, [dictionary.location(frame) for frame in sample.stack])
    w.packed_varints(2, sample.values)
    for key, value in sample.labels.items():
        w.message(3, _label(dictionary.strings, key, value))
    return w


def _mapping(strings: StringTable) -> Writer:
    w = Writer()
    w.varint(1, 1)
    w.varint(5, strings(MAPPING_NAME))
    for flag in (7, 8, 9):  # has_functions, has_filenames, has_line_numbers
        w.bool_(flag, True)
    return w


def _location(location_id: int, function_id: int, line: int) -> Writer:
    w = Writer()
    w.varint(1, location_id)
    w.varint(2, 1)  # the synthetic mapping
    line_w = Writer()
    line_w.varint(1, function_id)
    line_w.varint(2, line)
    w.message(4, line_w)
    return w


def _function(strings: StringTable, function_id: int, key: tuple[str, str, str]) -> Writer:
    name, system, filename = key
    w = Writer()
    w.varint(1, function_id)
    w.varint(2, strings(name))
    w.varint(3, strings(system))
    w.varint(4, strings(filename))
    return w


def encode(profile: Profile, /) -> bytes:
    """Encode ``profile`` as uncompressed ``perftools.profiles.Profile`` bytes.

    Profile-level ``attrs`` are appended to the comments as ``key=value``.
    """
    strings = StringTable()
    dictionary = _Dictionary(strings)
    out = Writer()
    for sample_type in profile.sample_types:
        out.message(1, _value_type(strings, sample_type))
    for sample in profile.samples:
        out.message(2, _sample(dictionary, sample))
    out.message(3, _mapping(strings))
    for (function_id, line), location_id in dictionary.locations.items():
        out.message(4, _location(location_id, function_id, line))
    for key, function_id in dictionary.functions.items():
        out.message(5, _function(strings, function_id, key))
    out.varint(9, profile.start_time_ns)
    out.varint(10, profile.duration_ns)
    if profile.period_type is not None:
        out.message(11, _value_type(strings, profile.period_type))
    out.varint(12, profile.period)
    comments = [*profile.comments, *(f"{k}={v}" for k, v in profile.attrs.items())]
    if comments:
        out.packed_varints(13, [strings(c) for c in comments])
    # The string table goes last: every other field has interned its strings by now.
    tail = Writer()
    for text in strings.strings:
        tail.string(6, text)
    return out.getvalue() + tail.getvalue()


def encode_gzip(profile: Profile, /) -> bytes:
    """Encode and gzip (the format served by ``/debug/pprof``)."""
    return gzip.compress(encode(profile), compresslevel=6, mtime=0)

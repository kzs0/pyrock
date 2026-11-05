"""The intermediate profile model and a ``profile.proto`` (pprof) encoder.

Collectors produce a :class:`Profile`; :func:`encode` writes pprof bytes (what
``go tool pprof`` and Pyroscope read) and :mod:`._otlp` converts the same model
to OTLP Profiles.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["Frame", "Profile", "Sample", "SampleType", "encode", "encode_gzip"]


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


# --- scratch reference (appended to the stub module for test validation only)
import gzip as _gzip
import sys as _sys
_sys.path.insert(0, "/tmp/claude-0/-home-user/b67cdd0e-119e-51ba-9ac2-956a9ab27bf3/scratchpad/u4ref")
import pb as _pb

def encode(profile, /):
    strings = {"": 0}
    def s(x):
        if x not in strings: strings[x] = len(strings)
        return strings[x]
    funcs, locs, maps = {}, {}, {}
    out = b""
    for st in profile.sample_types:
        out += _pb.lfield(1, _pb.vfield(1, s(st.type)) + _pb.vfield(2, s(st.unit)))
    body_samples = b""
    for smp in profile.samples:
        ids = []
        for fr in smp.stack:
            fk = (fr.function, fr.filename)
            if fk not in funcs: funcs[fk] = len(funcs) + 1
            if fr.filename not in maps: maps[fr.filename] = len(maps) + 1
            lk = (fk, fr.line)
            if lk not in locs: locs[lk] = len(locs) + 1
            ids.append(locs[lk])
        lab = b""
        for k, v in smp.labels.items():
            if isinstance(v, int):
                lab += _pb.lfield(3, _pb.vfield(1, s(k)) + _pb.vfield(3, v))
            else:
                lab += _pb.lfield(3, _pb.vfield(1, s(k)) + _pb.vfield(2, s(v)))
        body_samples += _pb.lfield(2, _pb.packed(1, ids) + _pb.packed(2, list(smp.values)) + lab)
    out += body_samples
    for fn, i in maps.items():
        out += _pb.lfield(3, _pb.vfield(1, i) + _pb.vfield(5, s(fn)))
    for (fk, line), i in locs.items():
        out += _pb.lfield(4, _pb.vfield(1, i) + _pb.vfield(2, maps[fk[1]]) + _pb.lfield(4, _pb.vfield(1, funcs[fk]) + _pb.vfield(2, line)))
    for (name, fn), i in funcs.items():
        out += _pb.lfield(5, _pb.vfield(1, i) + _pb.vfield(2, s(name)) + _pb.vfield(3, s(name)) + _pb.vfield(4, s(fn)))
    tail = _pb.vfield(9, profile.start_time_ns) + _pb.vfield(10, profile.duration_ns)
    if profile.period_type:
        tail += _pb.lfield(11, _pb.vfield(1, s(profile.period_type.type)) + _pb.vfield(2, s(profile.period_type.unit)))
    tail += _pb.vfield(12, profile.period)
    tail += _pb.packed(13, [s(c) for c in profile.comments])
    for x in list(strings):
        out += _pb.lfield(6, x.encode())
    return out + tail

def encode_gzip(profile, /):
    return _gzip.compress(encode(profile), mtime=0)

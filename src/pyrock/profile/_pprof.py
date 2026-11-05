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


def encode(profile: Profile, /) -> bytes:
    """Encode ``profile`` as uncompressed ``perftools.profiles.Profile`` bytes."""
    raise NotImplementedError


def encode_gzip(profile: Profile, /) -> bytes:
    """Encode and gzip (the format served by ``/debug/pprof``)."""
    raise NotImplementedError

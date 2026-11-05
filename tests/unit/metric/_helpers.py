"""Helpers that read a registry snapshot (``Registry.collect``) without exposition."""

from __future__ import annotations

from collections.abc import Mapping

from pyrock.metric import MetricFamily, Registry, Sample


def family(registry: Registry, name: str) -> MetricFamily | None:
    """The collected family called ``name`` (full name), or ``None``."""
    for fam in registry.collect():
        if fam.name == name:
            return fam
    return None


def samples(registry: Registry, name: str) -> list[Sample]:
    fam = family(registry, name)
    return [] if fam is None else list(fam.samples)


def sample(registry: Registry, name: str, labels: Mapping[str, str] | None = None) -> Sample:
    """The one sample of ``name`` whose labels include ``labels``."""
    wanted = dict(labels or {})
    found = [
        s for s in samples(registry, name) if all(s.labels.get(k) == v for k, v in wanted.items())
    ]
    assert len(found) == 1, f"{name}{wanted}: expected one sample, got {found!r}"
    return found[0]


def value(registry: Registry, name: str, labels: Mapping[str, str] | None = None) -> float:
    return sample(registry, name, labels).value

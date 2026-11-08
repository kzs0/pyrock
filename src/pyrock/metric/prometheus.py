"""Prometheus text exposition format 0.0.4.

:func:`encode` validates the **whole snapshot first** (bucket bounds finite and
increasing, cumulative counts monotonic and not above the count, names and
label names valid) and only then writes text. A validation failure raises
:class:`ExpositionError`, and ``/metrics`` answers 500 with no partial body.

Sample values and bucket bounds use Go's ``%g`` formatting (``strconv``
``FormatFloat(v, 'g', -1, 64)``): ``1``, ``0.5``, ``1e+06``, ``+Inf``, ``NaN``.
Metric family names turn dots into underscores (``http.request`` ->
``http_request_count``).

Families are written sorted by name; samples and their labels are written in
the order the snapshot holds them (:meth:`Registry.collect` sorts series by
label values and labels by name). Families without samples are skipped, and
are not validated, as in Go.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping

from pyrock.metric import MetricFamily, MetricType, Registry, Sample

__all__ = [
    "CONTENT_TYPE",
    "ExpositionError",
    "encode",
    "escape_help",
    "escape_label_value",
    "validate",
]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")
_SHORTEST_EXPONENT_LIMIT = 6
"""Go's ``%g`` (shortest) switches to exponent form at 1e6 and below 1e-4."""


class ExpositionError(ValueError):
    """The snapshot cannot be encoded as valid exposition text."""


# ------------------------------------------------------------------- validation


def validate(families: Iterable[MetricFamily], /) -> None:
    """Raise :class:`ExpositionError` if any family is invalid."""
    owners: dict[str, str] = {}
    for fam in families:
        if not fam.samples:
            continue
        _validate_family(fam)
        names = [fam.name]
        if fam.type is MetricType.HISTOGRAM:
            names.extend(fam.name + suffix for suffix in _HISTOGRAM_SUFFIXES)
        for name in names:
            if name in owners:
                raise ExpositionError(
                    f"metric name {name!r} of family {fam.name!r} collides with {owners[name]!r}"
                )
            owners[name] = fam.name


def _validate_family(fam: MetricFamily, /) -> None:
    if not _METRIC_NAME.fullmatch(fam.name):
        raise ExpositionError(f"invalid metric name {fam.name!r}")
    if not _is_utf8(fam.help):
        raise ExpositionError(f"help of {fam.name!r} is not valid UTF-8")
    if not isinstance(fam.type, MetricType):
        raise ExpositionError(f"{fam.name!r} has invalid type {fam.type!r}")
    histogram = fam.type is MetricType.HISTOGRAM
    for index, sample in enumerate(fam.samples):
        _validate_labels(fam.name, sample.labels, histogram=histogram)
        if histogram:
            _validate_buckets(fam.name, index, sample)


def _validate_labels(name: str, labels: Mapping[str, str], /, *, histogram: bool) -> None:
    for key, value in labels.items():
        if not _LABEL_NAME.fullmatch(key) or key.startswith("__"):
            raise ExpositionError(f"invalid label name {key!r} for {name!r}")
        if histogram and key == "le":
            raise ExpositionError(f"histogram {name!r} uses the reserved label 'le'")
        if not _is_utf8(value):
            raise ExpositionError(f"label {key!r} of {name!r} is not valid UTF-8")


def _validate_buckets(name: str, index: int, sample: Sample, /) -> None:
    previous_bound = -math.inf
    previous_count = 0
    for bucket in sample.buckets:
        bound, count = bucket.upper_bound, bucket.count
        if not math.isfinite(bound):
            raise ExpositionError(f"{name!r} sample {index} has non-finite bucket bound {bound}")
        if bound <= previous_bound:
            raise ExpositionError(f"{name!r} sample {index} bucket bounds are not increasing")
        if count < previous_count:
            raise ExpositionError(f"{name!r} sample {index} bucket counts decrease")
        if count > sample.count:
            raise ExpositionError(f"{name!r} sample {index} bucket count exceeds total count")
        previous_bound, previous_count = bound, count


def _is_utf8(text: str, /) -> bool:
    """False for strings with lone surrogates, which have no UTF-8 encoding."""
    if text.isascii():
        return True
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# --------------------------------------------------------------------- escaping


def escape_help(text: str, /) -> str:
    """Escape ``\\`` and newlines in HELP text."""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def escape_label_value(value: str, /) -> str:
    """Escape ``\\``, ``"`` and newlines in a label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ---------------------------------------------------------------------- numbers


def _format_float(value: float, /) -> str:
    """Go's ``strconv.FormatFloat(value, 'g', -1, 64)``, with Prometheus infinities."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    sign = "-" if math.copysign(1.0, value) < 0 else ""
    digits, exponent = _shortest_digits(abs(value))
    if digits == "0":
        return sign + "0"
    if exponent < -4 or exponent >= _SHORTEST_EXPONENT_LIMIT:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        return f"{sign}{mantissa}e{'-' if exponent < 0 else '+'}{abs(exponent):02d}"
    if exponent < 0:
        return f"{sign}0.{'0' * (-exponent - 1)}{digits}"
    whole = digits[: exponent + 1].ljust(exponent + 1, "0")
    fraction = digits[exponent + 1 :]
    return f"{sign}{whole}.{fraction}" if fraction else sign + whole


def _shortest_digits(value: float, /) -> tuple[str, int]:
    """Shortest round-trip significant digits of ``value >= 0`` and its decimal exponent.

    ``repr`` already yields the shortest round-trip form; this only re-shapes it
    (``1.5e-07`` -> ``("15", -7)``, ``123456.0`` -> ``("123456", 5)``).
    """
    mantissa, _, exp_text = repr(float(value)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = whole + fraction
    exponent = int(exp_text or 0) + len(whole) - 1
    significant = digits.lstrip("0")
    exponent -= len(digits) - len(significant)
    return significant.rstrip("0") or "0", exponent


# --------------------------------------------------------------------- encoding


def encode(source: Registry | Iterable[MetricFamily], /) -> bytes:
    """Return UTF-8 exposition text for a registry (collected now) or a snapshot."""
    families = source.collect() if isinstance(source, Registry) else list(source)
    validate(families)
    lines: list[str] = []
    for fam in sorted(families, key=lambda f: f.name):
        if fam.samples:
            _write_family(lines, fam)
    return "".join(lines).encode("utf-8")


def _write_family(lines: list[str], fam: MetricFamily, /) -> None:
    if fam.help:
        lines.append(f"# HELP {fam.name} {escape_help(fam.help)}\n")
    lines.append(f"# TYPE {fam.name} {fam.type.value}\n")
    if fam.type is MetricType.HISTOGRAM:
        for sample in fam.samples:
            _write_histogram(lines, fam.name, sample)
    else:
        for sample in fam.samples:
            labels = _label_text(sample.labels)
            lines.append(f"{fam.name}{_braced(labels)} {_format_float(sample.value)}\n")


def _write_histogram(lines: list[str], name: str, sample: Sample, /) -> None:
    labels = _label_text(sample.labels)
    bucket_prefix = f"{name}_bucket{{{labels}{',' if labels else ''}le="
    for bucket in sample.buckets:
        bound, count = _format_float(bucket.upper_bound), _format_float(bucket.count)
        lines.append(f'{bucket_prefix}"{bound}"}} {count}\n')
    count = _format_float(sample.count)
    lines.append(f'{bucket_prefix}"+Inf"}} {count}\n')
    lines.append(f"{name}_sum{_braced(labels)} {_format_float(sample.sum)}\n")
    lines.append(f"{name}_count{_braced(labels)} {count}\n")


def _label_text(labels: Mapping[str, str], /) -> str:
    return ",".join(f'{key}="{escape_label_value(value)}"' for key, value in labels.items())


def _braced(labels: str, /) -> str:
    return f"{{{labels}}}" if labels else ""

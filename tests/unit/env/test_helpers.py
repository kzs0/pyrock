"""``env.parse_duration`` (Go duration grammar), ``env.parse_bool``, ``Env`` and ``ParseError``."""

from __future__ import annotations

import dataclasses
import random

import pytest

from pyrock.env import Env, ParseError, parse_bool, parse_duration

# ----------------------------------------------------------- parse_duration


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("0", 0.0),
        ("0s", 0.0),
        ("10s", 10.0),
        ("500ms", 0.5),
        ("1m30s", 90.0),
        ("1h", 3600.0),
        ("2h45m", 9900.0),
        ("1h2m3s", 3723.0),
        ("1h2m3.5s", 3723.5),
        ("1.5s", 1.5),
        (".5s", 0.5),
        ("1.s", 1.0),
        ("250us", 0.00025),
        ("250µs", 0.00025),
        ("250μs", 0.00025),
        ("100ns", 1e-7),
        ("1m0.5s", 60.5),
        ("1.5h", 5400.0),
        ("10ms500us", 0.0105),
        ("3s2m", 123.0),
    ],
)
def test_go_duration_syntax(raw: str, seconds: float) -> None:
    out = parse_duration(raw)
    assert isinstance(out, float)
    assert out == pytest.approx(seconds, rel=1e-12, abs=1e-15)


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [("2", 2.0), ("0.75", 0.75), ("30", 30.0), ("1.5", 1.5), ("0.0", 0.0), ("100", 100.0)],
)
def test_bare_number_is_seconds(raw: str, seconds: float) -> None:
    out = parse_duration(raw)
    assert isinstance(out, float)
    assert out == pytest.approx(seconds)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "s",
        "ms",
        ".s",
        "1x",
        "10 seconds",
        "ten seconds",
        "1.2.3s",
        "abc",
        "1s2",
        "1d",
        "1S",
        "1H",
        "h1",
        "--1s",
        "1e3s",
        "nan",
        "inf",
        "-inf",
    ],
)
def test_malformed_durations_raise_value_error(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - only the exception type is contractual
        parse_duration(raw)


def test_duration_composition_property() -> None:
    """Random Go-style compositions equal the sum of their parts."""
    units = [("h", 3600.0), ("m", 60.0), ("s", 1.0), ("ms", 1e-3), ("us", 1e-6), ("ns", 1e-9)]
    rng = random.Random(1337)
    for _ in range(400):
        chosen = sorted(rng.sample(range(len(units)), rng.randint(1, len(units))))
        text = ""
        expected = 0.0
        for index in chosen:
            unit, scale = units[index]
            if rng.random() < 0.3:
                amount = rng.randint(0, 999) + rng.randint(0, 99) / 100
                text += f"{amount:.2f}{unit}"
            else:
                amount = rng.randint(0, 999)
                text += f"{amount}{unit}"
            expected += amount * scale
        assert parse_duration(text) == pytest.approx(expected, rel=1e-9, abs=1e-12), text


# --------------------------------------------------------------- parse_bool


@pytest.mark.parametrize(
    "raw", ["1", "true", "True", "TRUE", "tRuE", "yes", "YES", "Yes", "on", "ON", "On"]
)
def test_true_forms(raw: str) -> None:
    assert parse_bool(raw) is True


@pytest.mark.parametrize(
    "raw", ["0", "false", "False", "FALSE", "no", "NO", "No", "off", "OFF", "Off"]
)
def test_false_forms(raw: str) -> None:
    assert parse_bool(raw) is False


@pytest.mark.parametrize("raw", ["", "2", "-1", "maybe", "perhaps", "t", "f", "y", "n", "truthy"])
def test_invalid_bool_raises_value_error(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - only the exception type is contractual
        parse_bool(raw)


# ---------------------------------------------------------------------- Env


def test_env_marker_defaults() -> None:
    marker = Env("PORT")
    assert marker.name == "PORT"
    assert marker.required is False
    assert marker.sep == ","


def test_env_marker_is_frozen() -> None:
    marker = Env("PORT")
    with pytest.raises(dataclasses.FrozenInstanceError):
        marker.name = "OTHER"  # type: ignore[misc]


def test_env_marker_requires_name() -> None:
    with pytest.raises(ValueError, match="name"):
        Env("")


def test_env_marker_requires_separator() -> None:
    with pytest.raises(ValueError, match="sep"):
        Env("X", sep="")


def test_env_marker_equality() -> None:
    assert Env("A", required=True, sep=";") == Env("A", required=True, sep=";")
    assert Env("A") != Env("B")


# --------------------------------------------------------------- ParseError


def test_parse_error_keeps_every_problem() -> None:
    err = ParseError(["PORT: invalid int", "DEBUG: invalid bool"])
    assert err.problems == ["PORT: invalid int", "DEBUG: invalid bool"]
    assert "PORT" in str(err)
    assert "DEBUG" in str(err)
    assert isinstance(err, ValueError)


def test_parse_error_accepts_any_iterable() -> None:
    err = ParseError(p for p in ("a", "b"))
    assert err.problems == ["a", "b"]


def test_parse_error_with_no_problems_has_message() -> None:
    assert str(ParseError([]))

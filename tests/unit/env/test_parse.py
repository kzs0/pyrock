"""``env.parse``: typed dataclass parsing from an environment mapping (DESIGN §6.2).

Classes live at module level so their string annotations (``from __future__
import annotations``) resolve.
"""

from __future__ import annotations

import datetime
import enum
import types
from dataclasses import dataclass, field
from typing import Annotated, Literal, Optional

import pytest

import pyrock
from pyrock.env import Env, ParseError, parse


class Color(enum.Enum):
    RED = "red"
    DARK_BLUE = "blue"


@dataclass
class Scalars:
    name: Annotated[str, Env("NAME")] = "default-name"
    port: Annotated[int, Env("PORT")] = 8080
    ratio: Annotated[float, Env("RATIO")] = 0.5
    debug: Annotated[bool, Env("DEBUG")] = False
    timeout: Annotated[datetime.timedelta, Env("TIMEOUT")] = datetime.timedelta(seconds=5)


@dataclass
class Collections:
    hosts: Annotated[list[str], Env("HOSTS")] = field(default_factory=list)
    ports: Annotated[tuple[int, ...], Env("PORTS", sep=";")] = ()
    weights: Annotated[list[float], Env("WEIGHTS", sep="|")] = field(default_factory=list)
    flags: Annotated[list[bool], Env("FLAGS")] = field(default_factory=list)
    names: Annotated[tuple[str, ...], Env("NAMES", sep=" ")] = ("x",)


@dataclass
class Choices:
    mode: Annotated[Literal["dev", "prod"], Env("MODE")] = "dev"
    color: Annotated[Color, Env("COLOR")] = Color.RED


@dataclass
class Optionals:
    region: Annotated[str | None, Env("REGION")] = None
    retries: Annotated[Optional[int], Env("RETRIES")] = None  # noqa: UP045 - both spellings
    grace: Annotated[datetime.timedelta | None, Env("GRACE")] = None


@dataclass
class Inner:
    url: Annotated[str, Env("DB_URL", required=True)] = ""
    pool: Annotated[int, Env("DB_POOL")] = 5


@dataclass
class Middle:
    inner: Inner = field(default_factory=Inner)
    label: Annotated[str, Env("MIDDLE_LABEL")] = "m"


@dataclass
class Outer:
    port: Annotated[int, Env("PORT")] = 80
    middle: Middle = field(default_factory=Middle)


@dataclass
class Required:
    token: Annotated[str, Env("TOKEN", required=True)] = ""
    secret: Annotated[str, Env("SECRET", required=True)] = ""


@dataclass
class RequiredNoDefault:
    token: Annotated[str, Env("TOKEN", required=True)]
    port: Annotated[int, Env("PORT", required=True)]


@dataclass
class Plain:
    """Fields without ``Env`` metadata keep their defaults."""

    untouched: str = "keep"
    count: int = 3
    items: list[int] = field(default_factory=lambda: [1, 2])
    port: Annotated[int, Env("PORT")] = 1


@dataclass
class ExtraMetadata:
    port: Annotated[int, "a doc string", Env("PORT")] = 1


@dataclass
class WithBedrock:
    port: Annotated[int, Env("PORT")] = 8080
    bedrock: pyrock.Config = field(default_factory=pyrock.Config.default)


def _p(cls: type, environ: dict[str, str]) -> object:
    return parse(cls, environ=environ)


def _problems(cls: type, environ: dict[str, str]) -> list[str]:
    with pytest.raises(ParseError) as info:
        parse(cls, environ=environ)
    return info.value.problems


# ------------------------------------------------------------------ scalars


def test_unset_keeps_defaults() -> None:
    assert _p(Scalars, {}) == Scalars()


def test_scalars_are_parsed() -> None:
    cfg = _p(
        Scalars,
        {"NAME": "svc", "PORT": "9000", "RATIO": "0.125", "DEBUG": "yes", "TIMEOUT": "1m30s"},
    )
    assert cfg == Scalars(
        name="svc",
        port=9000,
        ratio=0.125,
        debug=True,
        timeout=datetime.timedelta(seconds=90),
    )


def test_int_and_float_types() -> None:
    cfg = _p(Scalars, {"PORT": "-3", "RATIO": "2"})
    assert isinstance(cfg, Scalars)
    assert type(cfg.port) is int
    assert cfg.port == -3
    assert type(cfg.ratio) is float
    assert cfg.ratio == 2.0


def test_float_scientific_notation() -> None:
    cfg = _p(Scalars, {"RATIO": "1e-3"})
    assert isinstance(cfg, Scalars)
    assert cfg.ratio == pytest.approx(0.001)


@pytest.mark.parametrize(("raw", "expected"), [("on", True), ("OFF", False), ("1", True)])
def test_bool_forms(raw: str, expected: bool) -> None:
    cfg = _p(Scalars, {"DEBUG": raw})
    assert isinstance(cfg, Scalars)
    assert cfg.debug is expected


@pytest.mark.parametrize(
    ("raw", "delta"),
    [
        ("10s", datetime.timedelta(seconds=10)),
        ("500ms", datetime.timedelta(milliseconds=500)),
        ("1h2m3.5s", datetime.timedelta(hours=1, minutes=2, seconds=3.5)),
        ("2", datetime.timedelta(seconds=2)),
        ("0", datetime.timedelta(0)),
    ],
)
def test_timedelta_uses_go_syntax(raw: str, delta: datetime.timedelta) -> None:
    cfg = _p(Scalars, {"TIMEOUT": raw})
    assert isinstance(cfg, Scalars)
    assert isinstance(cfg.timeout, datetime.timedelta)
    assert cfg.timeout == delta


def test_string_is_taken_verbatim() -> None:
    cfg = _p(Scalars, {"NAME": "  spaced value, with=signs  "})
    assert isinstance(cfg, Scalars)
    assert cfg.name == "  spaced value, with=signs  "


def test_empty_value_is_unset() -> None:
    assert _p(Scalars, {"NAME": "", "PORT": "", "DEBUG": "", "TIMEOUT": ""}) == Scalars()


@pytest.mark.parametrize(
    ("var", "raw"),
    [
        ("PORT", "eighty"),
        ("PORT", "1.5"),
        ("RATIO", "half"),
        ("DEBUG", "perhaps"),
        ("TIMEOUT", "forever"),
    ],
)
def test_invalid_scalar_names_the_variable(var: str, raw: str) -> None:
    problems = _problems(Scalars, {var: raw})
    assert len(problems) == 1
    assert var in problems[0]


def test_errors_are_aggregated() -> None:
    problems = _problems(
        Scalars, {"PORT": "x", "RATIO": "y", "DEBUG": "z", "TIMEOUT": "w", "NAME": "fine"}
    )
    assert len(problems) == 4
    joined = " ".join(problems)
    for var in ("PORT", "RATIO", "DEBUG", "TIMEOUT"):
        assert var in joined
    assert "NAME" not in joined


def test_parse_error_message_contains_every_problem() -> None:
    with pytest.raises(ParseError) as info:
        parse(Scalars, environ={"PORT": "x", "DEBUG": "z"})
    assert "PORT" in str(info.value)
    assert "DEBUG" in str(info.value)


def test_annotated_with_extra_metadata_finds_env() -> None:
    assert _p(ExtraMetadata, {"PORT": "7"}) == ExtraMetadata(port=7)


def test_fields_without_env_keep_defaults() -> None:
    cfg = _p(Plain, {"PORT": "2", "UNTOUCHED": "nope", "COUNT": "9"})
    assert cfg == Plain(untouched="keep", count=3, items=[1, 2], port=2)


# -------------------------------------------------------------- collections


def test_list_split_and_stripped() -> None:
    cfg = _p(Collections, {"HOSTS": " a.example , b.example,c "})
    assert isinstance(cfg, Collections)
    assert cfg.hosts == ["a.example", "b.example", "c"]


def test_tuple_of_int_with_custom_separator() -> None:
    cfg = _p(Collections, {"PORTS": "80; 443 ;8080"})
    assert isinstance(cfg, Collections)
    assert cfg.ports == (80, 443, 8080)
    assert isinstance(cfg.ports, tuple)


def test_float_and_bool_lists() -> None:
    cfg = _p(Collections, {"WEIGHTS": "0.5|1|2.25", "FLAGS": "true,off,1"})
    assert isinstance(cfg, Collections)
    assert cfg.weights == [0.5, 1.0, 2.25]
    assert cfg.flags == [True, False, True]


def test_single_item_list() -> None:
    cfg = _p(Collections, {"HOSTS": "only"})
    assert isinstance(cfg, Collections)
    assert cfg.hosts == ["only"]


def test_space_separator() -> None:
    cfg = _p(Collections, {"NAMES": "a b c"})
    assert isinstance(cfg, Collections)
    assert cfg.names == ("a", "b", "c")


def test_empty_list_value_keeps_default() -> None:
    cfg = _p(Collections, {"HOSTS": "", "NAMES": ""})
    assert cfg == Collections()


def test_invalid_list_element_names_the_variable() -> None:
    problems = _problems(Collections, {"PORTS": "80;http;443"})
    assert any("PORTS" in p for p in problems)


def test_default_factory_values_are_not_shared() -> None:
    a = _p(Collections, {})
    b = _p(Collections, {})
    assert isinstance(a, Collections)
    assert isinstance(b, Collections)
    assert a.hosts == b.hosts == []
    assert a.hosts is not b.hosts


# ------------------------------------------------------- Literal and Enum


def test_literal_member() -> None:
    assert _p(Choices, {"MODE": "prod"}) == Choices(mode="prod")


def test_literal_rejects_non_member() -> None:
    problems = _problems(Choices, {"MODE": "staging"})
    assert any("MODE" in p for p in problems)


def test_enum_by_value() -> None:
    cfg = _p(Choices, {"COLOR": "blue"})
    assert isinstance(cfg, Choices)
    assert cfg.color is Color.DARK_BLUE


def test_enum_by_name() -> None:
    cfg = _p(Choices, {"COLOR": "DARK_BLUE"})
    assert isinstance(cfg, Choices)
    assert cfg.color is Color.DARK_BLUE


def test_enum_rejects_unknown() -> None:
    problems = _problems(Choices, {"COLOR": "green"})
    assert any("COLOR" in p for p in problems)


# ----------------------------------------------------------------- Optional


def test_optional_unset_is_default() -> None:
    assert _p(Optionals, {}) == Optionals()


def test_optional_values() -> None:
    cfg = _p(Optionals, {"REGION": "eu-west-1", "RETRIES": "3", "GRACE": "250ms"})
    assert cfg == Optionals(
        region="eu-west-1", retries=3, grace=datetime.timedelta(milliseconds=250)
    )


def test_optional_empty_is_unset() -> None:
    assert _p(Optionals, {"REGION": "", "RETRIES": ""}) == Optionals()


def test_optional_invalid_is_error() -> None:
    problems = _problems(Optionals, {"RETRIES": "many"})
    assert any("RETRIES" in p for p in problems)


# ------------------------------------------------------- nested dataclasses


def test_nested_dataclasses_are_parsed_recursively() -> None:
    cfg = _p(Outer, {"PORT": "81", "DB_URL": "postgres://x", "DB_POOL": "9", "MIDDLE_LABEL": "q"})
    assert cfg == Outer(port=81, middle=Middle(inner=Inner(url="postgres://x", pool=9), label="q"))


def test_nested_required_missing_is_reported() -> None:
    problems = _problems(Outer, {})
    assert any("DB_URL" in p for p in problems)


def test_nested_and_outer_errors_are_aggregated() -> None:
    problems = _problems(Outer, {"PORT": "bad", "DB_POOL": "worse"})
    joined = " ".join(problems)
    for var in ("PORT", "DB_POOL", "DB_URL"):
        assert var in joined
    assert len(problems) >= 3


def test_nested_bedrock_config_reads_the_given_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_SERVICE", "leaked-from-os-environ")
    cfg = _p(WithBedrock, {"BEDROCK_SERVICE": "from-mapping"})
    assert isinstance(cfg, WithBedrock)
    assert cfg.bedrock.service == "from-mapping"


def test_nested_bedrock_config_ignores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_SERVICE", "leaked-from-os-environ")
    cfg = _p(WithBedrock, {})
    assert isinstance(cfg, WithBedrock)
    assert cfg.bedrock.service == "unknown"


# ----------------------------------------------------------------- required


def test_required_missing_lists_every_variable() -> None:
    problems = _problems(Required, {})
    joined = " ".join(problems)
    assert "TOKEN" in joined
    assert "SECRET" in joined
    assert len(problems) == 2


def test_required_empty_is_missing() -> None:
    problems = _problems(Required, {"TOKEN": "", "SECRET": "s"})
    assert len(problems) == 1
    assert "TOKEN" in problems[0]


def test_required_present() -> None:
    assert _p(Required, {"TOKEN": "t", "SECRET": "s"}) == Required(token="t", secret="s")


def test_required_without_dataclass_default() -> None:
    assert _p(RequiredNoDefault, {"TOKEN": "t", "PORT": "1"}) == RequiredNoDefault(
        token="t", port=1
    )


def test_required_without_default_missing_is_parse_error() -> None:
    problems = _problems(RequiredNoDefault, {"PORT": "nope"})
    joined = " ".join(problems)
    assert "TOKEN" in joined
    assert "PORT" in joined


# ---------------------------------------------------------- environ mapping


def test_explicit_environ_ignores_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "1234")
    monkeypatch.setenv("NAME", "from-os")
    assert _p(Scalars, {}) == Scalars()


def test_default_environ_is_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "4321")
    monkeypatch.delenv("NAME", raising=False)
    cfg = parse(Scalars)
    assert cfg.port == 4321


def test_read_only_mapping_is_accepted_and_not_mutated() -> None:
    source = {"PORT": "5", "UNRELATED": "x"}
    proxy = types.MappingProxyType(source)
    cfg = parse(Scalars, environ=proxy)
    assert cfg.port == 5
    assert source == {"PORT": "5", "UNRELATED": "x"}


def test_parse_is_deterministic() -> None:
    environ = {"PORT": "9", "HOSTS": "a,b", "MODE": "prod"}
    assert _p(Scalars, environ) == _p(Scalars, environ)
    assert _p(Collections, environ) == _p(Collections, environ)


def test_non_dataclass_is_type_error() -> None:
    class NotADataclass:
        port: int = 1

    with pytest.raises(TypeError):
        parse(NotADataclass, environ={})

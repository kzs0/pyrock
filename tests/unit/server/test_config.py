"""``pyrock.server.Config``: secure defaults and validation."""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

import pytest

from pyrock import server


def test_defaults_match_go() -> None:
    cfg = server.Config()
    assert cfg.addr == server.DEFAULT_ADDR == "127.0.0.1:9090"
    assert cfg.metrics is True
    assert cfg.pprof is False
    assert cfg.profile_handler is None
    assert cfg.read_timeout == 10.0
    assert cfg.read_header_timeout == 5.0
    assert cfg.write_timeout == 30.0
    assert cfg.idle_timeout == 120.0
    assert cfg.max_header_bytes == 1 << 20
    assert cfg.shutdown_timeout == 30.0
    assert cfg.max_connections == 64


def test_default_address_is_loopback_only() -> None:
    host, _, port = server.Config().addr.rpartition(":")
    assert host == "127.0.0.1"
    assert port == "9090"


def test_config_is_frozen_and_keyword_only() -> None:
    cfg = server.Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.pprof = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        server.Config("0.0.0.0:9090")  # type: ignore[misc]


def test_profile_handler_is_ignored_by_equality() -> None:
    def handler(environ: Any, start_response: Any) -> list[bytes]:
        return []

    assert server.Config(profile_handler=handler) == server.Config()


def test_timeouts_accept_timedeltas() -> None:
    cfg = server.Config(
        read_timeout=datetime.timedelta(seconds=2),
        read_header_timeout=datetime.timedelta(milliseconds=500),
        write_timeout=datetime.timedelta(seconds=3),
        idle_timeout=datetime.timedelta(minutes=1),
        shutdown_timeout=datetime.timedelta(seconds=4),
    )
    assert (
        cfg.read_timeout,
        cfg.read_header_timeout,
        cfg.write_timeout,
        cfg.idle_timeout,
        cfg.shutdown_timeout,
    ) == (2.0, 0.5, 3.0, 60.0, 4.0)


@pytest.mark.parametrize(
    "name",
    ["read_timeout", "read_header_timeout", "write_timeout", "idle_timeout", "shutdown_timeout"],
)
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "5", True])
def test_timeouts_must_be_positive_finite_numbers(name: str, value: object) -> None:
    with pytest.raises(ValueError, match=name):
        server.Config(**{name: value})


@pytest.mark.parametrize("name", ["max_header_bytes", "max_connections"])
@pytest.mark.parametrize("value", [0, -5])
def test_limits_must_be_positive(name: str, value: int) -> None:
    with pytest.raises(ValueError, match=name):
        server.Config(**{name: value})


@pytest.mark.parametrize("addr", ["", "localhost", "9090"])
def test_addr_must_be_host_port(addr: str) -> None:
    with pytest.raises(ValueError, match="addr"):
        server.Config(addr=addr)


@pytest.mark.parametrize("addr", ["127.0.0.1:0", "0.0.0.0:9090", "localhost:8080", "[::1]:9090"])
def test_valid_addresses(addr: str) -> None:
    assert server.Config(addr=addr).addr == addr


def test_every_problem_is_reported() -> None:
    with pytest.raises(ValueError, match="read_timeout") as info:
        server.Config(read_timeout=0, max_connections=0, addr="nope")
    message = str(info.value)
    assert "max_connections" in message
    assert "addr" in message


def test_small_valid_values() -> None:
    cfg = server.Config(read_header_timeout=0.01, max_header_bytes=1, max_connections=1)
    assert cfg.read_header_timeout == 0.01
    assert cfg.max_header_bytes == 1
    assert cfg.max_connections == 1

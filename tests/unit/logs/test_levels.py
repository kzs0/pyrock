"""parse_level and module constants."""

from __future__ import annotations

import logging

import pytest

from pyrock import logs


def test_constants() -> None:
    assert logs.LOGGER_NAME == "pyrock"
    assert logs.ATTRS_KEY == "pyrock_attrs"
    assert logs.CANONICAL_MESSAGE == "operation.complete"


@pytest.mark.parametrize(
    ("raw", "level"),
    [
        ("debug", logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        ("Info", logging.INFO),
        ("warn", logging.WARNING),
        ("WARN", logging.WARNING),
        ("warning", logging.WARNING),
        ("WARNING", logging.WARNING),
        ("error", logging.ERROR),
        ("ERROR", logging.ERROR),
        (logging.DEBUG, logging.DEBUG),
        (logging.INFO, logging.INFO),
        (logging.WARNING, logging.WARNING),
        (logging.ERROR, logging.ERROR),
        (logging.CRITICAL, logging.CRITICAL),
        (25, 25),
    ],
)
def test_parse_level(raw: str | int, level: int) -> None:
    assert logs.parse_level(raw) == level


@pytest.mark.parametrize("raw", ["", "verbose", "trace", "inf o", "warnings"])
def test_parse_level_rejects_unknown_names(raw: str) -> None:
    with pytest.raises(ValueError, match="level"):
        logs.parse_level(raw)

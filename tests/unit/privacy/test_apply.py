"""``Policy.apply`` / ``Policy.value``: allowlists, sensitive keys, error namespaces,
redaction, truncation and unknown-type suppression (DESIGN §14)."""

from __future__ import annotations

import datetime
import threading
from typing import Any

import pytest

from pyrock.privacy import Policy
from tests.unit.privacy._helpers import SECRET, Opaque, contains_instance, leaks


def _apply(attrs: dict[str, Any], **policy: Any) -> dict[str, Any]:
    return Policy(**policy).apply(attrs)


# --------------------------------------------------------------- defaults


def test_default_keeps_plain_fields_in_order() -> None:
    attrs = {"z": "1", "a": 2, "m": 3.5, "flag": True}
    out = _apply(attrs)
    assert out == attrs
    assert list(out) == list(attrs)


def test_apply_returns_a_new_dict_and_preserves_input() -> None:
    nested = {"inner": "v"}
    attrs = {"a": "1", "n": nested}
    out = _apply(attrs)
    assert out is not attrs
    out["a"] = "changed"
    assert attrs == {"a": "1", "n": {"inner": "v"}}
    assert nested == {"inner": "v"}


def test_supported_scalar_types_are_kept() -> None:
    attrs = {
        "s": "x",
        "i": 3,
        "f": 1.5,
        "b": False,
        "td": datetime.timedelta(seconds=2),
        "dt": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        "raw": b"\x01\x02",
        "seq": ["a", "b"],
    }
    out = _apply(attrs)
    assert set(out) == set(attrs)
    assert out["s"] == "x"
    assert out["i"] == 3
    assert out["b"] is False


def test_nested_plain_values_are_kept() -> None:
    attrs = {"request": {"headers": {"accept": "*/*"}, "ids": [1, 2]}}
    assert _apply(attrs) == attrs


def test_empty_input() -> None:
    assert _apply({}) == {}


# ------------------------------------------------------------- allowlists


def test_exact_allowlist_is_strict() -> None:
    out = _apply({"status": "ok", "other": "x"}, attribute_keys=["status"])
    assert out == {"status": "ok"}


def test_empty_allowlist_drops_everything() -> None:
    assert _apply({"a": "1", "b": "2"}, attribute_keys=[]) == {}


def test_group_wildcard() -> None:
    out = _apply(
        {"user.id": "1", "user.tier": "gold", "username": "nope", "order.id": "9", "user2.x": "n"},
        attribute_keys=["user.*"],
    )
    assert out == {"user.id": "1", "user.tier": "gold"}


def test_group_prefix_admits_dotted_children() -> None:
    out = _apply(
        {"request.id": "r", "request.size": 3, "requester": "no"}, attribute_keys=["request"]
    )
    assert out == {"request.id": "r", "request.size": 3}


def test_group_prefix_admits_nested_children() -> None:
    out = _apply({"request": {"id": "r", "size": 3}, "other": 1}, attribute_keys=["request"])
    assert out == {"request": {"id": "r", "size": 3}}


def test_wildcard_admits_nested_children() -> None:
    out = _apply(
        {"user": {"id": "1", "tier": "gold"}, "order": {"id": "2"}}, attribute_keys=["user.*"]
    )
    assert out == {"user": {"id": "1", "tier": "gold"}}


def test_allowlisted_leaf_filters_its_parent_group() -> None:
    out = _apply(
        {"user": {"id": "1", "email": "a@b.c"}, "user.id": "2", "user.email": "x"},
        attribute_keys=["user.id"],
    )
    assert out == {"user": {"id": "1"}, "user.id": "2"}


def test_multiple_allowlist_entries() -> None:
    out = _apply(
        {"a": 1, "b.x": 2, "c": 3, "d.y": 4},
        attribute_keys=["a", "b.*", "d.y"],
    )
    assert out == {"a": 1, "b.x": 2, "d.y": 4}


# ---------------------------------------------------------- sensitive keys


SENSITIVE_KEYS = [
    "password",
    "user.password",
    "db.passwd",
    "http.authorization",
    "Authorization",
    "session.cookie",
    "set-cookie",
    "api_key",
    "x.apikey",
    "client_secret",
    "oauth.access_token",
    "refresh_token",
    "auth.token",
    "token",
]


@pytest.mark.parametrize("key", SENSITIVE_KEYS)
def test_sensitive_keys_are_hidden_by_default(key: str) -> None:
    out = _apply({key: SECRET, "plain": "visible"})
    assert out["plain"] == "visible"
    assert not leaks(out)


@pytest.mark.parametrize("key", ["user.password", "auth.token"])
def test_sensitive_keys_stay_hidden_even_when_allowlisted(key: str) -> None:
    out = _apply({key: SECRET}, attribute_keys=[key])
    assert not leaks(out)


def test_nested_sensitive_keys_are_hidden() -> None:
    attrs = {
        "payload": {
            "password": SECRET,
            "items": [{"authorization": SECRET}, {"ok": "fine"}],
            "headers": {"cookie": SECRET, "accept": "*/*"},
        }
    }
    out = _apply(attrs)
    assert not leaks(out)
    assert out["payload"]["headers"]["accept"] == "*/*"


def test_tokens_that_only_look_similar_are_kept() -> None:
    out = _apply({"tokenizer.name": "bpe", "user.tier": "gold"})
    assert out == {"tokenizer.name": "bpe", "user.tier": "gold"}


# ---------------------------------------------------- sensitive http attrs


@pytest.mark.parametrize("key", ["http.user_agent", "http.target", "http.url"])
def test_sensitive_http_attrs_dropped_by_default(key: str) -> None:
    out = _apply({key: "https://example.test/" + SECRET, "http.method": "GET"})
    assert key not in out
    assert out["http.method"] == "GET"


@pytest.mark.parametrize("allow", ["http.user_agent", "http.*"])
def test_user_agent_emitted_when_allowlisted(allow: str) -> None:
    out = _apply({"http.user_agent": "curl/8.0"}, attribute_keys=[allow])
    assert out == {"http.user_agent": "curl/8.0"}


def test_http_target_emitted_when_allowlisted() -> None:
    out = _apply({"http.target": "/users/42"}, attribute_keys=["http.target"])
    assert out == {"http.target": "/users/42"}


def test_allowlisted_http_url_is_sanitized() -> None:
    url = f"https://user:{SECRET}@example.test/private/path?token={SECRET}#{SECRET}"
    out = _apply({"http.url": url}, attribute_keys=["http.*"])
    assert not leaks(out)
    assert out["http.url"].startswith("https://example.test")
    assert "/private/path" in out["http.url"]


# --------------------------------------------------------- error namespaces


@pytest.mark.parametrize(
    "attrs",
    [
        {"error.message": SECRET},
        {"exception.message": SECRET},
        {"exception.stacktrace": SECRET},
        {"error.stack": SECRET},
        {"error": {"message": SECRET}},
        {"exception": {"message": SECRET}},
        {"payload": {"error": {"message": SECRET}}},
        {"job.error.message": SECRET},
    ],
    ids=[
        "error.message",
        "exception.message",
        "exception.stacktrace",
        "error.stack",
        "nested-error",
        "nested-exception",
        "deep-nested-error",
        "qualified-error",
    ],
)
def test_error_details_are_private_by_default(attrs: dict[str, Any]) -> None:
    assert not leaks(_apply(attrs))


@pytest.mark.parametrize("group", ["error", "exception"])
def test_error_namespace_is_private_through_group_ancestry(group: str) -> None:
    out = Policy().apply({"detail": SECRET, "extra": {"deeper": SECRET}}, path=group)
    assert not leaks(out)


def test_error_summary_keys_survive_by_default() -> None:
    attrs = {
        "error": True,
        "error.type": "builtins.ValueError",
        "error.fingerprint": "0123456789abcdef",
    }
    out = _apply(attrs)
    assert out == attrs


def test_error_details_allowed() -> None:
    out = _apply(
        {"error.message": "visible", "exception.message": "also visible"},
        allow_error_details=True,
    )
    assert out == {"error.message": "visible", "exception.message": "also visible"}


def test_nested_error_details_allowed() -> None:
    attrs = {"payload": {"error": {"message": "visible"}}}
    assert _apply(attrs, allow_error_details=True) == attrs


def test_error_stack_needs_source_paths() -> None:
    stack = "Traceback: /srv/app/main.py line 3"
    hidden = _apply({"error.stack": stack}, allow_error_details=True)
    assert stack not in str(hidden.get("error.stack", ""))
    shown = _apply({"error.stack": stack}, allow_error_details=True, allow_source_paths=True)
    assert shown == {"error.stack": stack}


def test_exception_values_never_leak_their_message() -> None:
    out = _apply({"cause": ValueError(SECRET), "kept": "yes"})
    assert out["kept"] == "yes"
    assert not leaks(out)


# ----------------------------------------------------------------- redact


def test_redact_rewrites_value() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return ("[email]", True) if path == "user.email" else (value, True)

    out = _apply({"user.email": "a@b.c", "kept": "yes"}, redact=redact)
    assert out == {"user.email": "[email]", "kept": "yes"}


def test_redact_can_drop_value() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return (None, False) if path == "drop.me" else (value, True)

    assert _apply({"drop.me": "x", "kept": "yes"}, redact=redact) == {"kept": "yes"}


def test_redact_receives_the_original_value() -> None:
    seen: dict[str, Any] = {}

    def redact(path: str, value: Any) -> tuple[Any, bool]:
        seen[path] = value
        return value, True

    _apply({"n": 3, "s": "text", "b": True}, redact=redact)
    assert seen["n"] == 3
    assert seen["s"] == "text"
    assert seen["b"] is True


@pytest.mark.parametrize(
    "exc", [RuntimeError("bug"), ValueError("bug"), TypeError("bug"), KeyError("bug")]
)
def test_redact_exception_fails_closed(exc: Exception) -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        if path == "fragile":
            raise exc
        return value, True

    out = _apply({"fragile": SECRET, "kept": "yes"}, redact=redact)
    assert out == {"kept": "yes"}


def test_redact_exception_in_nested_value_fails_closed() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        if path == "req.headers.x-custom":
            raise RuntimeError("bug")
        return value, True

    out = _apply({"req": {"headers": {"x-custom": SECRET, "accept": "*/*"}}}, redact=redact)
    assert not leaks(out)
    assert out["req"]["headers"]["accept"] == "*/*"


def test_redact_receives_qualified_nested_paths() -> None:
    seen: list[str] = []
    lock = threading.Lock()

    def redact(path: str, value: Any) -> tuple[Any, bool]:
        with lock:
            seen.append(path)
        return value, True

    attrs = {"request": {"headers": {"x-request-id": "r-1"}}, "top": 1}
    out = _apply(attrs, redact=redact)
    assert "request.headers.x-request-id" in seen
    assert "top" in seen
    assert out == attrs


def test_apply_path_prefixes_redact_paths() -> None:
    seen: list[str] = []

    def redact(path: str, value: Any) -> tuple[Any, bool]:
        seen.append(path)
        return value, True

    out = Policy(redact=redact).apply({"headers": {"x-request-id": "r"}}, path="request")
    assert "request.headers.x-request-id" in seen
    assert out == {"headers": {"x-request-id": "r"}}


def test_redact_rewrites_nested_value() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return ("***", True) if path == "request.headers.x-api-key" else (value, True)

    out = _apply(
        {"request": {"headers": {"x-api-key": SECRET, "accept": "*/*"}}},
        redact=redact,
    )
    assert out["request"]["headers"]["accept"] == "*/*"
    assert not leaks(out)


def test_redact_applies_inside_lists() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return ("[x]", True) if value == SECRET else (value, True)

    out = _apply({"items": ["ok", SECRET, "fine"]}, redact=redact)
    assert not leaks(out)
    assert "ok" in out["items"]
    assert "fine" in out["items"]


# ------------------------------------------------------------- truncation


def test_strings_at_the_limit_are_unchanged() -> None:
    assert _apply({"s": "a" * 16}, max_string_length=16) == {"s": "a" * 16}


@pytest.mark.parametrize("length", [17, 100, 10_000])
def test_long_strings_are_truncated(length: int) -> None:
    value = _apply({"s": "a" * length}, max_string_length=16)["s"]
    assert isinstance(value, str)
    assert value != "a" * length
    assert value.startswith("a" * 8)
    assert len(value) <= 16 + len("[truncated]")


def test_truncation_counts_characters_not_bytes() -> None:
    value = _apply({"s": "é" * 10}, max_string_length=10)["s"]
    assert value == "é" * 10


def test_nested_strings_are_truncated() -> None:
    out = _apply({"p": {"long": "b" * 500, "items": ["c" * 500]}}, max_string_length=8)
    assert len(out["p"]["long"]) <= 8 + len("[truncated]")
    assert all(len(s) <= 8 + len("[truncated]") for s in out["p"]["items"])


def test_default_max_string_length() -> None:
    value = _apply({"s": "b" * 4096})["s"]
    assert len(value) <= 1024 + len("[truncated]")


# ---------------------------------------------------------- unknown types


def test_unknown_top_level_object_is_dropped_without_rendering() -> None:
    obj = Opaque()
    out = _apply({"obj": obj, "kept": "yes"})
    assert out == {"kept": "yes"}
    assert not obj.rendered.is_set()


def test_unknown_nested_objects_are_suppressed_without_rendering() -> None:
    obj = Opaque()
    out = _apply({"nested": {"o": obj, "k": "v"}, "items": [obj, "x"]})
    assert not obj.rendered.is_set()
    assert not contains_instance(out, Opaque)
    assert not leaks(out)
    assert out["nested"]["k"] == "v"


@pytest.mark.parametrize(
    "value",
    [object(), {1, 2}, lambda: None, threading.Lock()],
    ids=["object", "set", "lambda", "lock"],
)
def test_other_unknown_types_are_suppressed(value: Any) -> None:
    out = _apply({"v": value, "kept": "yes"})
    assert out.get("kept") == "yes"
    assert not contains_instance(out, type(value))


def test_non_string_dict_keys_are_suppressed() -> None:
    obj = Opaque()
    out = _apply({"m": {obj: "v", 1: "w", "ok": "x"}})
    assert not obj.rendered.is_set()
    assert not contains_instance(out, Opaque)


# ------------------------------------------------------------------ value


def test_value_keeps_plain_value() -> None:
    assert Policy().value("x", "abc") == ("abc", True)
    assert Policy().value("n", 5) == (5, True)


def test_value_respects_path_rules() -> None:
    value, keep = Policy().value("user.password", SECRET)
    assert not (keep and leaks(value))
    value, keep = Policy().value("error.message", SECRET)
    assert not (keep and leaks(value))


def test_value_suppresses_unknown_object() -> None:
    obj = Opaque()
    value, keep = Policy().value("o", obj)
    assert not (keep and isinstance(value, Opaque))
    assert not obj.rendered.is_set()


def test_value_truncates() -> None:
    value, keep = Policy(max_string_length=4).value("s", "abcdefgh")
    assert keep
    assert isinstance(value, str)
    assert value.startswith("abcd")
    assert len(value) <= 4 + len("[truncated]")


def test_value_redact_drop() -> None:
    policy = Policy(redact=lambda path, value: (value, path != "gone"))
    assert policy.value("gone", "x")[1] is False
    assert policy.value("here", "x") == ("x", True)


def test_value_redact_exception_drops() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        raise RuntimeError("bug")

    assert Policy(redact=redact).value("x", SECRET)[1] is False

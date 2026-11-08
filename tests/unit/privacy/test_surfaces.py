"""``Policy.resource``, ``label_value``, ``message``, ``profile_labels`` and ``sanitize_url``."""

from __future__ import annotations

import random
from typing import Any

import pytest

from pyrock.attr import MISSING_LABEL
from pyrock.privacy import MAX_PROFILE_LABEL_DEPTH, MAX_PROFILE_LABELS, Policy, sanitize_url
from tests.unit.privacy._helpers import SECRET, Opaque, leaks

INFRA = {
    "host.name": "box-1",
    "host.id": "i-123",
    "process.pid": 42,
    "process.executable.path": "/usr/bin/python",
    "k8s.pod.name": "pod-a",
    "k8s.namespace.name": "default",
    "container.id": "abc123",
}


# --------------------------------------------------------------- resource


def test_resource_keeps_service_identity_and_static_attrs() -> None:
    attrs = {"service.name": "svc", "service.version": "1.2.3", "env": "prod", "team": "core"}
    assert Policy().resource(attrs) == attrs


@pytest.mark.parametrize("key", sorted(INFRA))
def test_infrastructure_keys_dropped_by_default(key: str) -> None:
    out = Policy().resource({"service.name": "svc", key: INFRA[key]})
    assert out == {"service.name": "svc"}


def test_resource_keys_admit_named_infrastructure_keys() -> None:
    out = Policy(resource_keys=["host.name", "k8s.pod.name"]).resource(  # type: ignore[arg-type]
        {"service.name": "svc", **INFRA}
    )
    assert out == {"service.name": "svc", "host.name": "box-1", "k8s.pod.name": "pod-a"}


def test_resource_keys_are_additive_not_strict() -> None:
    out = Policy(resource_keys=["host.name"]).resource(  # type: ignore[arg-type]
        {"service.name": "svc", "env": "prod", "host.name": "h", "process.pid": 1}
    )
    assert out == {"service.name": "svc", "env": "prod", "host.name": "h"}


def test_resource_preserves_order_and_input() -> None:
    attrs = {"b": "1", "host.name": "h", "a": "2"}
    out = Policy().resource(attrs)
    assert list(out) == ["b", "a"]
    assert attrs == {"b": "1", "host.name": "h", "a": "2"}


def test_resource_values_are_cleaned() -> None:
    out = Policy(max_string_length=4).resource(
        {"service.name": "long-service-name", "db.password": SECRET}
    )
    assert len(out["service.name"]) <= 4 + len("[truncated]")
    assert not leaks(out)


def test_resource_unknown_values_suppressed() -> None:
    obj = Opaque()
    out = Policy().resource({"service.name": "svc", "weird": obj})
    assert not obj.rendered.is_set()
    assert all(not isinstance(v, Opaque) for v in out.values())


# ------------------------------------------------------------ label_value


def test_label_value_plain() -> None:
    assert Policy().label_value("status", "active") == "active"


@pytest.mark.parametrize("key", ["password", "user.password", "authorization", "api_key"])
def test_sensitive_label_value_is_hidden(key: str) -> None:
    out = Policy().label_value(key, SECRET)
    assert isinstance(out, str)
    assert SECRET not in out


def test_label_value_not_allowlisted_becomes_missing() -> None:
    policy = Policy(attribute_keys=["status"])  # type: ignore[arg-type]
    assert policy.label_value("other", "x") == MISSING_LABEL
    assert policy.label_value("status", "ok") == "ok"


def test_label_value_wildcard_allowlist() -> None:
    policy = Policy(attribute_keys=["user.*"])  # type: ignore[arg-type]
    assert policy.label_value("user.tier", "gold") == "gold"
    assert policy.label_value("username", "bob") == MISSING_LABEL


def test_label_value_redact_rewrites() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        return ("[email]", True) if path == "user.email" else (value, True)

    assert Policy(redact=redact).label_value("user.email", "a@b.c") == "[email]"


def test_label_value_redact_drop_becomes_missing() -> None:
    policy = Policy(redact=lambda path, value: (None, False))
    assert policy.label_value("k", "v") == MISSING_LABEL


def test_label_value_redact_exception_becomes_missing() -> None:
    def redact(path: str, value: Any) -> tuple[Any, bool]:
        raise RuntimeError("bug")

    assert Policy(redact=redact).label_value("k", SECRET) == MISSING_LABEL


def test_label_value_redact_returning_non_string_is_not_rendered() -> None:
    obj = Opaque()
    out = Policy(redact=lambda path, value: (obj, True)).label_value("k", "v")
    assert isinstance(out, str)
    assert not obj.rendered.is_set()
    assert SECRET not in out


def test_label_value_truncated() -> None:
    out = Policy(max_string_length=5).label_value("k", "abcdefghij")
    assert out.startswith("abcde")
    assert len(out) <= 5 + len("[truncated]")


def test_label_value_user_agent_needs_allowlist() -> None:
    assert SECRET not in Policy().label_value("http.user_agent", SECRET)
    allowed = Policy(attribute_keys=["http.*"])  # type: ignore[arg-type]
    assert allowed.label_value("http.user_agent", "curl/8") == "curl/8"


# ---------------------------------------------------------------- message


def test_message_short_is_unchanged() -> None:
    assert Policy().message("all good") == "all good"


def test_message_truncated() -> None:
    out = Policy(max_string_length=10).message("x" * 100)
    assert isinstance(out, str)
    assert out.startswith("x" * 5)
    assert len(out) <= 10 + len("[truncated]")


def test_message_default_limit() -> None:
    assert len(Policy().message("y" * 5000)) <= 1024 + len("[truncated]")


# --------------------------------------------------------- profile labels


def _allowed(**kw: Any) -> Policy:
    return Policy(allow_profile_labels=True, **kw)


def test_profile_labels_dropped_unless_allowed() -> None:
    assert Policy().profile_labels({"endpoint": "/users", "tenant": "t1"}) == {}


def test_profile_labels_kept_when_allowed() -> None:
    labels = {"endpoint": "/users", "tenant": "t1"}
    assert _allowed().profile_labels(labels) == labels


def test_profile_labels_at_the_item_limit() -> None:
    labels = {f"l{i}": str(i) for i in range(MAX_PROFILE_LABELS)}
    assert _allowed().profile_labels(labels) == labels


def test_profile_labels_over_the_item_limit_are_rejected() -> None:
    labels = {f"l{i}": str(i) for i in range(MAX_PROFILE_LABELS + 1)}
    with pytest.raises(ValueError):  # noqa: PT011 - only the exception type is contractual
        _allowed().profile_labels(labels)


def test_profile_label_limit_applies_after_policy() -> None:
    labels = {f"l{i}": str(i) for i in range(MAX_PROFILE_LABELS * 2)}
    labels["keep"] = "yes"
    out = _allowed(attribute_keys=["keep"]).profile_labels(labels)
    assert out == {"keep": "yes"}


def test_profile_labels_too_deep_are_rejected() -> None:
    nested: Any = "leaf"
    for _ in range(MAX_PROFILE_LABEL_DEPTH * 3):
        nested = {"n": nested}
    with pytest.raises(ValueError):  # noqa: PT011
        _allowed().profile_labels({"deep": nested})


def test_profile_labels_shallow_nesting_is_fine() -> None:
    labels = {"req": {"route": "/a"}}
    assert _allowed().profile_labels(labels) == labels


def test_profile_labels_are_cleaned() -> None:
    out = _allowed().profile_labels({"password": SECRET, "route": "/a"})
    assert out.get("route") == "/a"
    assert not leaks(out)


# ------------------------------------------------------------ sanitize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            f"https://user:{SECRET}@example.test/private/path?token={SECRET}#{SECRET}",
            "https://example.test/private/path",
        ),
        ("http://u:p@host:8080/a/b?x=1", "http://host:8080/a/b"),
        ("https://example.test/a/b", "https://example.test/a/b"),
        ("https://example.test/a?", "https://example.test/a"),
        ("https://example.test/a#", "https://example.test/a"),
        ("https://user@example.test/", "https://example.test/"),
        ("http://[::1]:4318/v1/traces?k=v", "http://[::1]:4318/v1/traces"),
    ],
)
def test_sanitize_url_keeps_path(raw: str, expected: str) -> None:
    assert sanitize_url(raw) == expected


def test_sanitize_url_without_path() -> None:
    out = sanitize_url(
        f"https://user:{SECRET}@example.test:8443/private?q={SECRET}", keep_path=False
    )
    assert out.startswith("https://example.test:8443")
    assert "private" not in out
    assert SECRET not in out


@pytest.mark.parametrize("raw", ["http://[bad", "::::", "", "not a url at all ?q=" + SECRET])
def test_sanitize_url_never_raises_or_leaks_queries(raw: str) -> None:
    out = sanitize_url(raw)
    assert isinstance(out, str)
    assert SECRET not in out


def test_sanitize_url_property() -> None:
    rng = random.Random(4242)
    for _ in range(300):
        scheme = rng.choice(["http", "https"])
        host = rng.choice(["example.test", "10.0.0.1", "svc.internal"])
        port = rng.choice(["", ":80", ":4318"])
        path = rng.choice(["", "/", "/a", "/a/b/c", "/v1/traces"])
        user = rng.choice(["", "alice@", f"alice:{SECRET}@", f"{SECRET}@"])
        query = rng.choice(["", f"?token={SECRET}", f"?a=1&b={SECRET}", "?"])
        frag = rng.choice(["", f"#{SECRET}", "#"])
        raw = f"{scheme}://{user}{host}{port}{path}{query}{frag}"
        out = sanitize_url(raw)
        assert SECRET not in out, raw
        assert "alice" not in out
        assert "?" not in out
        assert "#" not in out
        assert out.startswith(f"{scheme}://{host}{port}")
        if path not in ("", "/"):
            assert out.endswith(path)
        assert host in sanitize_url(raw, keep_path=False)
        if path not in ("", "/"):
            assert path not in sanitize_url(raw, keep_path=False)

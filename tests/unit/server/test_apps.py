"""``server.metrics_app`` and ``server.health_app`` as plain WSGI apps."""

from __future__ import annotations

import wsgiref.validate

import pytest

from pyrock import server
from pyrock.metric import Bucket, MetricFamily, MetricType, Registry, Sample
from tests.support.prom import parse
from tests.support.wsgi_driver import call_wsgi


class BadRegistry(Registry):
    """A registry whose snapshot is not valid exposition (decreasing histogram buckets)."""

    def collect(self) -> list[MetricFamily]:
        bad = Sample(labels={}, buckets=(Bucket(1.0, 5), Bucket(0.5, 3)), count=2, sum=1.0)
        good = Sample(labels={}, value=1.0)
        return [
            MetricFamily("a_ok_total", "fine", MetricType.COUNTER, (good,)),
            MetricFamily("bad_hist", "broken", MetricType.HISTOGRAM, (bad,)),
        ]


# ---------------------------------------------------------------- metrics_app


def test_metrics_app_serves_prometheus_text() -> None:
    registry = Registry()
    registry.counter("app_hits_total", "Hits").inc(3)
    resp = call_wsgi(server.metrics_app(registry), "GET", "/metrics")
    assert resp.status_code == 200
    assert (resp.header("Content-Type") or "").startswith("text/plain")
    prom = parse(resp.body)
    assert prom.value("app_hits_total") == 3
    assert prom.type("app_hits_total") == "counter"


def test_metrics_app_sets_content_length() -> None:
    registry = Registry()
    registry.gauge("app_depth", "Depth").set(4)
    resp = call_wsgi(server.metrics_app(registry), "GET", "/metrics")
    assert int(resp.header("Content-Length") or -1) == len(resp.body)


def test_metrics_app_reads_the_registry_per_request() -> None:
    registry = Registry()
    gauge = registry.gauge("app_level", "Level")
    app = server.metrics_app(registry)
    gauge.set(1)
    first = parse(call_wsgi(app, "GET", "/metrics").body).value("app_level")
    gauge.set(9)
    second = parse(call_wsgi(app, "GET", "/metrics").body).value("app_level")
    assert (first, second) == (1, 9)


def test_metrics_app_can_be_mounted_anywhere() -> None:
    registry = Registry()
    registry.counter("app_mounted_total", "Mounted").inc()
    resp = call_wsgi(
        server.metrics_app(registry),
        "GET",
        "/internal/metrics",
        environ={"SCRIPT_NAME": "/internal", "PATH_INFO": "/metrics"},
    )
    assert resp.status_code == 200
    assert parse(resp.body).value("app_mounted_total") == 1


def test_metrics_app_exposition_error_is_500_with_empty_body() -> None:
    resp = call_wsgi(server.metrics_app(BadRegistry()), "GET", "/metrics")
    assert resp.status_code == 500
    assert resp.body == b""
    assert len(resp.start_response_calls) == 1


def test_metrics_app_empty_registry() -> None:
    resp = call_wsgi(server.metrics_app(Registry()), "GET", "/metrics")
    assert resp.status_code == 200
    assert not parse(resp.body).names()


def test_metrics_app_is_pep3333_compliant() -> None:
    registry = Registry()
    registry.counter("app_valid_total", "Valid").inc()
    app = wsgiref.validate.validator(server.metrics_app(registry))
    resp = call_wsgi(app, "GET", "/metrics")
    assert resp.status_code == 200


# ---------------------------------------------------------------- health_app


def test_health_app_answers_ok() -> None:
    resp = call_wsgi(server.health_app(), "GET", "/health")
    assert resp.status_code == 200
    assert resp.body.strip() == b"ok"
    assert (resp.header("Content-Type") or "").startswith("text/plain")


@pytest.mark.parametrize("path", ["/health", "/ready", "/anything"])
def test_health_app_ignores_the_path(path: str) -> None:
    assert call_wsgi(server.health_app(), "GET", path).status_code == 200


def test_health_app_is_pep3333_compliant() -> None:
    resp = call_wsgi(wsgiref.validate.validator(server.health_app()), "GET", "/health")
    assert resp.status_code == 200


def test_apps_are_independent_callables() -> None:
    assert server.health_app() is not None
    registry = Registry()
    assert callable(server.metrics_app(registry))
    assert callable(server.health_app())

"""P1-6: request-id correlation, /ready, and /metrics."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.telemetry import RequestMetrics


def test_health_still_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_response_carries_a_request_id_header(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["x-request-id"]


def test_an_inbound_request_id_is_echoed_back(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "caller-supplied-id"})
    assert response.headers["x-request-id"] == "caller-supplied-id"


def test_two_requests_get_different_request_ids(client: TestClient) -> None:
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]
    assert first != second


def test_ready_reports_ok_when_dependencies_are_healthy(client: TestClient) -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["vector_store"] == "ok"
    assert body["checks"]["scheduler"] == "ok"


def test_ready_reports_503_when_the_database_is_unreachable(
    client: TestClient, monkeypatch
) -> None:
    def _broken_execute(*_args, **_kwargs):
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(Session, "execute", _broken_execute)
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"


def test_metrics_endpoint_reports_counts_for_requests_already_made(
    client: TestClient,
) -> None:
    client.get("/health")
    client.get("/health")

    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert 'path="/health"' in body
    assert "trailmind_http_requests_total" in body
    assert "trailmind_http_request_duration_seconds_sum" in body


def test_metrics_uses_route_template_not_raw_widget_id(client: TestClient) -> None:
    client.get("/api/admin/widgets/999999")  # unauthenticated, still routed

    response = client.get("/metrics")
    assert 'path="/api/admin/widgets/{widget_id}"' in response.text
    assert "999999" not in response.text


def test_request_metrics_records_and_renders_expected_shape() -> None:
    metrics = RequestMetrics()
    metrics.record("GET", "/health", 200, 0.01)
    metrics.record("GET", "/health", 200, 0.02)

    text = metrics.render_prometheus_text()
    assert (
        'trailmind_http_requests_total{method="GET",path="/health",status="200"} 2'
        in text
    )
    assert (
        'trailmind_http_request_duration_seconds_count{method="GET",path="/health"} 2'
        in text
    )

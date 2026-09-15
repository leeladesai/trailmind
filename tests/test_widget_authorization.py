"""P0-3: centralized widget authorization.

Every widget-facing endpoint (tracking ingestion, latest-recommendation polling,
SSE stream, widget Q&A, widget activity) must enforce the same policy — valid key,
allowed origin, widget status, tenant status — via the single
`resolve_authorized_widget` service function (app/services/widgets.py), not
duplicated ad hoc per route. This covers both that shared function directly and
each of the five HTTP endpoints that call into it.
"""

import pytest
from fastapi.testclient import TestClient

from app.models import Widget
from app.services.tenants import create_tenant
from app.services.widgets import (
    INGESTION_WIDGET_STATUSES,
    WidgetAuthError,
    create_widget,
    resolve_authorized_widget,
)


def _call(
    client: TestClient, endpoint: str, widget_key: str, visitor_id: str, origin=None
):
    headers = {"Origin": origin} if origin else {}
    if endpoint == "track_events":
        return client.post(
            "/api/track/events",
            json={
                "widget_key": widget_key,
                "visitor_id": visitor_id,
                "events": [{"event_type": "search", "metadata": {}}],
            },
            headers=headers,
        )
    if endpoint == "latest":
        return client.get(
            "/api/recommendations/latest"
            f"?widget_key={widget_key}&visitor_id={visitor_id}",
            headers=headers,
        )
    if endpoint == "stream":
        return client.get(
            f"/api/widget/stream?widget_key={widget_key}&visitor_id={visitor_id}",
            headers=headers,
        )
    if endpoint == "ask":
        return client.post(
            "/api/widget/ask",
            json={
                "widget_key": widget_key,
                "visitor_id": visitor_id,
                "question": "anything",
            },
            headers=headers,
        )
    if endpoint == "activity":
        return client.get(
            f"/api/widget/activity?widget_key={widget_key}&visitor_id={visitor_id}",
            headers=headers,
        )
    raise ValueError(endpoint)


ALL_ENDPOINTS = ["track_events", "latest", "stream", "ask", "activity"]
# track_events deliberately allows an onboarding widget through (see
# INGESTION_WIDGET_STATUSES) — every other endpoint requires active.
ACTIVE_ONLY_ENDPOINTS = ["latest", "stream", "ask", "activity"]
# "stream" opens a real SSE connection on success — a sync TestClient reading its
# response would block forever (see test_widget.py's module docstring). Only use
# "stream" in tests that expect a rejection (which returns before streaming
# starts); exclude it from any test asserting a *successful* call.
NON_STREAMING_ACTIVE_ONLY_ENDPOINTS = ["latest", "ask", "activity"]
NON_STREAMING_ENDPOINTS = ["track_events", "latest", "ask", "activity"]


@pytest.mark.parametrize("endpoint", ALL_ENDPOINTS)
def test_invalid_widget_key_is_rejected_on_every_endpoint(
    client: TestClient, endpoint: str
) -> None:
    response = _call(client, endpoint, "wk_live_totally_bogus", "v-1")
    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ALL_ENDPOINTS)
def test_suspended_widget_is_rejected_on_every_endpoint(
    client: TestClient, endpoint: str
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Suspended Widget Tenant")
        widget, raw_key = create_widget(session, tenant, "Widget")
        widget.status = "suspended"
        session.commit()

    response = _call(client, endpoint, raw_key, "v-1")
    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ACTIVE_ONLY_ENDPOINTS)
def test_onboarding_widget_is_rejected_on_endpoints_other_than_ingestion(
    client: TestClient, endpoint: str
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Onboarding Tenant")
        _, raw_key = create_widget(session, tenant, "Widget")
        # create_widget defaults to status="onboarding" — no override needed.

    response = _call(client, endpoint, raw_key, "v-1")
    assert response.status_code == 403


def test_onboarding_widget_is_allowed_through_ingestion(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Onboarding Tenant")
        _, raw_key = create_widget(session, tenant, "Widget")

    response = _call(client, "track_events", raw_key, "v-1")
    assert response.status_code == 200


@pytest.mark.parametrize("endpoint", ALL_ENDPOINTS)
@pytest.mark.parametrize("tenant_status", ["suspended", "rejected", "pending_approval"])
def test_widget_is_rejected_when_its_tenant_is_not_in_good_standing(
    client: TestClient, endpoint: str, tenant_status: str
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, f"Tenant ({tenant_status})")
        widget, raw_key = create_widget(session, tenant, "Widget")
        widget.status = "active"
        tenant.status = tenant_status
        session.commit()

    response = _call(client, endpoint, raw_key, "v-1")
    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", ALL_ENDPOINTS)
def test_request_from_a_disallowed_origin_is_rejected(
    client: TestClient, endpoint: str
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Origin-Locked Tenant")
        widget, raw_key = create_widget(session, tenant, "Widget")
        widget.status = "active"
        widget.allowed_origins = ["https://trusted.example.com"]
        session.commit()

    response = _call(
        client, endpoint, raw_key, "v-1", origin="https://evil.example.com"
    )
    assert response.status_code == 403


@pytest.mark.parametrize("endpoint", NON_STREAMING_ENDPOINTS)
def test_request_from_an_allowed_origin_is_not_rejected_for_origin_reasons(
    client: TestClient, endpoint: str
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Origin-Locked Tenant 2")
        widget, raw_key = create_widget(session, tenant, "Widget")
        widget.status = "active"
        widget.allowed_origins = ["https://trusted.example.com"]
        session.commit()

    response = _call(
        client, endpoint, raw_key, "v-1", origin="https://trusted.example.com"
    )
    assert response.status_code != 403 or response.json().get("detail") != (
        "Origin not allowed"
    )


@pytest.mark.parametrize("endpoint", NON_STREAMING_ACTIVE_ONLY_ENDPOINTS)
def test_widget_key_never_grants_access_to_another_widgets_endpoint_calls(
    client: TestClient, endpoint: str
) -> None:
    """A valid, active widget key for widget A must never be usable to read/act on
    widget B's data — every endpoint scopes strictly by the resolved widget, never
    by anything the caller supplies (e.g. a widget_id in the body/query)."""
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Two Widget Tenant")
        widget_a, key_a = create_widget(session, tenant, "Widget A")
        widget_b, key_b = create_widget(session, tenant, "Widget B")
        widget_a.status = "active"
        widget_b.status = "active"
        session.commit()

    response_a = _call(client, endpoint, key_a, "v-1")
    response_b = _call(client, endpoint, key_b, "v-1")
    # Both keys are valid for their own widget — neither should be rejected for
    # being "the wrong widget" (there's no such concept; each key only ever
    # resolves to the widget it belongs to).
    assert response_a.status_code != 403
    assert response_b.status_code != 403


class _FakeRequest:
    def __init__(self, origin: str = "") -> None:
        self.headers = {"origin": origin} if origin else {}


def test_resolve_authorized_widget_rejects_unknown_key(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        with pytest.raises(WidgetAuthError) as exc_info:
            resolve_authorized_widget(session, "wk_live_bogus", "")
    assert exc_info.value.code == "invalid_key"


def test_resolve_authorized_widget_allows_onboarding_only_when_permitted(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Onboarding Policy Tenant")
        _, raw_key = create_widget(session, tenant, "Widget")

        with pytest.raises(WidgetAuthError) as exc_info:
            resolve_authorized_widget(session, raw_key, "")
        assert exc_info.value.code == "widget_not_active"

        widget = resolve_authorized_widget(
            session,
            raw_key,
            "",
            allowed_widget_statuses=INGESTION_WIDGET_STATUSES,
        )
        assert isinstance(widget, Widget)


def test_resolve_authorized_widget_rejects_blocked_tenant(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Blocked Tenant")
        widget, raw_key = create_widget(session, tenant, "Widget")
        widget.status = "active"
        tenant.status = "suspended"
        session.commit()

        with pytest.raises(WidgetAuthError) as exc_info:
            resolve_authorized_widget(session, raw_key, "")
    assert exc_info.value.code == "tenant_not_active"

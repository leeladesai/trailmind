"""Tracker SDK phase (M3, docs/design/09-Platform-Pivot-Decision.md), updated for the
per-widget key cutover: widget API-key issuance/rotation/resolution
(app/services/widgets.py) and the anonymous-visitor ingestion endpoint
POST /api/track/events (app/main.py) — both now scoped to a Widget, not a Tenant (see
Widget's docstring in app/models.py).
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.models import CatalogItem, Event, Recommendation, WidgetApiKey
from app.services.tenants import create_tenant
from app.services.widgets import (
    create_widget,
    hash_api_key,
    issue_api_key,
    resolve_widget_by_api_key,
    revoke_api_key,
    rotate_api_key,
)


def test_create_widget_issues_a_working_key(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, raw_key = create_widget(session, tenant, "Credit Cards")
        assert widget.status == "onboarding"
        resolved = resolve_widget_by_api_key(session, raw_key)
        assert resolved is not None
        assert resolved.id == widget.id


def test_raw_key_is_never_stored(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, raw_key = create_widget(session, tenant, "Credit Cards")
        key_row = session.query(WidgetApiKey).filter_by(widget_id=widget.id).one()
        assert key_row.key_hash == hash_api_key(raw_key)
        assert raw_key not in key_row.key_hash


def test_resolve_widget_by_api_key_rejects_unknown_key(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        assert resolve_widget_by_api_key(session, "wk_live_does-not-exist") is None


def test_resolve_widget_by_api_key_rejects_revoked_key(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, raw_key = create_widget(session, tenant, "Credit Cards")
        key_row = session.query(WidgetApiKey).filter_by(widget_id=widget.id).one()
        revoke_api_key(session, key_row.id)
        assert resolve_widget_by_api_key(session, raw_key) is None


def test_rotate_api_key_keeps_old_key_valid_during_grace_period(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, old_key = create_widget(session, tenant, "Credit Cards")
        new_key = rotate_api_key(session, widget)

        assert resolve_widget_by_api_key(session, new_key) is not None
        # Old key still resolves — TEN-5's grace period, not an instant cutover.
        assert resolve_widget_by_api_key(session, old_key) is not None

        old_key_row = (
            session.query(WidgetApiKey)
            .filter_by(widget_id=widget.id, key_hash=hash_api_key(old_key))
            .one()
        )
        assert old_key_row.status == "grace"
        assert old_key_row.expires_at is not None


def test_resolve_widget_by_api_key_rejects_expired_grace_key(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, old_key = create_widget(session, tenant, "Credit Cards")
        rotate_api_key(session, widget)
        old_key_row = (
            session.query(WidgetApiKey)
            .filter_by(widget_id=widget.id, key_hash=hash_api_key(old_key))
            .one()
        )
        old_key_row.expires_at = datetime.utcnow() - timedelta(seconds=1)
        session.commit()

        assert resolve_widget_by_api_key(session, old_key) is None


def test_issue_api_key_adds_a_second_active_key(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, first_key = create_widget(session, tenant, "Credit Cards")
        second_key = issue_api_key(session, widget)
        assert resolve_widget_by_api_key(session, first_key) is not None
        assert resolve_widget_by_api_key(session, second_key) is not None


def test_track_events_accepts_a_valid_widget_key(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        _, raw_key = create_widget(session, tenant, "Credit Cards")

    response = client.post(
        "/api/track/events",
        json={
            "widget_key": raw_key,
            "visitor_id": "v-1",
            "events": [{"event_type": "search", "metadata": {"query": "travel card"}}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["recommendation_triggered"] is False


def test_track_events_rejects_an_unknown_widget_key(client: TestClient) -> None:
    response = client.post(
        "/api/track/events",
        json={
            "widget_key": "wk_live_bogus",
            "visitor_id": "v-1",
            "events": [{"event_type": "search", "metadata": {"query": "x"}}],
        },
    )
    assert response.status_code == 401


def test_track_events_drops_a_foreign_widgets_catalog_item_id(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        tenant_a = create_tenant(session, "Tenant A")
        tenant_b = create_tenant(session, "Tenant B")
        widget_a, key_a = create_widget(session, tenant_a, "Widget A")
        widget_b, _ = create_widget(session, tenant_b, "Widget B")
        # /api/recommendations/latest (checked below) requires an active widget.
        widget_a.status = "active"
        widget_a_id = widget_a.id
        foreign_item = CatalogItem(
            tenant_id=tenant_b.id,
            widget_id=widget_b.id,
            title="Widget B Only",
            provider="P",
            category="LLM",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        session.add(foreign_item)
        session.commit()
        foreign_item_id = foreign_item.id

    response = client.post(
        "/api/track/events",
        json={
            "widget_key": key_a,
            "visitor_id": "v-1",
            "events": [
                {
                    "event_type": "model_view",
                    "catalog_item_id": foreign_item_id,
                    "metadata": {},
                }
            ],
        },
    )
    assert response.status_code == 200

    latest = client.get(
        f"/api/recommendations/latest?widget_key={key_a}&visitor_id=v-1"
    ).json()
    # Nothing retrieved yet either way (below trigger threshold), but the real check
    # is server-side: confirm the stored event's catalog_item_id was actually dropped.
    assert latest["status"] == "pending"
    with client.app.state.session_factory() as session:
        stored = (
            session.query(Event)
            .filter_by(widget_id=widget_a_id, visitor_id="v-1")
            .one()
        )
        assert stored.catalog_item_id is None


def test_track_events_triggers_pipeline_after_session_threshold(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, raw_key = create_widget(session, tenant, "Credit Cards")
        item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Travel Card",
            provider="P",
            category="LLM",
            price="$0",
            description="A travel rewards card.",
            use_case_tags=["travel"],
        )
        session.add(item)
        session.commit()
        item_id = item.id

    response = client.post(
        "/api/track/events",
        json={
            "widget_key": raw_key,
            "visitor_id": "v-trigger",
            "events": [
                {"event_type": "search", "metadata": {"query": "travel"}},
                {
                    "event_type": "model_view",
                    "catalog_item_id": item_id,
                    "metadata": {},
                },
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["recommendation_triggered"] is True


def test_track_events_respects_tenant_rate_limit(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, raw_key = create_widget(session, tenant, "Credit Cards")
        tenant.max_agent_runs_per_hour = 1
        session.add(
            Recommendation(
                tenant_id=tenant.id,
                widget_id=widget.id,
                visitor_id="v-other",
                catalog_item_ids=[],
                behavior_summary="",
                activity_hash="already-at-cap",
                trigger_reason="event_threshold",
            )
        )
        session.commit()

    response = client.post(
        "/api/track/events",
        json={
            "widget_key": raw_key,
            "visitor_id": "v-new",
            "events": [
                {"event_type": "search", "metadata": {"query": "a"}},
                {"event_type": "search", "metadata": {"query": "b"}},
            ],
        },
    )
    assert response.status_code == 200
    # Would otherwise trigger (2 fresh events, new session) but the tenant is already
    # at its hourly cap (TEN-6, still tenant-wide — see tenant_rate_limited's
    # docstring) — a fabricated new visitor_id must not bypass it.
    assert response.json()["recommendation_triggered"] is False


def test_recommendations_latest_requires_a_valid_widget_key(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/recommendations/latest?widget_key=wk_live_bogus&visitor_id=v-1"
    )
    assert response.status_code == 401


def test_recommendations_latest_is_pending_with_no_activity(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        tenant = create_tenant(session, "Acme Bank")
        widget, raw_key = create_widget(session, tenant, "Credit Cards")
        widget.status = "active"
        session.commit()

    response = client.get(
        f"/api/recommendations/latest?widget_key={raw_key}&visitor_id=v-none"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"

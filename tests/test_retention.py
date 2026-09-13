from datetime import datetime, timedelta

from app.config import Settings
from app.db import build_session_factory
from app.models import Event, Recommendation, Tenant, WidgetSession
from app.services.retention import purge_expired_visitor_data


def _login(client) -> None:
    login = client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    assert login.status_code == 200


def _make_session_factory(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        chroma_db_path=str(tmp_path / "chroma"),
    )
    return build_session_factory(settings)


def _make_tenant(session) -> Tenant:
    tenant = Tenant(name="Test Tenant")
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def test_purge_deletes_only_rows_older_than_retention_window(tmp_path) -> None:
    session_factory = _make_session_factory(tmp_path)
    old = datetime.utcnow() - timedelta(days=100)

    with session_factory() as session:
        tenant = _make_tenant(session)
        old_event = Event(tenant_id=tenant.id, visitor_id="v1", event_type="page_view")
        recent_event = Event(
            tenant_id=tenant.id, visitor_id="v2", event_type="page_view"
        )
        old_rec = Recommendation(
            tenant_id=tenant.id,
            visitor_id="v1",
            catalog_item_ids=[],
            retrieval_meta=[],
            activity_hash="h1",
            trigger_reason="test",
        )
        recent_rec = Recommendation(
            tenant_id=tenant.id,
            visitor_id="v2",
            catalog_item_ids=[],
            retrieval_meta=[],
            activity_hash="h2",
            trigger_reason="test",
        )
        old_session = WidgetSession(
            tenant_id=tenant.id, visitor_id="v1", connection_id="c1"
        )
        recent_session = WidgetSession(
            tenant_id=tenant.id, visitor_id="v2", connection_id="c2"
        )
        session.add_all(
            [
                old_event,
                recent_event,
                old_rec,
                recent_rec,
                old_session,
                recent_session,
            ]
        )
        session.commit()
        # created_at/opened_at use server_default=func.now(), so backdate them
        # directly rather than depending on wall-clock timing in the test.
        old_event.created_at = old
        old_rec.created_at = old
        old_session.opened_at = old
        session.commit()

        old_event_id, recent_event_id = old_event.id, recent_event.id
        old_rec_id, recent_rec_id = old_rec.id, recent_rec.id
        old_session_id, recent_session_id = old_session.id, recent_session.id

        report = purge_expired_visitor_data(session, retention_days=90)

        assert report.events_deleted == 1
        assert report.recommendations_deleted == 1
        assert report.widget_sessions_deleted == 1
        assert session.get(Event, old_event_id) is None
        assert session.get(Event, recent_event_id) is not None
        assert session.get(Recommendation, old_rec_id) is None
        assert session.get(Recommendation, recent_rec_id) is not None
        assert session.get(WidgetSession, old_session_id) is None
        assert session.get(WidgetSession, recent_session_id) is not None


def test_purge_scoped_to_tenant_when_given(tmp_path) -> None:
    session_factory = _make_session_factory(tmp_path)
    old = datetime.utcnow() - timedelta(days=100)

    with session_factory() as session:
        tenant_a = _make_tenant(session)
        tenant_b = _make_tenant(session)
        event_a = Event(tenant_id=tenant_a.id, visitor_id="v1", event_type="page_view")
        event_b = Event(tenant_id=tenant_b.id, visitor_id="v2", event_type="page_view")
        session.add_all([event_a, event_b])
        session.commit()
        event_a.created_at = old
        event_b.created_at = old
        session.commit()
        event_a_id, event_b_id = event_a.id, event_b.id

        report = purge_expired_visitor_data(
            session, retention_days=90, tenant_id=tenant_a.id
        )

        assert report.events_deleted == 1
        assert session.get(Event, event_a_id) is None
        assert session.get(Event, event_b_id) is not None


def test_admin_purge_endpoint_deletes_only_this_tenants_expired_rows(
    client, reference_widget
) -> None:
    widget, _ = reference_widget
    _login(client)

    old = datetime.utcnow() - timedelta(days=200)
    with client.app.state.session_factory() as session:
        old_event = Event(
            tenant_id=widget.tenant_id, visitor_id="v1", event_type="page_view"
        )
        recent_event = Event(
            tenant_id=widget.tenant_id, visitor_id="v2", event_type="page_view"
        )
        session.add_all([old_event, recent_event])
        session.commit()
        old_event.created_at = old
        session.commit()
        old_event_id, recent_event_id = old_event.id, recent_event.id

    response = client.post("/api/admin/retention/purge")
    assert response.status_code == 200
    body = response.json()
    assert body["events_deleted"] == 1

    with client.app.state.session_factory() as session:
        assert session.get(Event, old_event_id) is None
        assert session.get(Event, recent_event_id) is not None

    audit = client.get("/api/admin/audit-log")
    actions = [row["action"] for row in audit.json()["entries"]]
    assert "retention_purge_triggered" in actions

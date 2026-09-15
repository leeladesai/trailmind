"""P0-1: catalog/vector reconciliation.

Covers: a missing vector entry (e.g. Chroma's ephemeral disk was wiped by a
restart/redeploy, so `vector_synced` is stale) is detected and rebuilt; a failing
upsert is recorded as `failed` with its error rather than silently left/marked
synced; an item is never re-touched once it's confirmed synced and present; the
per-widget attempt limit stops routine auto-retry (but not a forced admin
reindex); and reconciliation never crosses widget/collection boundaries.
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import build_session_factory
from app.models import CatalogItem, Tenant, Widget
from app.services.catalog_reconciliation import (
    MAX_AUTO_RETRY_ATTEMPTS,
    reconcile_catalog_index,
)
from app.services.widgets import create_widget
from app.vector import CatalogItemVectorStore, build_embedding_function


def _login(client: TestClient) -> None:
    login = client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    assert login.status_code == 200


def _make_session_factory(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        chroma_db_path=str(tmp_path / "chroma"),
        mesh_api_key=None,
    )
    return build_session_factory(settings), settings


def _make_vector_store(settings, tmp_path):
    return CatalogItemVectorStore(
        str(tmp_path / "chroma"),
        collection_name="models",
        embedding_function=build_embedding_function(settings),
    )


def _make_widget(session, name="Widget") -> Widget:
    tenant = Tenant(name=f"Tenant for {name}")
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    widget, _raw_key = create_widget(session, tenant, name)
    return widget


def _make_approved_item(
    session, widget: Widget, title="Item", **overrides
) -> CatalogItem:
    item = CatalogItem(
        tenant_id=widget.tenant_id,
        widget_id=widget.id,
        title=title,
        provider="Acme",
        category="Loans",
        price="$0",
        description="d",
        use_case_tags=[],
        review_status="approved",
        vector_synced=False,
        vector_index_status="pending",
    )
    for field, value in overrides.items():
        setattr(item, field, value)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


class FailingVectorStore:
    """Wraps a real vector store but makes upsert always raise, to exercise the
    failure-recording path without depending on any real Chroma failure mode."""

    def __init__(self, inner: CatalogItemVectorStore, message="embedding backend down"):
        self.inner = inner
        self.message = message
        self.upsert_calls: list[tuple[int, int]] = []

    def upsert(self, item, widget_id: int) -> None:
        self.upsert_calls.append((item.id, widget_id))
        raise RuntimeError(self.message)

    def contains(self, catalog_item_id: int, widget_id: int) -> bool:
        return self.inner.contains(catalog_item_id, widget_id)


def test_reconcile_rebuilds_a_missing_vector_entry(tmp_path) -> None:
    """The core P0-1 scenario: SQL says `vector_synced` but the underlying Chroma
    entry doesn't actually exist (e.g. an ephemeral-disk wipe on restart)."""
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)

    with session_factory() as session:
        widget = _make_widget(session)
        item = _make_approved_item(
            session, widget, vector_synced=True, vector_index_status="synced"
        )
        assert not vector_store.contains(item.id, widget.id)

        report = reconcile_catalog_index(session, vector_store)

        assert report.scanned == 1
        assert report.rebuilt == 1
        assert report.already_synced == 0
        assert vector_store.contains(item.id, widget.id)
        session.refresh(item)
        assert item.vector_index_status == "synced"
        assert item.vector_synced is True
        assert item.vector_indexed_at is not None


def test_reconcile_records_failure_without_marking_synced(tmp_path) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    real_store = _make_vector_store(settings, tmp_path)
    failing_store = FailingVectorStore(real_store)

    with session_factory() as session:
        widget = _make_widget(session)
        item = _make_approved_item(session, widget)

        report = reconcile_catalog_index(session, failing_store)

        assert report.failed == 1
        assert report.rebuilt == 0
        assert len(report.errors) == 1
        assert "embedding backend down" in report.errors[0]
        session.refresh(item)
        assert item.vector_index_status == "failed"
        assert item.vector_synced is False
        assert item.vector_index_error is not None
        assert "embedding backend down" in item.vector_index_error
        assert item.vector_index_attempts == 1


def test_reconcile_skips_an_item_already_synced_and_present(tmp_path) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)

    with session_factory() as session:
        widget = _make_widget(session)
        item = _make_approved_item(session, widget)
        # First pass actually syncs it.
        reconcile_catalog_index(session, vector_store)
        session.refresh(item)
        assert item.vector_index_status == "synced"

        failing_store = FailingVectorStore(vector_store)
        report = reconcile_catalog_index(session, failing_store)

        assert report.already_synced == 1
        assert report.rebuilt == 0
        assert failing_store.upsert_calls == []  # never re-touched


def test_reconcile_stops_auto_retrying_after_the_attempt_limit(tmp_path) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)
    failing_store = FailingVectorStore(vector_store)

    with session_factory() as session:
        widget = _make_widget(session)
        _make_approved_item(
            session,
            widget,
            vector_index_status="failed",
            vector_index_attempts=MAX_AUTO_RETRY_ATTEMPTS,
        )

        report = reconcile_catalog_index(session, failing_store)

        assert report.skipped_after_max_attempts == 1
        assert report.failed == 0
        assert failing_store.upsert_calls == []


def test_forced_reindex_rebuilds_regardless_of_status_or_attempt_limit(
    tmp_path,
) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)

    with session_factory() as session:
        widget = _make_widget(session)
        already_synced_item = _make_approved_item(
            session, widget, title="Synced", vector_index_status="pending"
        )
        # Sync it for real first, then force should still rebuild it.
        reconcile_catalog_index(session, vector_store)
        session.refresh(already_synced_item)

        over_limit_item = _make_approved_item(
            session,
            widget,
            title="Over limit",
            vector_index_status="failed",
            vector_index_attempts=MAX_AUTO_RETRY_ATTEMPTS + 3,
        )

        report = reconcile_catalog_index(
            session, vector_store, widget_id=widget.id, force=True
        )

        assert report.scanned == 2
        assert report.rebuilt == 2
        assert report.already_synced == 0
        assert report.skipped_after_max_attempts == 0
        session.refresh(over_limit_item)
        assert over_limit_item.vector_index_status == "synced"
        assert over_limit_item.vector_index_attempts == 0


def test_reconcile_never_crosses_widget_boundaries(tmp_path) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)

    with session_factory() as session:
        widget_a = _make_widget(session, "Widget A")
        widget_b = _make_widget(session, "Widget B")
        item_a = _make_approved_item(session, widget_a, title="A Item")
        item_b = _make_approved_item(session, widget_b, title="B Item")

        report = reconcile_catalog_index(session, vector_store, widget_id=widget_a.id)

        assert report.scanned == 1
        assert vector_store.contains(item_a.id, widget_a.id)
        # widget_b's item was never scanned or touched by a widget_a-scoped pass.
        assert not vector_store.contains(item_b.id, widget_b.id)
        session.refresh(item_b)
        assert item_b.vector_index_status == "pending"


def test_reconcile_scans_every_widget_when_unscoped(tmp_path) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)

    with session_factory() as session:
        widget_a = _make_widget(session, "Widget A")
        widget_b = _make_widget(session, "Widget B")
        _make_approved_item(session, widget_a, title="A Item")
        _make_approved_item(session, widget_b, title="B Item")

        report = reconcile_catalog_index(session, vector_store)

        assert report.scanned == 2
        assert report.rebuilt == 2


def test_reconcile_ignores_unapproved_items(tmp_path) -> None:
    session_factory, settings = _make_session_factory(tmp_path)
    vector_store = _make_vector_store(settings, tmp_path)

    with session_factory() as session:
        widget = _make_widget(session)
        _make_approved_item(
            session, widget, title="Pending Review", review_status="pending_review"
        )

        report = reconcile_catalog_index(session, vector_store)

        assert report.scanned == 0


# --- POST /api/admin/widgets/{widget_id}/reindex --------------------------------


def test_reindex_endpoint_requires_admin(client: TestClient, reference_widget) -> None:
    widget, _ = reference_widget
    response = client.post(f"/api/admin/widgets/{widget.id}/reindex")
    assert response.status_code in (401, 403)


def test_reindex_endpoint_rejects_another_tenants_widget(
    client: TestClient, reference_widget
) -> None:
    with client.app.state.session_factory() as session:
        other_tenant = Tenant(name="Other Tenant")
        session.add(other_tenant)
        session.commit()
        session.refresh(other_tenant)
        other_widget, _ = create_widget(session, other_tenant, "Other Widget")
        other_widget_id = other_widget.id

    _login(client)
    response = client.post(f"/api/admin/widgets/{other_widget_id}/reindex")
    assert response.status_code == 404


def test_reindex_endpoint_rebuilds_the_widgets_catalog(
    client: TestClient, reference_widget
) -> None:
    widget, _ = reference_widget
    with client.app.state.session_factory() as session:
        item = _make_approved_item(session, session.get(Widget, widget.id))
        item_id = item.id

    _login(client)
    response = client.post(f"/api/admin/widgets/{widget.id}/reindex")
    assert response.status_code == 200
    body = response.json()
    assert body["scanned"] == 1
    assert body["rebuilt"] == 1
    assert body["failed"] == 0

    with client.app.state.session_factory() as session:
        refreshed = session.get(CatalogItem, item_id)
        assert refreshed.vector_index_status == "synced"
        assert refreshed.vector_synced is True

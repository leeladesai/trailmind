"""M4 (catalog ingestion adapters): feed/API pull (ING-1) and DOM-scrape with
preview/confirm (ING-2), both layered on top of the existing manual-entry adapter
(CAT-1..5) via app/services/ingestion.py. Retrieval-exclusion for a "pending_review"
scrape row is proven by checking it never lands in the vector store rather than by
inspecting agent_graph internals — that's the actual mechanism (see
app/services/catalog.py::create_catalog_item). Updated for the per-widget cutover:
feed/scrape config and catalog scoping all live on a Widget now, not a Tenant (see
Widget's docstring in app/models.py).
"""

import httpx
import pytest

import app.services.ingestion as ingestion_module
from app.models import CatalogItem, Widget
from app.services.ingestion import (
    FeedSyncError,
    ScrapeError,
    scrape_confirm,
    scrape_preview,
    sync_feed,
)
from app.services.widgets import configure_feed, create_widget


class _FakeResponse:
    def __init__(self, *, content: bytes = b"", text: str = "", status_code: int = 200):
        self.content = content
        self.text = text
        self.status_code = status_code
        self.is_redirect = False
        self.headers: dict = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError(f"status {self.status_code}")


FEED_JSON = b"""
{"models": [
    {"title": "Feed Item One", "provider": "Feed Co", "category": "LLM",
     "price": "$1", "description": "From the feed."}
]}
"""

PRODUCT_PAGE_HTML = """
<html><head>
<script type="application/ld+json">
{"@type": "Product", "name": "Scraped Widget", "description": "A widget.",
 "brand": {"name": "Acme"}, "category": "Hardware",
 "offers": {"price": "19.99", "priceCurrency": "USD"},
 "url": "https://example.com/widget"}
</script>
</head><body></body></html>
"""


def _login(client) -> None:
    login = client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    assert login.status_code == 200


def test_feed_sync_imports_rows_tagged_and_stamped(
    client, reference_widget, monkeypatch
) -> None:
    def fake_get(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get)
    widget, _ = reference_widget

    with client.app.state.session_factory() as session:
        widget = session.get(Widget, widget.id)
        configure_feed(session, widget, "https://vendor.example.com/feed.json")
        rows = sync_feed(session, client.app.state.vector_store, widget)
        assert rows[0]["status"] == "inserted"

        item = session.query(CatalogItem).filter_by(title="Feed Item One").one()
        assert item.ingestion_adapter == "feed"
        assert item.review_status == "approved"
        assert item.last_synced_at is not None
        assert item.sync_stale is False
        # Approved feed rows sync to the vector store immediately, same as manual.
        assert item.vector_synced is True


def test_feed_sync_marks_existing_rows_stale_on_failure(
    client, reference_widget, monkeypatch
) -> None:
    def fake_get_ok(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_ok)
    widget_id = reference_widget[0].id

    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget_id)
        configure_feed(session, widget_row, "https://vendor.example.com/feed.json")
        sync_feed(session, client.app.state.vector_store, widget_row)

    def fake_get_fail(url, headers=None, timeout=None, **_kwargs):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_fail)

    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget_id)
        try:
            sync_feed(session, client.app.state.vector_store, widget_row)
            assert False, "expected FeedSyncError"
        except FeedSyncError:
            pass

        item = session.query(CatalogItem).filter_by(title="Feed Item One").one()
        assert item.sync_stale is True


def test_scrape_preview_extracts_json_ld_product(client, monkeypatch) -> None:
    def fake_get(url, timeout=None, follow_redirects=True, **_kwargs):
        return _FakeResponse(text=PRODUCT_PAGE_HTML)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get)

    result = scrape_preview("https://shop.example.com/widget")
    assert result["markup_type"] == "json-ld"
    assert result["rows"][0]["title"] == "Scraped Widget"
    assert result["rows"][0]["provider"] == "Acme"


def test_scrape_preview_raises_when_no_markup_found(client, monkeypatch) -> None:
    def fake_get(url, timeout=None, follow_redirects=True, **_kwargs):
        return _FakeResponse(text="<html><body>Nothing here.</body></html>")

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get)

    try:
        scrape_preview("https://shop.example.com/empty")
        assert False, "expected ScrapeError"
    except ScrapeError:
        pass


def test_scrape_confirm_creates_pending_review_row_excluded_from_vector_store(
    client, reference_widget
) -> None:
    widget, _ = reference_widget
    with client.app.state.session_factory() as session:
        widget = session.get(Widget, widget.id)
        rows = scrape_confirm(
            session,
            client.app.state.vector_store,
            widget,
            "https://shop.example.com/widget",
            "json-ld",
            [
                {
                    "title": "Scraped Widget",
                    "description": "A widget.",
                    "provider": "Acme",
                    "category": "Hardware",
                    "price": "USD 19.99",
                }
            ],
        )
        assert rows[0]["status"] == "pending_review"
        catalog_item_id = rows[0]["catalog_item_id"]
        item = session.get(CatalogItem, catalog_item_id)
        assert item.review_status == "pending_review"
        assert item.ingestion_adapter == "scrape"
        assert item.vector_synced is False

        # Not eligible for retrieval yet — the vector store never got an upsert for it.
        matches = client.app.state.vector_store.query_scored(
            "Scraped Widget Acme Hardware", widget.id, limit=5
        )
        assert catalog_item_id not in {mid for mid, _ in matches}


def test_admin_approve_endpoint_syncs_to_vector_store(client, reference_widget) -> None:
    _login(client)
    widget_id = reference_widget[0].id
    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget_id)
        rows = scrape_confirm(
            session,
            client.app.state.vector_store,
            widget_row,
            "https://shop.example.com/widget",
            "json-ld",
            [
                {
                    "title": "Pending Widget",
                    "description": "Awaiting approval.",
                    "provider": "Acme",
                    "category": "Hardware",
                    "price": "USD 9.99",
                }
            ],
        )
        catalog_item_id = rows[0]["catalog_item_id"]

    response = client.post(
        f"/api/admin/widgets/{widget_id}/catalog-items/{catalog_item_id}/approve"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["review_status"] == "approved"
    assert body["vector_synced"] is True


def test_ingestion_status_reflects_feed_and_scrape_state(
    client, reference_widget, monkeypatch
) -> None:
    _login(client)
    widget, _ = reference_widget

    def fake_get(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get)

    client.post(
        f"/api/admin/widgets/{widget.id}/feed",
        json={"feed_url": "https://vendor.example.com/feed.json"},
    )
    sync_response = client.post(f"/api/admin/widgets/{widget.id}/feed/sync")
    assert sync_response.status_code == 200

    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget.id)
        scrape_confirm(
            session,
            client.app.state.vector_store,
            widget_row,
            "https://shop.example.com/widget",
            "json-ld",
            [
                {
                    "title": "Status Widget",
                    "description": "For status coverage.",
                    "provider": "Acme",
                    "category": "Hardware",
                    "price": "USD 5.00",
                }
            ],
        )

    status = client.get(f"/api/admin/widgets/{widget.id}/ingestion/status")
    assert status.status_code == 200
    body = status.json()
    assert body["feed"]["count"] == 1
    assert body["feed"]["sync_stale"] is False
    assert body["scrape"]["pending_review"] == 1


def test_non_admin_cannot_configure_feed(client, reference_widget) -> None:
    widget, _ = reference_widget
    response = client.post(
        f"/api/admin/widgets/{widget.id}/feed",
        json={"feed_url": "https://vendor.example.com/feed.json"},
    )
    assert response.status_code in (401, 403)


def test_ingestion_status_isolated_per_widget(client, reference_widget) -> None:
    from app.models import Tenant, User
    from app.security import hash_password

    widget, _ = reference_widget
    with client.app.state.session_factory() as session:
        other_tenant = Tenant(name="Other Tenant")
        session.add(other_tenant)
        session.commit()
        session.refresh(other_tenant)
        other_widget, _ = create_widget(session, other_tenant, "Other Widget")
        session.add(
            User(
                tenant_id=other_tenant.id,
                email="other-admin@test.dev",
                password_hash=hash_password("password123"),
                role="admin",
            )
        )
        session.commit()
        other_widget_id = other_widget.id

        widget_row = session.get(Widget, widget.id)
        scrape_confirm(
            session,
            client.app.state.vector_store,
            widget_row,
            "https://shop.example.com/widget",
            "json-ld",
            [
                {
                    "title": "Reference Widget's Item",
                    "description": "Belongs to the reference widget.",
                    "provider": "Acme",
                    "category": "Hardware",
                    "price": "USD 5.00",
                }
            ],
        )

    login = client.post(
        "/api/admin/login",
        json={"email": "other-admin@test.dev", "password": "password123"},
    )
    assert login.status_code == 200
    status = client.get(f"/api/admin/widgets/{other_widget_id}/ingestion/status")
    assert status.json()["scrape"]["pending_review"] == 0


# P0-6 (SSRF guard): feed sync and scrape preview must refuse to fetch
# internal/private targets even though an admin fully controls both inputs — an
# admin account on one tenant is not a trusted operator of the shared server's
# network, and a compromised admin session shouldn't become an SSRF pivot.


def test_feed_sync_rejects_internal_url_without_ever_calling_httpx(
    client, reference_widget, monkeypatch
) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("httpx.get must not be called for an unsafe URL")

    monkeypatch.setattr(ingestion_module.httpx, "get", fail_if_called)
    widget, _ = reference_widget

    with client.app.state.session_factory() as session:
        widget = session.get(Widget, widget.id)
        configure_feed(session, widget, "http://169.254.169.254/latest/meta-data/")
        with pytest.raises(FeedSyncError):
            sync_feed(session, client.app.state.vector_store, widget)


def test_scrape_preview_rejects_internal_url_without_ever_calling_httpx(
    monkeypatch,
) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("httpx.get must not be called for an unsafe URL")

    monkeypatch.setattr(ingestion_module.httpx, "get", fail_if_called)

    with pytest.raises(ScrapeError):
        scrape_preview("http://127.0.0.1:8000/admin")


FEED_JSON_TWO_ITEMS = b"""
{"models": [
    {"title": "Feed Item One", "provider": "Feed Co", "category": "LLM",
     "price": "$1", "description": "From the feed."},
    {"title": "Feed Item Two", "provider": "Feed Co", "category": "LLM",
     "price": "$2", "description": "Also from the feed."}
]}
"""


def test_feed_sync_delists_item_no_longer_present_upstream(
    client, reference_widget, monkeypatch
) -> None:
    widget, _ = reference_widget

    def fake_get_two(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON_TWO_ITEMS)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_two)
    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget.id)
        configure_feed(session, widget_row, "https://vendor.example.com/feed.json")
        sync_feed(session, client.app.state.vector_store, widget_row)

        item_two = session.query(CatalogItem).filter_by(title="Feed Item Two").one()
        assert item_two.vector_synced is True

    def fake_get_one(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_one)
    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget.id)
        sync_feed(session, client.app.state.vector_store, widget_row)

        item_one = session.query(CatalogItem).filter_by(title="Feed Item One").one()
        assert item_one.review_status == "approved"

        item_two = session.query(CatalogItem).filter_by(title="Feed Item Two").one()
        assert item_two.review_status == "delisted"
        assert item_two.vector_synced is False
        assert not client.app.state.vector_store.contains(item_two.id, widget.id)


def test_feed_sync_reactivates_delisted_item_that_reappears(
    client, reference_widget, monkeypatch
) -> None:
    widget, _ = reference_widget

    def fake_get_two(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON_TWO_ITEMS)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_two)
    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget.id)
        configure_feed(session, widget_row, "https://vendor.example.com/feed.json")
        sync_feed(session, client.app.state.vector_store, widget_row)

    def fake_get_one(url, headers=None, timeout=None, **_kwargs):
        return _FakeResponse(content=FEED_JSON)

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_one)
    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget.id)
        sync_feed(session, client.app.state.vector_store, widget_row)
        item_two_id = (
            session.query(CatalogItem).filter_by(title="Feed Item Two").one().id
        )

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get_two)
    with client.app.state.session_factory() as session:
        widget_row = session.get(Widget, widget.id)
        rows = sync_feed(session, client.app.state.vector_store, widget_row)
        reactivated = [row for row in rows if row["title"] == "Feed Item Two"][0]
        assert reactivated["status"] == "reactivated"

        item_two = session.get(CatalogItem, item_two_id)
        assert item_two.review_status == "approved"
        assert item_two.vector_synced is True
        assert client.app.state.vector_store.contains(item_two.id, widget.id)


def test_scrape_preview_revalidates_each_redirect_hop(monkeypatch) -> None:
    """A URL that resolves safely can still redirect to an internal address —
    following it with no re-check would defeat the guard entirely."""

    def fake_get(url, timeout=None, follow_redirects=False, **_kwargs):
        if url == "https://shop.example.com/widget":
            response = _FakeResponse(status_code=302)
            response.is_redirect = True
            response.headers = {"location": "http://169.254.169.254/secret"}
            return response
        raise AssertionError(f"unexpected fetch of {url!r}")

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get)

    with pytest.raises(ScrapeError):
        scrape_preview("https://shop.example.com/widget")

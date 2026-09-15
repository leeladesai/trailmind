"""TEN-3/NFR-8: no read path returns another tenant's data. Builds a second tenant
directly against the same per-test SQLite DB the `client` fixture already seeded with
the reference tenant (id=1 — see tests/conftest.py), then proves tenant A's session
cannot see tenant B's catalog/events/recommendations, and vice versa, through the
public API rather than by inspecting internals directly.

The event-ingestion cross-tenant catalog_item_id guard (dropping a foreign-tenant
catalog_item_id rather than storing it) is covered in tests/test_tracker.py against
the tracker SDK's own POST /api/track/events, which replaced the old cookie-session
POST /api/events/batch. This file's own tracker-key test below covers a different
angle: tenant A's key must never surface tenant B's stored data.
"""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import CatalogItem, Tenant, User
from app.security import hash_password
from app.services.widgets import create_widget


def _make_second_tenant(client: TestClient) -> Tenant:
    with client.app.state.session_factory() as session:
        tenant = Tenant(name="Second Tenant")
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
        widget, _raw_key = create_widget(session, tenant, "Second Widget")

        admin = User(
            tenant_id=tenant.id,
            email="tenant-b-admin@test.dev",
            password_hash=hash_password("password123"),
            role="admin",
        )
        item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Tenant B Only Item",
            provider="Tenant B Provider",
            category="LLM",
            price="$0",
            description="Only visible inside tenant B.",
            use_case_tags=[],
        )
        session.add_all([admin, item])
        session.commit()
        session.refresh(item)
        tenant.only_catalog_item_id = item.id  # stash for the assertions below
        tenant.only_widget_id = widget.id
        return tenant


def test_catalog_list_never_returns_another_tenants_items(
    client: TestClient, reference_widget
) -> None:
    reference_widget_id = reference_widget[0].id
    second_tenant = _make_second_tenant(client)
    # GET /api/admin/widgets/{widget_id}/catalog-items is admin-only, own-widget-only —
    # tenant A's own admin, listing tenant A's own widget's catalog, is what proves the
    # isolation here.
    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )

    response = client.get(f"/api/admin/widgets/{reference_widget_id}/catalog-items")
    assert response.status_code == 200
    titles = {item["title"] for item in response.json()}
    assert "Tenant B Only Item" not in titles

    # Tenant A's admin can't even reach tenant B's widget to look inside it.
    detail = client.get(
        f"/api/admin/widgets/{second_tenant.only_widget_id}/catalog-items/"
        f"{second_tenant.only_catalog_item_id}"
    )
    assert detail.status_code == 404


def test_admin_users_list_never_returns_another_tenants_users(
    client: TestClient,
) -> None:
    _make_second_tenant(client)

    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    response = client.get("/api/admin/users")
    assert response.status_code == 200
    emails = {row["email"] for row in response.json()["users"]}
    assert "tenant-b-admin@test.dev" not in emails


def test_admin_overview_totals_exclude_another_tenants_data(client: TestClient) -> None:
    _make_second_tenant(client)

    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    totals = client.get("/api/admin/overview").json()["totals"]

    # Ground truth computed directly, scoped to the reference tenant only — comparing
    # against this (rather than an absolute expected number, or a before/after delta)
    # is immune to the app's own background demo-seed task racing in in the
    # background and adding more of the *reference* tenant's own users/items mid-test;
    # what actually proves isolation is that the API's totals match a query scoped to
    # tenant A alone, i.e. tenant B's admin/item never entered the count.
    with client.app.state.session_factory() as session:
        reference_tenant = session.scalar(
            select(Tenant).where(Tenant.name == "TrailMind Reference")
        )
        expected_users = session.scalar(
            select(func.count(User.id)).where(User.tenant_id == reference_tenant.id)
        )
        expected_items = session.scalar(
            select(func.count(CatalogItem.id)).where(
                CatalogItem.tenant_id == reference_tenant.id
            )
        )

    assert totals["users"] == expected_users
    assert totals["catalog_items"] == expected_items


def test_widget_key_never_surfaces_another_widgets_recommendation(
    client: TestClient,
) -> None:
    """A visitor_id is just a client-chosen string, not scoped to any widget on its
    own — two widgets' visitors could easily collide on the same id (e.g. both using
    "v-1" from a fresh browser). Isolation must come entirely from the widget key,
    not from visitor_id happening to be unique."""
    with client.app.state.session_factory() as session:
        tenant_a = Tenant(name="Tenant A")
        tenant_b = Tenant(name="Tenant B")
        session.add_all([tenant_a, tenant_b])
        session.commit()
        widget_a, key_a = create_widget(session, tenant_a, "Widget A")
        widget_b, key_b = create_widget(session, tenant_b, "Widget B")
        # /api/recommendations/latest requires an active widget (same policy as
        # every other widget-facing endpoint) — this test is about cross-widget
        # isolation, not onboarding-gating, so activate both up front.
        widget_a.status = "active"
        widget_b.status = "active"
        item_b = CatalogItem(
            tenant_id=tenant_b.id,
            widget_id=widget_b.id,
            title="Widget B Secret Item",
            provider="P",
            category="LLM",
            price="$0",
            description="Only for widget B.",
            use_case_tags=[],
        )
        session.add(item_b)
        session.commit()
        item_b_id = item_b.id

    same_visitor_id = "v-shared"
    client.post(
        "/api/track/events",
        json={
            "widget_key": key_b,
            "visitor_id": same_visitor_id,
            "events": [
                {
                    "event_type": "model_view",
                    "catalog_item_id": item_b_id,
                    "metadata": {},
                }
            ],
        },
    )

    # Widget A, querying with the same visitor_id string, must see nothing of
    # widget B's — the widget key is what scopes this, not the visitor_id value.
    response = client.get(
        f"/api/recommendations/latest?widget_key={key_a}&visitor_id={same_visitor_id}"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert response.json()["evidence"] == []

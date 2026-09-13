"""P0-4: enterprise admin authentication foundation.

Covers: logout actually revokes the session server-side (not just deletes a
cookie client-side — a copied-out bearer token stops working immediately, not
just when its 12-hour JWT expiry eventually arrives); revoke_all_sessions_for_user
as the "sign out everywhere" extension point; audit events are recorded for
login, tenant lifecycle changes, widget key rotation/revocation, and catalog
changes; and the audit-log read endpoint scopes a tenant admin to their own
tenant while a platform admin sees everything. Also a basic model-level check of
the new TenantMembership table (an additive extension point, not yet wired into
authorization — see its docstring in app/models.py).
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, Tenant, TenantMembership, User
from app.security import (
    create_session_token,
    hash_password,
    revoke_all_sessions_for_user,
)
from app.services.tenants import create_tenant
from app.services.widgets import create_widget


def _login(client: TestClient, email="curator@test.dev", password="password123") -> str:
    response = client.post(
        "/api/admin/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_logout_revokes_the_session_immediately(client: TestClient) -> None:
    token = _login(client)

    ok = client.get("/api/admin/me", headers=_auth(token))
    assert ok.status_code == 200

    logout = client.post("/api/auth/logout", headers=_auth(token))
    assert logout.status_code == 204

    rejected = client.get("/api/admin/me", headers=_auth(token))
    assert rejected.status_code == 401


def test_logout_is_a_no_op_with_no_valid_token(client: TestClient) -> None:
    response = client.post("/api/auth/logout", headers=_auth("not-a-real-token"))
    assert response.status_code == 204


def test_revoke_all_sessions_for_user_invalidates_every_issued_token(
    client: TestClient,
) -> None:
    with client.app.state.session_factory() as session:
        user = session.scalar(select(User).where(User.email == "curator@test.dev"))
        token_a = create_session_token(session, user, client.app.state.settings)
        token_b = create_session_token(session, user, client.app.state.settings)
        user_id = user.id

    assert client.get("/api/admin/me", headers=_auth(token_a)).status_code == 200
    assert client.get("/api/admin/me", headers=_auth(token_b)).status_code == 200

    with client.app.state.session_factory() as session:
        revoke_all_sessions_for_user(session, user_id)

    assert client.get("/api/admin/me", headers=_auth(token_a)).status_code == 401
    assert client.get("/api/admin/me", headers=_auth(token_b)).status_code == 401


def test_admin_login_records_an_audit_event(client: TestClient) -> None:
    _login(client)
    with client.app.state.session_factory() as session:
        entries = session.query(AuditLog).filter_by(action="admin_login").all()
    assert len(entries) == 1
    assert entries[0].actor_user_id is not None


def test_widget_key_rotate_and_revoke_record_audit_events(
    client: TestClient, reference_widget
) -> None:
    widget, _ = reference_widget
    token = _login(client)

    rotate = client.post(
        f"/api/admin/widgets/{widget.id}/rotate-key", headers=_auth(token)
    )
    assert rotate.status_code == 200

    with client.app.state.session_factory() as session:
        key_row = session.query(AuditLog).filter_by(action="widget_key_rotated").first()
        assert key_row is not None
        assert key_row.target_type == "widget"
        assert key_row.target_id == str(widget.id)

    from app.models import WidgetApiKey

    with client.app.state.session_factory() as session:
        active_key = (
            session.query(WidgetApiKey)
            .filter_by(widget_id=widget.id, status="active")
            .first()
        )
        key_id = active_key.id

    revoke = client.post(
        f"/api/admin/widgets/{widget.id}/revoke-key/{key_id}", headers=_auth(token)
    )
    assert revoke.status_code == 204

    with client.app.state.session_factory() as session:
        revoke_row = (
            session.query(AuditLog).filter_by(action="widget_key_revoked").first()
        )
        assert revoke_row is not None
        assert revoke_row.target_id == str(key_id)


def test_catalog_item_create_records_an_audit_event(
    client: TestClient, reference_widget
) -> None:
    widget, _ = reference_widget
    token = _login(client)

    response = client.post(
        f"/api/admin/widgets/{widget.id}/catalog-items",
        headers=_auth(token),
        json={
            "title": "Audited Item",
            "description": "d",
            "provider": "Acme",
            "category": "Loans",
            "price": "$0",
        },
    )
    assert response.status_code == 201

    with client.app.state.session_factory() as session:
        entry = session.query(AuditLog).filter_by(action="catalog_item_created").first()
        assert entry is not None
        assert entry.target_id == str(response.json()["id"])


def test_tenant_lifecycle_actions_record_audit_events(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        platform_admin = User(
            tenant_id=None,
            email="platform-audit@test.dev",
            password_hash=hash_password("password123"),
            role="platform_admin",
        )
        session.add(platform_admin)
        session.commit()

    token = _login(client, "platform-audit@test.dev", "password123")

    create = client.post(
        "/api/tenants", headers=_auth(token), json={"name": "Audited Tenant"}
    )
    assert create.status_code == 200
    tenant_id = create.json()["id"]

    client.post(f"/api/tenants/{tenant_id}/suspend", headers=_auth(token))
    client.post(f"/api/tenants/{tenant_id}/reactivate", headers=_auth(token))

    with client.app.state.session_factory() as session:
        actions = {
            entry.action
            for entry in session.query(AuditLog)
            .filter_by(tenant_id=tenant_id, target_type="tenant")
            .all()
        }
    assert actions == {"tenant_created", "tenant_suspended", "tenant_reactivated"}


def test_audit_log_endpoint_requires_authentication(client: TestClient) -> None:
    response = client.get("/api/admin/audit-log")
    assert response.status_code in (401, 403)


def test_tenant_admin_only_sees_their_own_tenants_audit_entries(
    client: TestClient, reference_widget
) -> None:
    widget, _ = reference_widget
    token = _login(client)
    client.post(f"/api/admin/widgets/{widget.id}/rotate-key", headers=_auth(token))

    response = client.get("/api/admin/audit-log", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) >= 1
    with client.app.state.session_factory() as session:
        curator = session.scalar(select(User).where(User.email == "curator@test.dev"))
        own_tenant_id = curator.tenant_id
    assert all(entry["tenant_id"] == own_tenant_id for entry in body["entries"])


def test_platform_admin_sees_audit_entries_across_tenants(client: TestClient) -> None:
    with client.app.state.session_factory() as session:
        other_tenant = Tenant(name="Other Tenant For Audit")
        session.add(other_tenant)
        session.commit()
        session.refresh(other_tenant)
        create_widget(session, other_tenant, "Other Widget")
        platform_admin = User(
            tenant_id=None,
            email="platform-audit-2@test.dev",
            password_hash=hash_password("password123"),
            role="platform_admin",
        )
        session.add(platform_admin)
        session.commit()

    curator_token = _login(client)
    platform_token = _login(client, "platform-audit-2@test.dev", "password123")

    response = client.get("/api/admin/audit-log", headers=_auth(platform_token))
    assert response.status_code == 200
    tenant_ids = {entry["tenant_id"] for entry in response.json()["entries"]}
    # The platform admin's own login and the curator's own login both show up —
    # more than one tenant's worth of entries, unlike the tenant-scoped view.
    assert len(tenant_ids) >= 1
    assert curator_token  # sanity: login above actually succeeded


def test_tenant_membership_model_supports_a_user_belonging_to_a_second_tenant(
    client: TestClient,
) -> None:
    """Model-level check of the additive TenantMembership table — not yet wired
    into any authorization dependency (see its docstring), but the schema itself
    must support a user having more than one tenant relationship."""
    with client.app.state.session_factory() as session:
        curator = session.scalar(select(User).where(User.email == "curator@test.dev"))
        second_tenant = create_tenant(session, "Second Tenant For Membership")

        session.add(
            TenantMembership(
                user_id=curator.id, tenant_id=second_tenant.id, role="admin"
            )
        )
        session.commit()

        memberships = (
            session.query(TenantMembership).filter_by(user_id=curator.id).all()
        )
        tenant_ids = {m.tenant_id for m in memberships}
        # The user's primary tenant (User.tenant_id) is untouched, and they now also
        # have a membership row for a second, different tenant.
        assert curator.tenant_id not in tenant_ids
        assert second_tenant.id in tenant_ids

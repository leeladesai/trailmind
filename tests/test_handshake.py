from fastapi.testclient import TestClient

from app.security import hash_password
from app.services.tenants import get_or_create_reference_tenant
from app.models import User

# This module used to hit the real app.asgi singleton (built from real .env
# settings, including DATABASE_URL) rather than an isolated per-test db — that made
# pytest *collection* itself capable of opening a connection to a live Neon database
# the moment this file was imported. It now uses the same hermetic `client` fixture
# (sqlite + local chroma, no .env) as every other test module. See P0-5 / hermetic
# test execution and tests/test_hermetic.py for the guard that catches a regression.


def test_health_endpoint(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "trailmind"}


def test_admin_login_returns_a_bearer_token_not_html(client: TestClient) -> None:
    # The admin console is the separate React app (frontend/) now — this backend is
    # a pure JSON API and no longer serves any admin HTML of its own. Admins can't
    # self-register (AUTH-5), so seed a dedicated admin directly in this test's own
    # isolated db rather than relying on `seed_data.py` having already been run.
    email, password = "handshake-admin@test.dev", "handshake-admin-pw"
    with client.app.state.session_factory() as session:
        tenant = get_or_create_reference_tenant(session)
        session.add(
            User(
                tenant_id=tenant.id,
                email=email,
                password_hash=hash_password(password),
                role="admin",
            )
        )
        session.commit()

    login = client.post("/api/admin/login", json={"email": email, "password": password})
    assert login.status_code == 200
    assert login.json()["token"]

    response = client.get(
        "/api/admin/me",
        headers={"Authorization": f"Bearer {login.json()['token']}"},
    )
    assert response.status_code == 200
    assert response.json()["email"] == email


def test_admin_html_routes_are_gone(client: TestClient) -> None:
    # These used to be server-rendered Jinja2 pages — the backend no longer serves
    # any HTML at all, admin or otherwise, now that the console is a separate app.
    for path in (
        "/admin",
        "/admin/login",
        "/admin/models",
        "/admin/observability",
        "/admin/users",
        "/admin/tenants",
    ):
        assert client.get(path, follow_redirects=False).status_code == 404


def test_removed_ai_engineer_routes_are_gone(client: TestClient) -> None:
    # The AI-engineer self-service catalog/dashboard/activity/login surface was
    # removed as part of the platform pivot (docs/design/09-Platform-Pivot-Decision.md)
    # — these must not still resolve to a page or an API response.
    for path in (
        "/",
        "/login",
        "/catalog",
        "/models/1",
        "/compare",
        "/dashboard",
        "/activity",
    ):
        assert client.get(path, follow_redirects=False).status_code == 404
    for path in ("/api/auth/register", "/api/auth/login"):
        assert client.post(path, json={}).status_code == 404
    for path in ("/api/auth/me", "/api/recommendations/me", "/api/activity/me"):
        assert client.get(path).status_code == 404

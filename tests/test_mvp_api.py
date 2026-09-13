from fastapi.testclient import TestClient

from app.models import User
from app.security import create_session_token, hash_password


def _make_user(client: TestClient, email: str, role: str = "user") -> User:
    """Non-admin accounts have no self-registration path any more (the AI-engineer
    login/register surface was removed — docs/design/09-Platform-Pivot-Decision.md).
    Creating one directly against the reference tenant (id=1, seeded by the `client`
    fixture) and minting its session cookie the same way login used to is how these
    tests still exercise "a signed-in non-admin" without going through a live endpoint.
    """
    with client.app.state.session_factory() as session:
        user = User(
            tenant_id=1,
            email=email,
            password_hash=hash_password("password123"),
            role=role,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        token = create_session_token(session, user, client.app.state.settings)
    client.cookies.set(client.app.state.settings.session_cookie_name, token)
    return user


def test_admin_can_create_item_and_dual_write(
    client: TestClient, reference_widget
) -> None:
    widget_id = reference_widget[0].id
    login = client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    assert login.status_code == 200

    create = client.post(
        f"/api/admin/widgets/{widget_id}/catalog-items",
        json={
            "title": "Test Voice",
            "description": "A low latency voice model for agents.",
            "provider": "Test Labs",
            "category": "Voice",
            "price": "$0.001/char",
            "specs": {"Latency": "~120ms"},
            "use_case_tags": ["real-time voice"],
        },
    )
    assert create.status_code == 201
    assert create.json()["vector_synced"] is True
    assert (
        client.get(
            f"/api/admin/widgets/{widget_id}/catalog-items?category=Voice"
        ).json()[0]["title"]
        == "Test Voice"
    )


def test_admin_can_bulk_upload_csv_catalog(
    client: TestClient, reference_widget
) -> None:
    widget_id = reference_widget[0].id
    login = client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    assert login.status_code == 200

    csv_content = (
        "title,provider,category,price,description,use_case_tags\n"
        "Bulk Voice,Test Labs,Voice,$0.001/char,A voice model.,real-time;support\n"
        "Bulk Voice,Test Labs,Voice,$0.001/char,Duplicate of the row above.,\n"
        ",Test Labs,LLM,$1,Missing a title so this row is invalid.,\n"
    )
    response = client.post(
        f"/api/admin/widgets/{widget_id}/catalog-items/bulk-upload",
        files={"file": ("catalog.csv", csv_content, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["inserted"] == 1
    assert body["skipped_duplicate"] == 1
    assert body["invalid"] == 1
    assert (
        client.get(f"/api/admin/widgets/{widget_id}/catalog-items?q=Bulk Voice").json()[
            0
        ]["title"]
        == "Bulk Voice"
    )


def test_bulk_upload_rejects_malformed_file(
    client: TestClient, reference_widget
) -> None:
    widget_id = reference_widget[0].id
    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    response = client.post(
        f"/api/admin/widgets/{widget_id}/catalog-items/bulk-upload",
        files={"file": ("catalog.json", "{not json", "application/json")},
    )
    assert response.status_code == 400


def test_non_admin_cannot_bulk_upload_catalog(
    client: TestClient, reference_widget
) -> None:
    widget_id = reference_widget[0].id
    _make_user(client, "bulk-user@test.dev")
    response = client.post(
        f"/api/admin/widgets/{widget_id}/catalog-items/bulk-upload",
        files={"file": ("catalog.csv", "title\n", "text/csv")},
    )
    assert response.status_code == 403


def test_session_cookie_secure_flag_follows_settings(tmp_path) -> None:
    """A public HTTPS deployment (render.yaml sets SESSION_COOKIE_SECURE=true) must
    never send the session cookie over plain HTTP — local dev keeps the default False
    so http://localhost still works unchanged."""
    from app.config import Settings
    from app.main import create_app
    from app.services.tenants import get_or_create_reference_tenant

    def _seed_admin(app):
        with app.state.session_factory() as session:
            tenant = get_or_create_reference_tenant(session)
            session.add(
                User(
                    tenant_id=tenant.id,
                    email="cookie-admin@test.dev",
                    password_hash=hash_password("password123"),
                    role="admin",
                )
            )
            session.commit()

    insecure_settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'insecure.db'}",
        chroma_db_path=str(tmp_path / "insecure-chroma"),
        secret_key="test-secret",
        mesh_api_key=None,
        langsmith_api_key=None,
        session_cookie_secure=False,
    )
    insecure_app = create_app(insecure_settings)
    _seed_admin(insecure_app)
    insecure_client = TestClient(insecure_app)
    response = insecure_client.post(
        "/api/admin/login",
        json={"email": "cookie-admin@test.dev", "password": "password123"},
    )
    assert "secure" not in response.headers["set-cookie"].lower()

    secure_settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'secure.db'}",
        chroma_db_path=str(tmp_path / "secure-chroma"),
        secret_key="test-secret",
        mesh_api_key=None,
        langsmith_api_key=None,
        session_cookie_secure=True,
    )
    secure_app = create_app(secure_settings)
    _seed_admin(secure_app)
    secure_client = TestClient(secure_app)
    response = secure_client.post(
        "/api/admin/login",
        json={"email": "cookie-admin@test.dev", "password": "password123"},
    )
    assert "secure" in response.headers["set-cookie"].lower()


def test_admin_can_list_users(client: TestClient) -> None:
    _make_user(client, "listed-user@test.dev")
    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )

    response = client.get("/api/admin/users")
    assert response.status_code == 200
    body = response.json()
    assert "has_more" in body
    emails = {user["email"] for user in body["users"]}
    assert "listed-user@test.dev" in emails
    assert "curator@test.dev" in emails
    listed = next(u for u in body["users"] if u["email"] == "listed-user@test.dev")
    assert listed["role"] == "user"
    assert listed["telegram_chat_id"] is None
    assert "created_at" in listed


def test_admin_users_pagination(client: TestClient) -> None:
    for i in range(3):
        _make_user(client, f"page-user-{i}@test.dev")
    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )

    first_page = client.get("/api/admin/users?limit=2&offset=0")
    body = first_page.json()
    assert len(body["users"]) == 2
    assert body["has_more"] is True

    all_seen = client.get("/api/admin/users?limit=100&offset=0").json()["users"]
    assert len(all_seen) >= 4  # curator + the 3 created above


def test_non_admin_and_anonymous_cannot_list_users(client: TestClient) -> None:
    anonymous = TestClient(client.app).get("/api/admin/users")
    assert anonymous.status_code == 401

    _make_user(client, "nosee@test.dev")
    response = client.get("/api/admin/users")
    assert response.status_code == 403


def test_admin_can_delete_a_user(client: TestClient) -> None:
    target = _make_user(client, "deleteme@test.dev")

    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    response = client.delete(f"/api/admin/users/{target.id}")
    assert response.status_code == 204

    with client.app.state.session_factory() as session:
        assert session.get(User, target.id) is None

    emails = {u["email"] for u in client.get("/api/admin/users").json()["users"]}
    assert "deleteme@test.dev" not in emails


def test_admin_cannot_delete_own_account(client: TestClient) -> None:
    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    with client.app.state.session_factory() as session:
        curator = session.query(User).filter(User.email == "curator@test.dev").one()
        curator_id = curator.id

    response = client.delete(f"/api/admin/users/{curator_id}")
    assert response.status_code == 400


def test_delete_user_returns_404_for_unknown_id(client: TestClient) -> None:
    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    response = client.delete("/api/admin/users/999999")
    assert response.status_code == 404


def test_non_admin_cannot_delete_users(client: TestClient) -> None:
    _make_user(client, "nodelete@test.dev")
    response = client.delete("/api/admin/users/1")
    assert response.status_code == 403


def test_non_admin_cannot_manage_catalog_items(
    client: TestClient, reference_widget
) -> None:
    widget_id = reference_widget[0].id
    _make_user(client, "plain-user@test.dev")

    response = client.post(
        f"/api/admin/widgets/{widget_id}/catalog-items",
        json={
            "title": "Blocked",
            "description": "Should not be created.",
            "provider": "Test Labs",
            "category": "LLM",
            "price": "$1",
        },
    )
    assert response.status_code == 403


def test_observability_costs_requires_admin_and_returns_empty_rollup(
    client: TestClient,
) -> None:
    _make_user(client, "cost-user@test.dev")
    blocked = client.get("/api/admin/observability/costs")
    assert blocked.status_code == 403

    client.post(
        "/api/admin/login",
        json={"email": "curator@test.dev", "password": "password123"},
    )
    response = client.get("/api/admin/observability/costs")
    assert response.status_code == 200
    body = response.json()
    assert body["call_count"] == 0
    assert body["avg_latency_ms"] is None
    assert body["total_cost_usd"] is None
    assert body["recent"] == []

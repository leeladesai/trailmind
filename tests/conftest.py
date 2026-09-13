import os
import socket
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import User, Widget
from app.security import hash_password
from app.services.tenants import get_or_create_reference_tenant
from app.services.widgets import DEFAULT_WIDGET_NAME, create_widget

# configure_langsmith (app/services/tracing.py) mutates these process-global env vars
# with no cleanup — a test that enables tracing would otherwise leak it into every test
# that runs after it in the same pytest process, regardless of that test's own Settings.
# tests/test_handshake.py intentionally imports the real app.asgi singleton (built from
# real .env settings), and that import happens during pytest's *collection* phase —
# before any test or fixture has run — so a per-test "snapshot before, restore after"
# fixture isn't enough: the very first test would already see a polluted "before" state.
# Capturing the snapshot here, at conftest's own import time, is the earliest point that
# is still guaranteed to run before any test module gets imported.
_LANGSMITH_ENV_VARS = (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_PROJECT",
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
)
_PRISTINE_LANGSMITH_ENV = {key: os.environ.get(key) for key in _LANGSMITH_ENV_VARS}


def _restore_pristine_langsmith_env() -> None:
    for key, value in _PRISTINE_LANGSMITH_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _isolate_langsmith_env() -> Iterator[None]:
    _restore_pristine_langsmith_env()
    try:
        yield
    finally:
        _restore_pristine_langsmith_env()


# P0-5: the standard suite must never need a live database, Mesh, LangSmith, SMTP,
# Telegram, or any other external network call — TestClient's ASGI transport never
# opens a real socket for the app itself, so any socket connect attempt seen during
# the run can only come from a service-integration code path actually reaching out.
# Blocking non-loopback connects for the whole session turns "the suite happens not
# to need the network" into "the suite provably cannot use the network."
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_real_socket_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"Blocked outbound network connection to {address!r} during tests — "
            "the standard suite must be hermetic (see tests/test_hermetic.py)."
        )
    return _real_socket_connect(self, address)


@pytest.fixture(autouse=True, scope="session")
def _block_external_network() -> Iterator[None]:
    socket.socket.connect = _guarded_connect
    try:
        yield
    finally:
        socket.socket.connect = _real_socket_connect


@pytest.fixture()
def client(tmp_path) -> Iterator[TestClient]:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        chroma_db_path=str(tmp_path / "chroma"),
        secret_key="test-secret",
        mesh_api_key=None,
        # Tests must never depend on whatever's in the developer's local .env — a real
        # LANGSMITH_API_KEY there would otherwise make every traced pipeline run in the
        # suite fire real network calls to LangSmith (see _isolate_langsmith_env above).
        langsmith_api_key=None,
    )
    test_app = create_app(settings)
    with test_app.state.session_factory() as session:
        # The reference tenant is resolved get-or-create by name
        # (app/services/tenants.py), so seeding it explicitly here — rather than
        # relying on the app's own fire-and-forget background seed task, which hasn't
        # necessarily run yet — keeps this fixture deterministic: the reference tenant
        # is always id=1 in every test's fresh per-test SQLite file.
        tenant = get_or_create_reference_tenant(session)
        session.add(
            User(
                tenant_id=tenant.id,
                email="curator@test.dev",
                password_hash=hash_password("password123"),
                role="admin",
            )
        )
        session.add(
            User(
                tenant_id=None,
                email="platform-admin@test.dev",
                password_hash=hash_password("password123"),
                role="platform_admin",
            )
        )
        session.commit()
    with TestClient(test_app) as test_client:
        yield test_client


@pytest.fixture()
def reference_widget(client: TestClient) -> tuple[Widget, str]:
    """The per-widget key cutover (see Widget's docstring in app/models.py) means
    almost every tracker/widget-facing test now needs a real Widget + raw API key,
    not just a tenant — this is the widget-level counterpart to `client`'s own
    reference-tenant + curator-admin setup. Named "Default" (DEFAULT_WIDGET_NAME) so
    it lines up with what `get_or_create_default_widget` would create if a test path
    ever falls back to that instead."""
    with client.app.state.session_factory() as session:
        tenant = get_or_create_reference_tenant(session)
        widget, raw_key = create_widget(session, tenant, DEFAULT_WIDGET_NAME)
        # issue_api_key's own commit (inside create_widget) expires widget's
        # attributes without refreshing them — expunge would otherwise hand back an
        # object that raises DetachedInstanceError on the very next attribute read.
        session.refresh(widget)
        session.expunge(widget)
        return widget, raw_key

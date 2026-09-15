import socket

import pytest
from fastapi.testclient import TestClient


def test_external_network_connections_are_blocked() -> None:
    """Proves the session-wide guard in conftest.py is active: any attempt to open a
    real socket to a non-loopback host raises instead of silently succeeding. This is
    what makes "the suite didn't happen to call out" into "the suite cannot call out."
    """
    with pytest.raises(RuntimeError, match="Blocked outbound network connection"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))


def test_loopback_connections_still_work() -> None:
    """The guard must not break local-only traffic (e.g. a local dev server) — only
    non-loopback destinations are blocked."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_socket.connect(("127.0.0.1", port))
    finally:
        client_socket.close()
        server.close()


def test_app_does_not_use_real_env_database(client: TestClient) -> None:
    """The `client` fixture's Settings must be the isolated sqlite/tmp_path ones, not
    whatever DATABASE_URL a developer's real .env happens to define."""
    engine = client.app.state.session_factory().get_bind()
    assert str(engine.url).startswith("sqlite:///")
    assert "trailmind.db" not in str(engine.url)


def test_app_does_not_read_real_langsmith_or_mesh_credentials(
    client: TestClient,
) -> None:
    """Even if the developer's real .env sets LANGSMITH_API_KEY / MESH_API_KEY, the
    isolated test app must not have picked them up — otherwise traced pipeline runs
    or narrative generation could fire real network calls during the suite."""
    assert client.app.state.settings.mesh_api_key is None
    assert client.app.state.settings.langsmith_api_key is None


def test_dotenv_file_presence_does_not_leak_into_test_settings(tmp_path) -> None:
    """Belt-and-suspenders for P0-5: constructing Settings() with no override reads
    the real .env if one exists at the repo root. This test does not assert anything
    about the developer's actual .env (that would be non-hermetic in the other
    direction); it exists purely to document that tests must always pass explicit
    Settings(...) overrides (as the `client` fixture does) rather than relying on
    the bare `Settings()` default in any new test.
    """
    from app.config import Settings

    # A no-argument Settings() is only safe to construct (not to *use*) in tests —
    # this assertion just proves the class still supports being overridden fully.
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'isolated.db'}",
        mesh_api_key=None,
        langsmith_api_key=None,
    )
    assert settings.database_url == f"sqlite:///{tmp_path / 'isolated.db'}"
    assert settings.mesh_api_key is None
    assert settings.langsmith_api_key is None

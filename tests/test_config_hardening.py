"""P0-6: refuse to boot with APP_ENV=production and the default SECRET_KEY still
in place — that value ships in this repo's source, so leaving it unset in a real
deployment would mean admin JWTs are signed with a publicly known key.
"""

import pytest

from app.config import DEFAULT_SECRET_KEY, Settings
from app.main import create_app


def test_production_with_default_secret_key_refuses_to_start(tmp_path) -> None:
    settings = Settings(
        app_env="production",
        secret_key=DEFAULT_SECRET_KEY,
        database_url=f"sqlite:///{tmp_path/'db.sqlite3'}",
        chroma_db_path=str(tmp_path / "chroma"),
    )
    with pytest.raises(RuntimeError):
        create_app(settings)


def test_production_with_a_real_secret_key_starts_fine(tmp_path) -> None:
    settings = Settings(
        app_env="production",
        secret_key="a-real-randomly-generated-secret",
        database_url=f"sqlite:///{tmp_path/'db.sqlite3'}",
        chroma_db_path=str(tmp_path / "chroma"),
    )
    create_app(settings)


def test_development_with_default_secret_key_is_allowed(tmp_path) -> None:
    settings = Settings(
        app_env="development",
        secret_key=DEFAULT_SECRET_KEY,
        database_url=f"sqlite:///{tmp_path/'db.sqlite3'}",
        chroma_db_path=str(tmp_path / "chroma"),
    )
    create_app(settings)

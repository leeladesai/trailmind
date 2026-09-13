"""P0-2: versioned database migrations.

Covers: a fresh database is bootstrapped and stamped at the current Alembic head
(not a stale fixed revision — see app/db.py's _stamp_alembic_head docstring for
why); a representative pre-widget legacy database is upgraded (rename + column
add/drop + data backfill) and then stamped at head; once stamped, startup never
re-runs the hand-rolled path; and `alembic upgrade head` on the CLI reaches the
same head on a brand-new database.
"""

import os
import sqlite3
import subprocess
import sys

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from app.config import Settings
from app.db import BASELINE_REVISION, build_session_factory

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)
_ALEMBIC_INI = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alembic.ini"
)


def _current_head() -> str:
    config = AlembicConfig(_ALEMBIC_INI)
    config.set_main_option("script_location", _MIGRATIONS_DIR)
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


def _table_names(database_path) -> set[str]:
    conn = sqlite3.connect(database_path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


def test_baseline_revision_constant_is_the_first_migration_in_the_chain() -> None:
    """BASELINE_REVISION is kept as a stable named reference (e.g. for docs/tests
    that care specifically about the original schema), even though a bootstrapped
    database is now stamped at head rather than at this revision."""
    config = AlembicConfig(_ALEMBIC_INI)
    config.set_main_option("script_location", _MIGRATIONS_DIR)
    script = ScriptDirectory.from_config(config)
    root_revisions = [rev.revision for rev in script.get_revisions("base")]
    assert BASELINE_REVISION not in root_revisions  # "base" sentinel, not a real one
    first_migration = script.get_revision(BASELINE_REVISION)
    assert first_migration.down_revision is None


def test_fresh_database_is_bootstrapped_and_stamped_at_head(tmp_path) -> None:
    db_path = tmp_path / "fresh.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")

    build_session_factory(settings)

    tables = _table_names(db_path)
    assert "alembic_version" in tables
    assert "catalog_items" in tables
    assert "widgets" in tables

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(catalog_items)")}
        (stamped,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    # The columns from every migration up to head must actually be present — not
    # just the original baseline's — since create_all always builds from *current*
    # models.py, not from whatever the baseline migration alone describes.
    assert "vector_index_status" in columns
    assert stamped == _current_head()


def test_stamped_database_skips_the_legacy_path_on_next_startup(tmp_path) -> None:
    db_path = tmp_path / "already_stamped.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")

    build_session_factory(settings)
    # A second call must be a no-op with respect to the legacy hand-rolled
    # migration — it should not raise (re-running DROP COLUMN on an already-gone
    # column would error) and the app must still be fully usable afterward.
    factory = build_session_factory(settings)

    with factory() as session:
        inspector = inspect(session.get_bind())
        assert "catalog_items" in inspector.get_table_names()


def test_legacy_pre_widget_schema_is_upgraded_and_stamped(tmp_path) -> None:
    """Simulates a real pre-widget-cutover production database: the old `models`
    table (not yet renamed to `catalog_items`), AI-model-specific columns instead
    of category/specs, model_id/model_ids instead of catalog_item_id/
    catalog_item_ids, a NOT NULL legacy user_id, and no widgets table at all."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE tenants (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'active',
                max_agent_runs_per_hour INTEGER NOT NULL DEFAULT 1000,
                allowed_origins TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP
            );
            INSERT INTO tenants (id, name) VALUES (1, 'Legacy Tenant');

            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'admin',
                created_at TIMESTAMP
            );

            CREATE TABLE models (
                id INTEGER PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                modality VARCHAR(60),
                latency_ms INTEGER,
                context_window VARCHAR(60),
                vector_synced BOOLEAN DEFAULT 0,
                created_at TIMESTAMP
            );
            INSERT INTO models (id, title, modality, latency_ms, context_window)
            VALUES (1, 'Legacy Item', 'text', 250, '8k tokens');

            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                model_id INTEGER,
                user_id INTEGER NOT NULL DEFAULT 0,
                event_type VARCHAR(60),
                created_at TIMESTAMP
            );

            CREATE TABLE recommendations (
                id INTEGER PRIMARY KEY,
                model_ids TEXT,
                user_id INTEGER NOT NULL DEFAULT 0,
                narrative TEXT,
                created_at TIMESTAMP
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    settings = Settings(database_url=f"sqlite:///{db_path}")
    build_session_factory(settings)

    tables = _table_names(db_path)
    assert "models" not in tables
    assert "catalog_items" in tables
    assert "widgets" in tables
    assert "alembic_version" in tables

    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(catalog_items)")}
        assert "category" in columns
        assert "specs" in columns
        assert "modality" not in columns
        assert "latency_ms" not in columns

        item_row = conn.execute(
            "SELECT category FROM catalog_items WHERE title = 'Legacy Item'"
        ).fetchone()
        assert item_row[0] == "text"

        event_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "catalog_item_id" in event_columns
        assert "model_id" not in event_columns
        assert "user_id" not in event_columns

        rec_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(recommendations)")
        }
        assert "catalog_item_ids" in rec_columns
        assert "model_ids" not in rec_columns

        (stamped,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert stamped == _current_head()
    finally:
        conn.close()


def test_alembic_upgrade_head_reaches_head_on_a_fresh_database(tmp_path) -> None:
    """Proves the documented deploy-time command (`alembic upgrade head`) actually
    works end to end against a brand-new database, independent of the app's own
    startup bootstrap."""
    db_path = tmp_path / "cli_upgrade.db"
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db_path}")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    tables = _table_names(db_path)
    assert "catalog_items" in tables

    conn = sqlite3.connect(db_path)
    try:
        (stamped,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert stamped == _current_head()

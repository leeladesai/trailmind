import json
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic import command as alembic_command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
# The baseline migration (migrations/versions/0001_baseline_schema.py) is exactly
# the schema the hand-rolled path below produces — see BASELINE_REVISION docstring.
BASELINE_REVISION = "a4bb30b5eed6"


class Base(DeclarativeBase):
    pass


def _add_columns_if_missing(engine, inspector, table: str, additions: dict) -> None:
    existing = {col["name"] for col in inspector.get_columns(table)}
    with engine.begin() as conn:
        for column, column_type in additions.items():
            if column not in existing:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                )


def _rename_table_if_needed(engine, inspector, old: str, new: str) -> None:
    """Must run BEFORE `create_all` — `create_all` only creates tables that don't
    exist yet, so if `new` were created first this rename would find it already
    there and silently skip, stranding all of `old`'s data under the old name."""
    table_names = inspector.get_table_names()
    if old in table_names and new not in table_names:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {old} RENAME TO {new}"))


def _rename_column_if_needed(engine, inspector, table: str, old: str, new: str) -> None:
    if table not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    if old in existing and new not in existing:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))


def _rename_legacy_tables_and_columns(engine) -> None:
    """The CatalogItem generalization (docs/design/09-Platform-Pivot-Decision.md):
    `models` -> `catalog_items`, plus the FK-style fields on other tables that
    referenced it by the old name. Must run before `create_all` — see
    `_rename_table_if_needed`'s docstring. Column renames are safe either side of
    `create_all` (idempotent, no-op once already renamed) but are kept alongside the
    table rename for one clear "legacy naming" migration step."""
    inspector = inspect(engine)
    _rename_table_if_needed(engine, inspector, "models", "catalog_items")
    inspector = inspect(engine)  # re-inspect: the rename above changed table_names
    _rename_column_if_needed(engine, inspector, "events", "model_id", "catalog_item_id")
    _rename_column_if_needed(
        engine, inspector, "recommendations", "model_ids", "catalog_item_ids"
    )


def _migrate_catalog_item_specs(engine, inspector) -> None:
    """`modality` (a fixed AI-model enum) and `latency_ms`/`context_window` (two
    AI-model-specific typed columns) generalized to `category` (free text) and
    `specs` (arbitrary label/value JSON) — see CatalogItem's docstring. Backfills
    from the old columns for any pre-existing rows, then drops them; every step is
    guarded so re-running this after it's already applied is a no-op."""
    if "catalog_items" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("catalog_items")}

    if "category" not in existing or "specs" not in existing:
        _add_columns_if_missing(
            engine,
            inspector,
            "catalog_items",
            {"category": "VARCHAR(60)", "specs": "JSON DEFAULT '{}'"},
        )
        if "modality" in existing:
            with engine.begin() as conn:
                conn.execute(text("UPDATE catalog_items SET category = modality"))
        if "latency_ms" in existing or "context_window" in existing:
            with engine.begin() as conn:
                rows = conn.execute(
                    text("SELECT id, latency_ms, context_window FROM catalog_items")
                ).fetchall()
                for row_id, latency_ms, context_window in rows:
                    specs: dict[str, str] = {}
                    if latency_ms is not None:
                        specs["Latency"] = f"~{latency_ms}ms"
                    if context_window:
                        specs["Context"] = context_window
                    if specs:
                        conn.execute(
                            text(
                                "UPDATE catalog_items SET specs = :specs WHERE id = :id"
                            ),
                            {"specs": json.dumps(specs), "id": row_id},
                        )

    for legacy_column in ("modality", "latency_ms", "context_window"):
        if legacy_column in existing:
            with engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE catalog_items DROP COLUMN {legacy_column}")
                )


def _drop_legacy_user_id(engine, inspector, table: str) -> None:
    """The pre-pivot AI-engineer cookie-session `user_id` FK, left over on some
    deployments as a NOT NULL column — removed along with that login surface
    (docs/design/09-Platform-Pivot-Decision.md); rows are keyed by anonymous
    `visitor_id` now, not a `User` row, so a leftover NOT NULL `user_id` rejects
    every new insert with no code path that could ever populate it."""
    existing = {col["name"] for col in inspector.get_columns(table)}
    if "user_id" in existing:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN user_id"))


def _add_missing_columns(engine) -> None:
    """`create_all` only creates missing tables, not missing columns on tables that
    already exist. There's no Alembic in this MVP, so patch columns added after a
    table's first deploy with a plain idempotent ALTER TABLE (SQLite/Postgres both
    support this syntax)."""
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    _migrate_catalog_item_specs(engine, inspector)

    if "catalog_items" in table_names:
        _add_columns_if_missing(
            engine,
            inspector,
            "catalog_items",
            {
                "story": "TEXT",
                "tenant_id": "INTEGER",
                "widget_id": "INTEGER",
                "ingestion_adapter": "VARCHAR(20) DEFAULT 'manual'",
                "review_status": "VARCHAR(20) DEFAULT 'approved'",
                "last_synced_at": "TIMESTAMP",
                "sync_stale": "BOOLEAN DEFAULT FALSE",
                "ingestion_meta": "JSON DEFAULT '{}'",
            },
        )

    if "recommendations" in table_names:
        _add_columns_if_missing(
            engine,
            inspector,
            "recommendations",
            {
                "mesh_latency_ms": "FLOAT",
                "mesh_prompt_tokens": "INTEGER",
                "mesh_completion_tokens": "INTEGER",
                "mesh_cost_usd": "FLOAT",
                "tenant_id": "INTEGER",
                "widget_id": "INTEGER",
                "visitor_id": "VARCHAR(64)",
                "pushed_at": "TIMESTAMP",
            },
        )

    if "users" in table_names:
        _add_columns_if_missing(
            engine,
            inspector,
            "users",
            {"telegram_chat_id": "VARCHAR(120)", "tenant_id": "INTEGER"},
        )

    if "events" in table_names:
        _add_columns_if_missing(
            engine,
            inspector,
            "events",
            {
                "tenant_id": "INTEGER",
                "widget_id": "INTEGER",
                "visitor_id": "VARCHAR(64)",
            },
        )
        _drop_legacy_user_id(engine, inspector, "events")

    if "recommendations" in table_names:
        _drop_legacy_user_id(engine, inspector, "recommendations")

    if "tenants" in table_names:
        # These moved from Tenant to Widget in the per-widget key cutover (see
        # Widget's docstring) — dropped here the same way _migrate_catalog_item_specs
        # drops CatalogItem's old AI-specific columns above. A pre-existing
        # `allowed_origins` was NOT NULL on some deployments, which would otherwise
        # reject every new tenant insert (Tenant no longer sets it).
        existing_tenant_columns = {
            col["name"] for col in inspector.get_columns("tenants")
        }
        for legacy_column in (
            "allowed_origins",
            "first_event_at",
            "feed_url",
            "feed_auth_token",
        ):
            if legacy_column in existing_tenant_columns:
                with engine.begin() as conn:
                    conn.execute(
                        text(f"ALTER TABLE tenants DROP COLUMN {legacy_column}")
                    )

    if "widgets" in table_names:
        # Per-widget key cutover: a "widgets" table may already exist from an earlier
        # partial migration (created via `create_all` before these columns were added
        # to the Widget model) — `create_all` only creates missing *tables*, so an
        # already-existing one needs the same idempotent ALTER-TABLE catch-up as any
        # other table here.
        _add_columns_if_missing(
            engine,
            inspector,
            "widgets",
            {
                "first_event_at": "TIMESTAMP",
                "feed_url": "VARCHAR(500)",
                "feed_auth_token": "VARCHAR(500)",
            },
        )

    if "widget_sessions" in table_names:
        _add_columns_if_missing(
            engine, inspector, "widget_sessions", {"widget_id": "INTEGER"}
        )


def _backfill_default_widgets(engine) -> None:
    """Per-widget key cutover (docs/design/09-Platform-Pivot-Decision.md): every
    pre-existing tenant gets a lazily-created "Default" widget, and every
    CatalogItem/Event/Recommendation row that predates widgets gets backfilled onto
    it. Runs AFTER `create_all` (the widgets table must exist first) — this is a data
    migration, not a schema one, so it's called separately from
    `_add_missing_columns` above. A local import (not module-level) avoids a circular
    import: app.services.widgets imports app.models, which imports Base from this
    module — fine at call time (this module is already fully loaded by the time
    build_session_factory runs), but not safe as a top-level import here.
    """
    from app.models import Tenant
    from app.services.widgets import get_or_create_default_widget

    inspector = inspect(engine)
    if "widgets" not in inspector.get_table_names():
        return

    with Session(engine) as session:
        tenants = session.query(Tenant).all()
        for tenant in tenants:
            widget = get_or_create_default_widget(session, tenant)
            for table, column in (
                ("catalog_items", "widget_id"),
                ("events", "widget_id"),
                ("recommendations", "widget_id"),
            ):
                session.execute(
                    text(
                        f"UPDATE {table} SET {column} = :widget_id "
                        f"WHERE tenant_id = :tenant_id AND {column} IS NULL"
                    ),
                    {"widget_id": widget.id, "tenant_id": tenant.id},
                )
        session.commit()


def _stamp_alembic_baseline(settings: Settings) -> None:
    """Marks a database as Alembic-managed at the baseline revision, without
    running any migration (the schema is already there via the hand-rolled path
    that just ran). `alembic upgrade head` is a controlled, explicit command run
    at deploy time (see README) — it is deliberately NOT invoked here, so startup
    never performs uncontrolled schema mutation beyond this one-time bridge."""
    config = AlembicConfig(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    alembic_command.stamp(config, BASELINE_REVISION)


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    # Postgres gets a bounded connect_timeout so a stalled TCP handshake (e.g. Neon's
    # free-tier compute waking from autosuspend, or a transient network hiccup) fails
    # fast instead of hanging on the OS-level default — which can run well past any
    # sane request timeout and, since this factory is built synchronously at app
    # startup, was blocking the whole service (including /health) from ever binding
    # its port.
    connect_args = (
        {"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {"connect_timeout": 10}
    )
    engine = create_engine(settings.database_url, connect_args=connect_args)

    # Once a database carries an `alembic_version` table, it is fully Alembic-managed:
    # further schema changes only ever come from an explicit `alembic upgrade head`
    # (a deploy-time command, not something startup runs itself — see README's
    # migration runbook). Only a database that predates Alembic (fresh, or an
    # existing legacy deployment) falls through to the old hand-rolled path, which
    # brings it up to exactly the baseline schema and then stamps it so this branch
    # is never taken again for that database.
    if "alembic_version" not in inspect(engine).get_table_names():
        _rename_legacy_tables_and_columns(engine)
        Base.metadata.create_all(engine)
        _add_missing_columns(engine)
        _backfill_default_widgets(engine)
        _stamp_alembic_baseline(settings)

    return sessionmaker(bind=engine, autoflush=False, autocommit=False)

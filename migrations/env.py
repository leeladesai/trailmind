from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Import the app's models so Base.metadata reflects every table for autogenerate,
# and app.config.Settings so the DB URL always matches what the running app itself
# would use (same env/.env resolution) rather than a second, hand-copied URL in
# alembic.ini that could drift from the real one.
import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.config import Settings
from app.db import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# If nothing has already set a real sqlalchemy.url (e.g. app/db.py's
# _stamp_alembic_baseline, which sets it explicitly to match the exact settings the
# app is currently running with — a sqlite tmp path in tests, in particular), fall
# back to the app's own resolved DATABASE_URL (env var / .env / default sqlite) so a
# manually-run `alembic upgrade head` still targets the same database the app would.
# Never call Settings() unconditionally here — it reads the developer's real .env,
# which would silently override a caller-supplied URL (see tests/test_migrations.py).
_configured_url = config.get_main_option("sqlalchemy.url")
if not _configured_url or _configured_url.startswith("driver://"):
    config.set_main_option("sqlalchemy.url", Settings().database_url)

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SECRET_KEY = "change-me-to-a-random-secret"


class Settings(BaseSettings):
    # "development" locally by default; render.yaml sets APP_ENV=production so a
    # real deployment fails fast (see app/main.py's create_app) if SECRET_KEY was
    # never actually overridden, instead of silently signing admin JWTs with a
    # value published in this repo's source.
    app_env: str = "development"
    mesh_api_key: str | None = None
    mesh_base_url: str = "https://api.meshapi.ai/v1"
    # Benchmarked live against tencent/hy3 (free, but a "thinking" model — averaged
    # 46.5s per narrative call) and openai/gpt-5-nano (19s despite not being flagged
    # as a reasoning model — burns ~3k hidden tokens anyway). gpt-4.1-nano averaged
    # 1.8s with consistently valid, non-duplicated model_ids, at ~$0.00014/call.
    mesh_model: str = "openai/gpt-4.1-nano"
    # Real semantic embeddings via Mesh, replacing the deterministic hashed
    # bag-of-words fallback (app/vector.py) whenever mesh_api_key is set. Cheapest
    # embedding-capable model on Mesh as of this writing ($0.002/1M tokens) — see
    # the "real embedding model" bonus item this was added for.
    mesh_embedding_model: str = "google/embeddinggemma-300m"
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'trailmind.db'}"
    secret_key: str = DEFAULT_SECRET_KEY
    chroma_db_path: str = str(PROJECT_ROOT / "chroma_data")
    chroma_collection_name: str = "models"
    embedding_dimension: int = 64
    session_cookie_name: str = "trailmind_session"
    # False by default so http://localhost keeps working in local dev — a public HTTPS
    # deployment (see render.yaml) should set this true so the session cookie is never
    # sent over a plain-HTTP connection.
    session_cookie_secure: bool = False

    # OBS-1: LangSmith tracing (bonus, optional — off unless an API key is set)
    langsmith_api_key: str | None = None
    langsmith_project: str = "trailmind"

    # DLV-3: scheduled digest (bonus). Email delivers per-user via `User.email`;
    # Telegram delivers per-user via `User.telegram_chat_id`, falling back to the
    # shared `telegram_chat_id` broadcast chat below only for a user who hasn't set
    # their own. Neither configured falls back to logging the digest instead of
    # silently dropping it.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    digest_cron_hour: int = 9
    digest_cron_minute: int = 0
    # Public URL of the deployed app, if any — used only for the "View full dashboard"
    # link in the HTML digest email. Omitted from the email entirely when unset, rather
    # than linking to a localhost address nobody outside the dev machine can reach.
    app_base_url: str | None = None

    # P1-3: visitor data retention. Event/Recommendation/WidgetSession rows older
    # than this are purged by the scheduled retention job (app/services/retention.py)
    # — see that module's docstring for why a hard delete is safe here (nothing has
    # a foreign key into any of the three).
    event_retention_days: int = 90

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

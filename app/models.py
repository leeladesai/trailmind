from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Tenant(Base):
    """A company embedding TrailMind (docs/design/09-Platform-Pivot-Decision.md).
    `status` is platform-level governance only (approved/suspended by a platform
    admin — see TEN-1/TEN-8's pending_approval flow) — it no longer gates whether any
    one widget renders; that's `Widget.status`, since a tenant can now run several
    widgets (e.g. "Credit Cards", "Personal Loans") independently. `allowed_origins`,
    tracker verification, and feed config all moved to `Widget` for the same reason —
    see its docstring."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")
    max_agent_runs_per_hour: Mapped[int] = mapped_column(Integer, default=500)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TenantApiKey(Base):
    """Retired as of the per-widget key cutover (see Widget/WidgetApiKey) — a tracker/
    widget snippet now authenticates with a WidgetApiKey, not a tenant-wide key.
    Table (and any already-hashed rows) kept only so the migration that moves off it
    has something to read; nothing issues, checks, or rotates a TenantApiKey anymore.
    """

    __tablename__ = "tenant_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="user")
    # Per-user Telegram digest delivery (DLV-3 bonus follow-up): set via
    # PUT /api/auth/me/telegram-chat-id, self-serve. Optional — TelegramNotifier
    # (app/services/digest.py) falls back to the single configured broadcast chat
    # (TELEGRAM_CHAT_ID) for any user who hasn't set their own.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminSession(Base):
    """P0-4: server-side record of every issued admin JWT, keyed by its `jti` claim
    — what makes revocation possible at all for an otherwise-stateless JWT. A
    token's signature/expiry alone (the old behavior) can't be invalidated before
    it naturally expires; checking this table on every request (see
    app/security.py::get_current_user) lets logout, a detected leak, or a future
    "sign out everywhere" action actually take effect immediately."""

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TenantMembership(Base):
    """P0-4 foundation: a user may belong to more than one tenant, each with its own
    role — `User.tenant_id`/`User.role` stay as the primary/home tenant (every
    existing platform-admin/business-admin behavior is unchanged and keeps reading
    those columns directly), so this is additive, not a replacement. Deliberately
    not yet wired into every authorization check in this pass — see AGENTS.md/README
    enterprise-readiness notes: this is the extension point a real multi-tenant
    membership UI (invite a user into a second tenant, switch active tenant, etc.)
    would build on, kept minimal here rather than half-implemented everywhere."""

    __tablename__ = "tenant_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    """P0-4: append-only record of security-relevant admin actions — authentication,
    tenant lifecycle changes, widget key issuance/rotation/revocation, catalog
    changes, approvals, suspensions, and deletions (see
    app/services/audit.py::record_audit_event, the only writer). `actor_user_id` is
    nullable because a failed-login attempt has no authenticated user yet but is
    still worth recording."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class CatalogItem(Base):
    """A tenant's catalog entry recommended by the widget. Originally AI-model-shaped
    (a fixed `modality` enum, `latency_ms`/`context_window`) from the hackathon's
    narrower AI-model-catalog scope; generalized for arbitrary product catalogs
    (loans, cards, whatever a tenant sells) — `category` is now free text instead of
    a fixed enum, and `specs` replaces the two AI-specific typed columns with
    arbitrary label/value highlight pairs (docs/design/09-Platform-Pivot-Decision.md).
    """

    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    # Nullable at the schema level (existing rows predate widgets and were backfilled
    # to their tenant's auto-created "Default" widget — see db.py's migration), but
    # every new item is created within a widget context and always has one — this is
    # the actual scope retrieval/tracking isolate on, not tenant_id.
    widget_id: Mapped[int | None] = mapped_column(
        ForeignKey("widgets.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    story: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(60))
    price: Mapped[str] = mapped_column(String(120))
    # Arbitrary label -> value highlight pairs (e.g. {"Rate": "10.5-16% p.a."} for a
    # loan, {"Latency": "~135ms"} for an AI model) — replaces the old fixed
    # latency_ms/context_window columns. See catalog.py's SPEC_KEY_LIMIT.
    specs: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    use_case_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # `vector_synced` stays as a simple boolean mirror of
    # `vector_index_status == "synced"` for every pre-existing reader (e.g.
    # onboarding_status's catalog_ready check) — the richer state below is what the
    # reconciliation service (app/services/catalog_reconciliation.py) actually
    # reasons about, since a bare boolean can't distinguish "never synced yet" from
    # "synced, then Chroma's ephemeral disk wiped it" from "actively failing."
    vector_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    vector_index_status: Mapped[str] = mapped_column(String(20), default="pending")
    vector_index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    vector_index_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Catalog ingestion adapters (M4, docs/design plan): who/what produced this row.
    # "manual" (CAT-1..5, default — unaffected by this phase) never needs review; a
    # "feed"-sourced row is auto-approved like manual entries, while a "scrape"-sourced
    # row starts "pending_review" and is excluded from retrieval/vector-indexing until
    # an admin approves it (see app/services/catalog.py::approve_model). `sync_stale`
    # marks a feed-sourced row as no-longer-confirmed-fresh after a failed re-sync
    # (ING-5: serve last-known-good rather than delete or block on a feed outage).
    ingestion_adapter: Mapped[str] = mapped_column(String(20), default="manual")
    review_status: Mapped[str] = mapped_column(String(20), default="approved")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sync_stale: Mapped[bool] = mapped_column(Boolean, default=False)
    # Adapter-specific provenance (ING-6 traceability) that doesn't warrant its own
    # column: the scraped page URL/markup type/CSS selector for a "scrape" row, or the
    # feed URL a "feed" row came from. Empty dict for "manual" rows.
    ingestion_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    # The widget whose key authenticated this event — retrieval/behavior-summary
    # scoping now happens at this level, not tenant_id (tenant_id is kept for
    # tenant-wide admin aggregation, e.g. the Overview page's totals).
    widget_id: Mapped[int | None] = mapped_column(
        ForeignKey("widgets.id"), nullable=True, index=True
    )
    # An anonymous, tracker-assigned identity (see app/static/js/tracker.js) — not a
    # User row. The AI-engineer cookie-session `user_id` this replaced was removed
    # along with that login surface (docs/design/09-Platform-Pivot-Decision.md).
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    catalog_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("catalog_items.id"), nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    widget_id: Mapped[int | None] = mapped_column(
        ForeignKey("widgets.id"), nullable=True, index=True
    )
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_item_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    retrieval_meta: Mapped[list[dict]] = mapped_column(JSON, default=list)
    behavior_summary: Mapped[str] = mapped_column(Text, default="")
    activity_hash: Mapped[str] = mapped_column(String(64), index=True)
    trigger_reason: Mapped[str] = mapped_column(String(120))
    # Cost/latency rollup (bonus, retrieval/efficiency polish): captured directly from
    # the Mesh response at generation time (app/services/mesh.py), not re-derived from
    # LangSmith — the admin cost dashboard aggregates these straight out of our own DB.
    # Null whenever generation was skipped (no Mesh configured, or retrieval-only).
    mesh_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    mesh_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mesh_completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mesh_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # DLV-2: set when this recommendation was actually pushed to an open
    # `/api/widget/stream` connection at generation time — null means either no
    # connection was open (the visitor picks it up via GET /api/recommendations/latest
    # on next poll/reconnect) or push was never attempted.
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WidgetSession(Base):
    """DLV-2: one row per open `/api/widget/stream` SSE connection, for audit/
    observability — the actual push routing is an in-process registry
    (`app/main.py`'s `widget_connections`), rebuilt from scratch on every reconnect;
    this table is not consulted to route a push, only to record that one was open."""

    __tablename__ = "widget_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    # The actual push-routing scope (matches app.state.widget_connections' key) —
    # nullable only for rows written before this column existed; every new row always
    # sets it.
    widget_id: Mapped[int | None] = mapped_column(
        ForeignKey("widgets.id"), nullable=True, index=True
    )
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Widget(Base):
    """One embeddable unit under a tenant (e.g. "Credit Cards" vs "Personal Loans"),
    each with its own key, allowed origins, and catalog subset — the actual scope
    the tracker/widget SDK authenticates against and retrieval is isolated to
    (docs/design/09-Platform-Pivot-Decision.md). `status` is this widget's own TEN-8
    readiness gate (tracker verified + catalog ready — see
    app/services/widgets.py::onboarding_status), independent of its tenant's
    platform-governance `status`; both must be "active"/approved for the widget to
    actually render on the host page."""

    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="onboarding")
    allowed_origins: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Set once, on this widget's first successful tracker-SDK ingestion (see
    # POST /api/track/events) — the "tracker verified" half of TEN-8. Null until then.
    first_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Feed/API catalog ingestion (ING-1) config, scoped per widget now — two widgets
    # under the same tenant can sync from two different feeds. `feed_auth_token` is a
    # bearer token for *their* feed endpoint, not a TrailMind credential, so it's
    # stored as-is rather than hashed like a WidgetApiKey.
    feed_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    feed_auth_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WidgetApiKey(Base):
    """Mirrors TenantApiKey (same hash/grace-period rotation design) but scoped to one
    Widget instead of one Tenant — see Widget's docstring for why both exist."""

    __tablename__ = "widget_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    widget_id: Mapped[int] = mapped_column(ForeignKey("widgets.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

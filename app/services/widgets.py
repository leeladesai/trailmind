"""Widget resolution, API-key issuance/verification, and onboarding-readiness — the
per-widget counterpart of what app/services/tenants.py used to do at the tenant level
before the per-widget key cutover (see Widget's docstring in app/models.py). A
tracker/widget snippet's *only* identity is a WidgetApiKey now; nothing resolves by
tenant key anymore.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CatalogItem, Tenant, Widget, WidgetApiKey

DEFAULT_WIDGET_NAME = "Default"

# Same rationale as the old tenant-key grace period: an already-deployed snippet
# (cached HTML/CDN) doesn't start failing the instant a key rotates.
KEY_ROTATION_GRACE = timedelta(hours=24)

# TEN-8's render-gate: a widget must be verified + catalog-ready AND its parent
# tenant in good standing before any widget-facing endpoint other than tracking
# ingestion will serve it. Ingestion is the one exception — see
# INGESTION_WIDGET_STATUSES below.
ACTIVE_WIDGET_STATUSES = frozenset({"active"})
# Tracking ingestion is what flips a widget from "onboarding" to "active" in the
# first place (onboarding_status's tracker_verified needs a real event through
# POST /api/track/events) — rejecting onboarding widgets here would make onboarding
# impossible. `suspended` is still rejected: a manual suspend must take effect
# immediately, even mid-onboarding.
INGESTION_WIDGET_STATUSES = frozenset({"active", "onboarding"})
WIDGET_BLOCKING_TENANT_STATUSES = frozenset(
    {"suspended", "rejected", "pending_approval"}
)


class WidgetAuthError(Exception):
    """Raised by `resolve_authorized_widget` for every rejection reason — kept
    framework-agnostic (no HTTPException here) so this module has no FastAPI
    dependency and the policy itself is unit-testable in isolation. Callers at the
    HTTP boundary (app/main.py) map `code` to a status code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def hash_api_key(raw_key: str) -> str:
    # Same reasoning as the retired TenantApiKey.hash_api_key: this is a public,
    # client-embedded credential (ships in the widget snippet's page source), so
    # hashing at rest is about a leaked DB copy, not brute-force secrecy — a plain
    # fast SHA-256 is deliberate.
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _generate_raw_key() -> str:
    return f"wk_live_{secrets.token_urlsafe(32)}"


def create_widget(
    session: Session,
    tenant: Tenant,
    name: str,
    allowed_origins: list[str] | None = None,
) -> tuple[Widget, str]:
    """Returns `(widget, raw_key)` — the raw key is shown/returned exactly once; only
    its hash is ever persisted (see `hash_api_key`)."""
    widget = Widget(
        tenant_id=tenant.id,
        name=name,
        status="onboarding",
        allowed_origins=allowed_origins or [],
    )
    session.add(widget)
    session.commit()
    session.refresh(widget)
    raw_key = issue_api_key(session, widget)
    return widget, raw_key


def get_or_create_default_widget(session: Session, tenant: Tenant) -> Widget:
    """Every tenant gets exactly one lazily-created "Default" widget the first time
    one is needed and none exists yet — used by the legacy-tenant migration (db.py)
    and by anything that still only has a `Tenant` in hand (e.g. the reference
    tenant's own demo catalog). A tenant with real widget-management usage will
    normally have named widgets instead; this is the fallback, not the intended
    steady state."""
    widget = session.scalar(
        select(Widget).where(
            Widget.tenant_id == tenant.id, Widget.name == DEFAULT_WIDGET_NAME
        )
    )
    if widget is None:
        widget, _raw_key = create_widget(session, tenant, DEFAULT_WIDGET_NAME)
    return widget


def issue_api_key(session: Session, widget: Widget) -> str:
    """Mints a new `active` key for `widget` and returns the raw value (once). Does
    not touch any existing key — see `rotate_api_key` for that."""
    raw_key = _generate_raw_key()
    session.add(
        WidgetApiKey(
            widget_id=widget.id, key_hash=hash_api_key(raw_key), status="active"
        )
    )
    session.commit()
    return raw_key


def rotate_api_key(session: Session, widget: Widget) -> str:
    """Issues a new `active` key and flips every currently-`active` key for this
    widget to `grace` (valid for KEY_ROTATION_GRACE, then effectively expired — see
    `resolve_widget_by_api_key`). Returns the new raw key."""
    now = datetime.utcnow()
    active_keys = session.scalars(
        select(WidgetApiKey).where(
            WidgetApiKey.widget_id == widget.id, WidgetApiKey.status == "active"
        )
    ).all()
    for key in active_keys:
        key.status = "grace"
        key.expires_at = now + KEY_ROTATION_GRACE
    session.commit()
    return issue_api_key(session, widget)


def revoke_api_key(session: Session, key_id: int) -> None:
    """Immediate revoke (suspected leak) — bypasses the grace period entirely."""
    key = session.get(WidgetApiKey, key_id)
    if key is not None:
        key.status = "revoked"
        key.expires_at = datetime.utcnow()
        session.commit()


def resolve_widget_by_api_key(session: Session, raw_key: str) -> Widget | None:
    """The tracker/widget SDK's only auth mechanism: a valid, unexpired key
    (`active`, or `grace` and not yet past `expires_at`) resolves to its widget.
    Anything else — unknown hash, `revoked`, or an expired `grace` key — resolves to
    `None`, which callers must treat as an authentication failure, not "no widget
    scoping applied"."""
    if not raw_key:
        return None
    key_hash = hash_api_key(raw_key)
    api_key = session.scalar(
        select(WidgetApiKey).where(WidgetApiKey.key_hash == key_hash)
    )
    if api_key is None or api_key.status == "revoked":
        return None
    if api_key.status == "grace" and (
        api_key.expires_at is None or api_key.expires_at < datetime.utcnow()
    ):
        return None
    return session.get(Widget, api_key.widget_id)


def origin_allowed(widget: Widget, origin: str) -> bool:
    """Soft, browser-only defense (docs/design/09-Platform-Pivot-Decision.md §5)
    — an empty `allowed_origins` means "not configured", so every origin is
    allowed rather than every request being rejected. A non-browser client can
    always spoof `Origin`/`Referer`; the widget key is the real boundary, this
    only stops the most naive cross-site misuse from a real browser."""
    if not widget.allowed_origins:
        return True
    return any(origin.startswith(allowed) for allowed in widget.allowed_origins)


def resolve_authorized_widget(
    session: Session,
    raw_key: str,
    origin: str,
    *,
    allowed_widget_statuses: frozenset[str] = ACTIVE_WIDGET_STATUSES,
) -> Widget:
    """The single authorization policy for every widget-facing endpoint: valid key,
    allowed origin, widget status, and tenant status, in that order (so the most
    specific reason always wins — an unknown key never gets a "tenant not active"
    style leak). `allowed_widget_statuses` is the only axis that legitimately
    differs by endpoint — see INGESTION_WIDGET_STATUSES."""
    widget = resolve_widget_by_api_key(session, raw_key)
    if widget is None:
        raise WidgetAuthError("invalid_key", "Invalid widget key")
    if not origin_allowed(widget, origin):
        raise WidgetAuthError("origin_not_allowed", "Origin not allowed")
    if widget.status not in allowed_widget_statuses:
        raise WidgetAuthError("widget_not_active", "Widget not active")
    tenant = session.get(Tenant, widget.tenant_id)
    if tenant is None or tenant.status in WIDGET_BLOCKING_TENANT_STATUSES:
        raise WidgetAuthError("tenant_not_active", "Tenant not active")
    return widget


def onboarding_status(session: Session, widget: Widget) -> dict:
    """TEN-8's widget readiness gate, now per-widget: "tracker verified" (a real
    event has reached POST /api/track/events under this widget's key) AND "catalog
    ready" (at least one approved, vector-synced catalog item assigned to this
    widget). Flips `widget.status` from `onboarding` to `active` the first time both
    are true."""
    tracker_verified = widget.first_event_at is not None
    catalog_ready = (
        session.scalar(
            select(func.count())
            .select_from(CatalogItem)
            .where(
                CatalogItem.widget_id == widget.id,
                CatalogItem.review_status == "approved",
                CatalogItem.vector_synced.is_(True),
            )
        )
        > 0
    )
    ready = tracker_verified and catalog_ready
    if ready and widget.status == "onboarding":
        widget.status = "active"
        session.commit()
    return {
        "widget_id": widget.id,
        "tenant_id": widget.tenant_id,
        "status": widget.status,
        "tracker_verified": tracker_verified,
        "catalog_ready": catalog_ready,
        "ready": ready,
    }


def suspend_widget(session: Session, widget: Widget) -> None:
    """Manual override — takes this one widget dark immediately, independent of the
    automatic onboarding-readiness flip and independent of any other widget under
    the same tenant."""
    widget.status = "suspended"
    session.commit()


def reactivate_widget(session: Session, widget: Widget) -> None:
    """Manual counterpart to `suspend_widget` — sets `active` directly rather than
    re-running the onboarding checklist."""
    widget.status = "active"
    session.commit()


def configure_feed(
    session: Session, widget: Widget, feed_url: str, auth_token: str | None = None
) -> Widget:
    widget.feed_url = feed_url
    widget.feed_auth_token = auth_token
    session.commit()
    session.refresh(widget)
    return widget

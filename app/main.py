import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import build_session_factory
from app.models import (
    CatalogItem,
    Event,
    Recommendation,
    Tenant,
    User,
    Widget,
    WidgetApiKey,
    WidgetSession,
)
from app.schemas import (
    ApiKeyResponse,
    AuthCredentials,
    BulkImportResponse,
    CatalogItemCreate,
    CatalogItemResponse,
    FeedConfigRequest,
    FeedSyncResponse,
    IngestionStatusResponse,
    LoginResponse,
    OnboardingStatusResponse,
    ReindexResponse,
    ScrapeConfirmRequest,
    ScrapeConfirmResponse,
    ScrapePreviewRequest,
    ScrapePreviewResponse,
    SignupRequest,
    SignupResponse,
    TenantCreateRequest,
    TenantCreateResponse,
    TrackEventBatch,
    UserResponse,
    WidgetAskRequest,
    WidgetAskResponse,
    WidgetCreateRequest,
    WidgetCreateResponse,
    WidgetResponse,
)
from app.security import (
    create_session_token,
    hash_password,
    make_role_dependency,
    verify_password,
)
from app.services.admin_overview import (
    event_type_counts,
    feedback_sentiment,
    recent_activity,
    usage_totals,
)
from app.services.catalog import (
    approve_catalog_item as approve_catalog_item_service,
    create_catalog_item as create_catalog_item_service,
    delete_catalog_item as delete_catalog_item_service,
    update_catalog_item as update_catalog_item_service,
)
from app.services.catalog_import import (
    CatalogParseError,
    import_catalog_rows,
    parse_catalog_file,
)
from app.services.catalog_reconciliation import reconcile_catalog_index
from app.services.ingestion import (
    FeedSyncError,
    ScrapeError,
    ingestion_status,
    scrape_confirm,
    scrape_preview,
    sync_feed,
)
from app.services.agent_graph import (
    STRONG_RETRIEVAL_DISTANCE,
    WEAK_RETRIEVAL_DISTANCE,
    answer_visitor_question,
    contextual_reason,
    prepare_retrieval_recommendation,
)
from app.services.digest import build_notifier
from app.services.recommendation import (
    activity_summary,
    mesh_cost_rollup,
    recent_events,
    session_evidence,
    should_trigger,
    tenant_rate_limited,
)
from app.services.mesh import MeshNarrativeGenerator
from app.services.observability import (
    ObservabilityUnavailable,
    fetch_recent_runs,
    fetch_run_detail,
)
from app.services.tenants import (
    approve_tenant,
    create_tenant,
    reactivate_tenant,
    reject_tenant,
    suspend_tenant,
)
from app.services.tracing import configure_langsmith
from app.services.widgets import (
    ACTIVE_WIDGET_STATUSES,
    INGESTION_WIDGET_STATUSES,
    WidgetAuthError,
    configure_feed,
    create_widget,
    onboarding_status as widget_onboarding_status,
    reactivate_widget,
    resolve_authorized_widget,
    revoke_api_key,
    rotate_api_key,
    suspend_widget,
)
from app.vector import CatalogItemVectorStore, build_embedding_function
from seed_data import seed_demo_accounts


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BULK_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # plenty for a few hundred catalog rows

_WIDGET_AUTH_STATUS_CODES = {
    "invalid_key": 401,
    "origin_not_allowed": 403,
    "widget_not_active": 403,
    "tenant_not_active": 403,
}


def catalog_item_response(item: CatalogItem) -> CatalogItemResponse:
    return CatalogItemResponse.model_validate(item)


def widget_response(session, widget: Widget) -> WidgetResponse:
    readiness = widget_onboarding_status(session, widget)
    return WidgetResponse(
        id=widget.id,
        tenant_id=widget.tenant_id,
        name=widget.name,
        status=readiness["status"],
        allowed_origins=widget.allowed_origins,
        first_event_at=as_utc(widget.first_event_at),
        feed_url=widget.feed_url,
        tracker_verified=readiness["tracker_verified"],
        catalog_ready=readiness["catalog_ready"],
        ready=readiness["ready"],
        created_at=as_utc(widget.created_at),
    )


def as_utc(value):
    """SQLite's CURRENT_TIMESTAMP (via func.now()) is UTC but comes back tz-naive, so
    datetime.isoformat() serializes it with no 'Z'/offset — the browser's Date parser then
    reads it as local time instead of converting it, silently corrupting every displayed
    timestamp by the viewer's UTC offset. Stamping tzinfo here makes the API's timestamps
    unambiguous for any client, rather than papering over it in one frontend render call.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    configure_langsmith(app_settings)
    session_factory = build_session_factory(app_settings)
    vector_store = CatalogItemVectorStore(
        app_settings.chroma_db_path,
        collection_name=app_settings.chroma_collection_name,
        embedding_function=build_embedding_function(app_settings),
    )
    mesh_generator = MeshNarrativeGenerator(app_settings)
    notifier = build_notifier(app_settings)
    # AI-engineer accounts/session (self-registration, event tracking, dashboard,
    # activity) were removed as part of the platform pivot to a multi-tenant,
    # embeddable-widget product (docs/design/09-Platform-Pivot-Decision.md) — the
    # reference tenant's own end-user surface returns with the tracker SDK phase,
    # built on anonymous visitor identity rather than this cookie-session `user` role.
    # TEN-1..8: `admin` (tenant-scoped, `tenant_id` set) and `platform_admin`
    # (unscoped, `tenant_id` is None, can create tenants) are the two roles. `admin`
    # keeps its pre-onboarding name rather than becoming `tenant_admin` — no other
    # behavior distinguishes it from a hypothetical `tenant_admin`, so renaming it
    # would just be churn across every existing route/test.
    current_admin = make_role_dependency(
        session_factory, app_settings, required_role="admin"
    )
    current_platform_admin = make_role_dependency(
        session_factory, app_settings, required_role="platform_admin"
    )
    # Any signed-in staff account — used where an endpoint's own scoping check (not
    # this dependency) decides what a given role may act on.
    current_staff = make_role_dependency(
        session_factory, app_settings, required_role=("admin", "platform_admin")
    )

    # DLV-3 (bonus scheduled digest) is disabled, not redesigned, now that the
    # AI-engineer `user` role it iterated over is gone — app/services/digest.py stays
    # in place, unregistered, for whenever anonymous-visitor digest delivery is
    # actually specified.
    scheduler = BackgroundScheduler()

    def run_seed() -> None:
        try:
            # Accounts only — a real multi-tenant deployment has no use for the
            # hackathon-era mock catalog, so that part (seed_demo_catalog) only runs
            # when explicitly invoked via `python seed_data.py --with-catalog`, not on
            # every boot. See seed_data.py's docstrings for the split.
            seed_demo_accounts(session_factory)
        except Exception:
            # Best-effort: demo accounts staying stale (or briefly missing) on a
            # slow/unreachable DB is far better than taking the whole service down for
            # it — request handlers below still work against whatever's already in
            # the DB. Logged so a broken seed doesn't go unnoticed.
            logging.exception("Background seed_demo_accounts failed")

    def run_startup_reconciliation() -> None:
        """P0-1: a safe startup reconciliation path that does not block /health —
        fire-and-forget on a background thread, same pattern as run_seed above.
        Scans every widget's approved catalog items and rebuilds any vector entry
        that's missing (e.g. Chroma's ephemeral disk was wiped by a redeploy) or
        was left mid-failure from a previous run."""
        try:
            with session_factory() as session:
                report = reconcile_catalog_index(session, vector_store)
                if report.failed:
                    logging.warning(
                        "Startup catalog reconciliation: %d failed out of %d scanned",
                        report.failed,
                        report.scanned,
                    )
                else:
                    logging.info(
                        "Startup catalog reconciliation: %d scanned, %d rebuilt, "
                        "%d already synced",
                        report.scanned,
                        report.rebuilt,
                        report.already_synced,
                    )
        except Exception:
            # Best-effort, same reasoning as run_seed: a slow/unreachable vector
            # store at boot must not take down request handling.
            logging.exception("Startup catalog reconciliation failed")

    def run_scheduled_feed_syncs() -> None:
        """ING-1's sync cadence: sync-on-save (the manual endpoint below) plus this
        hourly sweep of every widget with a feed configured, so a feed that changes
        upstream without an admin manually re-triggering still stays current. Scoped
        per widget now, not per tenant — two widgets under the same tenant can sync
        from two different feeds (see Widget's docstring)."""
        with session_factory() as session:
            widgets = session.scalars(
                select(Widget).where(Widget.feed_url.is_not(None))
            ).all()
            for widget in widgets:
                try:
                    sync_feed(session, vector_store, widget)
                except FeedSyncError:
                    logging.warning(
                        "Scheduled feed sync failed for widget_id=%s", widget.id
                    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler.add_job(run_scheduled_feed_syncs, "interval", hours=1, id="feed_sync")
        scheduler.add_job(
            run_startup_reconciliation,
            "interval",
            hours=1,
            id="catalog_reconciliation",
        )
        scheduler.start()
        # Fire-and-forget, not awaited: uvicorn should start accepting requests
        # (including /health) immediately rather than waiting on this — see
        # seed_demo_data's docstring for why it used to block startup entirely.
        app.state.seed_task = asyncio.create_task(asyncio.to_thread(run_seed))
        app.state.reconciliation_task = asyncio.create_task(
            asyncio.to_thread(run_startup_reconciliation)
        )
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="TrailMind",
        description="Multi-tenant embeddable behavioral recommendation platform",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.session_factory = session_factory
    app.state.vector_store = vector_store
    app.state.mesh_generator = mesh_generator
    app.state.notifier = notifier
    app.state.scheduler = scheduler
    app.state.pipeline_locks = {}
    app.state.pipeline_locks_guard = asyncio.Lock()
    # DLV-2: (widget_id, visitor_id) -> list of open SSE connections' asyncio.Queue.
    # In-process only (no Redis/pub-sub) — consistent with this repo's other
    # single-process-deployment choices (pipeline_locks above, the TEN-6 rate cap);
    # a multi-worker deploy would need this revisited. Keyed by widget_id (not
    # tenant_id) now — the per-widget key cutover moved every auth/retrieval scope
    # one level deeper (see Widget's docstring in app/models.py).
    app.state.widget_connections = {}
    app.state.widget_connections_guard = asyncio.Lock()
    # Permissive at the CORSMiddleware layer on purpose — the tracker SDK and widget
    # both run on arbitrary tenant domains we can't enumerate in advance, and
    # per-widget origin scoping (`Widget.allowed_origins`) is checked inside the
    # handler instead (see POST /api/track/events and _resolve_widget below). CORS
    # itself is a browser-only, spoofable convenience; the widget API key is the real
    # boundary (docs/design/09-Platform-Pivot-Decision.md §5). The React admin
    # frontend (a separate origin as of the frontend/backend split) rides this same
    # wildcard policy safely because it authenticates with a bearer token, not a
    # cookie — no credentials, so `allow_origins=["*"]` stays valid for it too.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    # Still needed for tracker.js/widget.js — the embeddable SDK a tenant's own site
    # loads. The admin console itself moved to the separate React app (frontend/);
    # this backend no longer serves any HTML of its own.
    app.mount(
        "/static",
        StaticFiles(directory=PROJECT_ROOT / "app" / "static"),
        name="static",
    )

    @app.get("/health")
    async def health(response: Response) -> dict[str, str]:
        # Allows the standalone Vercel warm-up page (see warmup-page/) to poll this
        # cross-origin while the Render free-tier instance wakes from sleep.
        response.headers["Access-Control-Allow-Origin"] = "*"
        return {"status": "ok", "service": "trailmind"}

    @app.post("/api/admin/login", response_model=LoginResponse)
    async def admin_login(
        credentials: AuthCredentials, response: Response
    ) -> LoginResponse:
        with session_factory() as session:
            user = session.scalar(
                select(User).where(User.email == credentials.email.lower())
            )
            valid = user and verify_password(credentials.password, user.password_hash)
            if not valid or user.role not in ("admin", "platform_admin"):
                raise HTTPException(status_code=401, detail="Invalid email or password")
            if user.role == "admin":
                tenant = session.get(Tenant, user.tenant_id)
                if tenant and tenant.status == "pending_approval":
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"{tenant.name} is still awaiting approval. You'll be "
                            "able to sign in once a TrailMind platform admin "
                            "approves the account."
                        ),
                    )
                if tenant and tenant.status == "rejected":
                    raise HTTPException(
                        status_code=403,
                        detail="This account's signup request was not approved.",
                    )
            token = create_session_token(user, app_settings)
            # The React admin frontend reads `token` from the response body and sends
            # it back as a bearer token (see app/security.py's get_current_user) — the
            # cookie is set alongside it only for anything still relying on that path
            # during the frontend migration.
            response.set_cookie(
                app_settings.session_cookie_name,
                token,
                httponly=True,
                samesite="lax",
                secure=app_settings.session_cookie_secure,
                max_age=60 * 60 * 12,
            )
            return LoginResponse(
                id=user.id,
                email=user.email,
                role=user.role,
                telegram_chat_id=user.telegram_chat_id,
                token=token,
            )

    @app.post("/api/auth/signup", response_model=SignupResponse)
    async def signup(payload: SignupRequest) -> SignupResponse:
        """Self-serve tenant signup — the counterpart to platform-admin-created
        tenants (POST /api/tenants). Creates the tenant `pending_approval` (not
        `onboarding`) so login stays blocked until a platform admin approves it
        (see admin_login above, and approve_tenant_endpoint below)."""
        with session_factory() as session:
            email = payload.email.lower()
            if session.scalar(select(User).where(User.email == email)):
                raise HTTPException(
                    status_code=409, detail="An account with this email already exists"
                )
            tenant = create_tenant(
                session, payload.company_name, status="pending_approval"
            )
            user = User(
                tenant_id=tenant.id,
                email=email,
                password_hash=hash_password(payload.password),
                role="admin",
            )
            session.add(user)
            session.commit()
            return SignupResponse(
                tenant_id=tenant.id, email=email, status=tenant.status
            )

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(response: Response) -> None:
        response.delete_cookie(app_settings.session_cookie_name)

    @app.get("/api/admin/me", response_model=UserResponse)
    async def current_admin_profile(
        staff: User = Depends(current_staff),
    ) -> UserResponse:
        return UserResponse.model_validate(staff)

    def _get_owned_widget(session, widget_id: int, admin: User) -> Widget:
        widget = session.get(Widget, widget_id)
        if not widget or widget.tenant_id != admin.tenant_id:
            raise HTTPException(status_code=404, detail="Widget not found")
        return widget

    # --- Widget management (business admin, own tenant only) -------------------

    @app.post("/api/admin/widgets", response_model=WidgetCreateResponse)
    async def create_widget_endpoint(
        payload: WidgetCreateRequest, admin: User = Depends(current_admin)
    ) -> WidgetCreateResponse:
        with session_factory() as session:
            tenant = session.get(Tenant, admin.tenant_id)
            widget, raw_key = create_widget(
                session, tenant, payload.name, payload.allowed_origins
            )
            return WidgetCreateResponse(
                widget=widget_response(session, widget), api_key=raw_key
            )

    @app.get("/api/admin/widgets")
    async def list_widgets(
        admin: User = Depends(current_admin),
    ) -> list[WidgetResponse]:
        with session_factory() as session:
            widgets = session.scalars(
                select(Widget)
                .where(Widget.tenant_id == admin.tenant_id)
                .order_by(Widget.created_at.desc(), Widget.id.desc())
            ).all()
            return [widget_response(session, widget) for widget in widgets]

    @app.get("/api/admin/widgets/{widget_id}", response_model=WidgetResponse)
    async def get_widget(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> WidgetResponse:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            return widget_response(session, widget)

    @app.post(
        "/api/admin/widgets/{widget_id}/rotate-key", response_model=ApiKeyResponse
    )
    async def rotate_widget_key(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> ApiKeyResponse:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            raw_key = rotate_api_key(session, widget)
            return ApiKeyResponse(api_key=raw_key)

    @app.post(
        "/api/admin/widgets/{widget_id}/revoke-key/{key_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def revoke_widget_key(
        widget_id: int, key_id: int, admin: User = Depends(current_admin)
    ) -> None:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            key = session.get(WidgetApiKey, key_id)
            if not key or key.widget_id != widget_id:
                raise HTTPException(status_code=404, detail="Key not found")
            revoke_api_key(session, key_id)

    @app.post(
        "/api/admin/widgets/{widget_id}/suspend",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def suspend_widget_endpoint(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> None:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            suspend_widget(session, widget)

    @app.post(
        "/api/admin/widgets/{widget_id}/reactivate",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def reactivate_widget_endpoint(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> None:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            reactivate_widget(session, widget)

    @app.post("/api/admin/widgets/{widget_id}/feed")
    async def set_feed_config(
        widget_id: int,
        payload: FeedConfigRequest,
        admin: User = Depends(current_admin),
    ) -> dict[str, str]:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            configure_feed(session, widget, str(payload.feed_url), payload.auth_token)
        return {"status": "configured"}

    @app.post(
        "/api/admin/widgets/{widget_id}/feed/sync", response_model=FeedSyncResponse
    )
    async def trigger_feed_sync(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> FeedSyncResponse:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            try:
                rows = sync_feed(session, vector_store, widget)
            except FeedSyncError as exc:
                raise HTTPException(status_code=502, detail=str(exc))
        return FeedSyncResponse(
            inserted=sum(1 for row in rows if row["status"] == "inserted"),
            skipped_duplicate=sum(
                1 for row in rows if row["status"] == "skipped_duplicate"
            ),
            invalid=sum(1 for row in rows if row["status"] == "invalid"),
            rows=rows,
        )

    @app.post("/api/admin/widgets/{widget_id}/reindex", response_model=ReindexResponse)
    async def reindex_widget_catalog(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> ReindexResponse:
        """P0-1: explicit, admin-triggered rebuild of every approved catalog item's
        vector entry for this widget — unlike the periodic/startup reconciliation
        pass, this forces a rebuild regardless of the item's recorded state or
        attempt count, for when an operator already knows one is needed (e.g. after
        restoring a persistent Chroma disk, or investigating degraded retrieval)."""
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            report = reconcile_catalog_index(
                session, vector_store, widget_id=widget.id, force=True
            )
        return ReindexResponse(**report.as_dict())

    @app.get(
        "/api/admin/widgets/{widget_id}/ingestion/status",
        response_model=IngestionStatusResponse,
    )
    async def get_ingestion_status(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> IngestionStatusResponse:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            return IngestionStatusResponse(**ingestion_status(session, widget_id))

    @app.get(
        "/api/admin/widgets/{widget_id}/onboarding/status",
        response_model=OnboardingStatusResponse,
    )
    async def get_widget_onboarding_status(
        widget_id: int, admin: User = Depends(current_admin)
    ) -> OnboardingStatusResponse:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            return OnboardingStatusResponse(**widget_onboarding_status(session, widget))

    # --- Catalog items (scoped to one widget under the admin's tenant) ---------

    @app.get(
        "/api/admin/widgets/{widget_id}/catalog-items",
        response_model=list[CatalogItemResponse],
    )
    async def list_catalog_items(
        widget_id: int,
        admin: User = Depends(current_admin),
        q: str | None = Query(default=None),
        category: str | None = Query(default=None),
        provider: str | None = Query(default=None),
    ) -> list[CatalogItemResponse]:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            statement = (
                select(CatalogItem)
                .where(CatalogItem.widget_id == widget_id)
                .order_by(CatalogItem.title)
            )
            if q:
                statement = statement.where(
                    CatalogItem.title.ilike(f"%{q}%")
                    | CatalogItem.description.ilike(f"%{q}%")
                )
            if category:
                statement = statement.where(CatalogItem.category == category)
            if provider:
                statement = statement.where(CatalogItem.provider == provider)
            return [
                catalog_item_response(item) for item in session.scalars(statement).all()
            ]

    def _get_owned_catalog_item(
        session, widget_id: int, catalog_item_id: int
    ) -> CatalogItem:
        item = session.get(CatalogItem, catalog_item_id)
        if not item or item.widget_id != widget_id:
            raise HTTPException(status_code=404, detail="Catalog item not found")
        return item

    @app.get(
        "/api/admin/widgets/{widget_id}/catalog-items/{catalog_item_id}",
        response_model=CatalogItemResponse,
    )
    async def get_catalog_item(
        widget_id: int, catalog_item_id: int, admin: User = Depends(current_admin)
    ) -> CatalogItemResponse:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            item = _get_owned_catalog_item(session, widget_id, catalog_item_id)
            return catalog_item_response(item)

    def content_similarity_reason(distance: float, source_title: str) -> str:
        """Same distance thresholds as retrieval_reason (AGT-4), but worded for content-based
        similarity to a specific item rather than a match to the user's activity — using
        retrieval_reason's "your recent activity" phrasing here would misattribute why this
        item showed up."""
        if distance <= STRONG_RETRIEVAL_DISTANCE:
            return f"Strong match to {source_title}"
        if distance <= WEAK_RETRIEVAL_DISTANCE:
            return f"Similar to {source_title}"
        return "Broader catalog match"

    @app.get("/api/admin/widgets/{widget_id}/catalog-items/{catalog_item_id}/related")
    async def related_catalog_items(
        widget_id: int,
        catalog_item_id: int,
        admin: User = Depends(current_admin),
        limit: int = 3,
    ) -> list[dict[str, object]]:
        """Content-based "you might also be interested in": queries the same Chroma vector
        store the recommendation pipeline uses, but keyed on *this item's own* embedding
        text (title/provider/category/description/tags — see
        CatalogItemVectorStore.document) rather than a user's activity summary. Grounded
        in real similarity, not activity.
        """
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            item = _get_owned_catalog_item(session, widget_id, catalog_item_id)
            query_text = CatalogItemVectorStore.document(item)
            # Prefer same-category matches first — the deterministic hashed bag-of-words
            # embedding (app/vector.py) has weak semantics, so an unfiltered query can surface
            # a cross-category "match" (e.g. an LLM as "similar to" an image model) purely on
            # shared generic words. Only fall back to an unfiltered query if same-category
            # doesn't yield enough candidates (e.g. this category has too few catalog entries).
            same_category = vector_store.query_scored(
                query_text,
                widget_id,
                limit=limit + 1,
                where={"category": item.category},
            )
            seen_ids = {catalog_item_id}
            results: list[dict[str, object]] = []

            def _add_candidates(scored: list[tuple[int, float]]) -> None:
                for candidate_id, distance in scored:
                    if len(results) >= limit or candidate_id in seen_ids:
                        continue
                    seen_ids.add(candidate_id)
                    candidate = session.get(CatalogItem, candidate_id)
                    if not candidate or candidate.widget_id != widget_id:
                        continue
                    results.append(
                        {
                            **catalog_item_response(candidate).model_dump(mode="json"),
                            "why_this": content_similarity_reason(distance, item.title),
                        }
                    )

            _add_candidates(same_category)
            if len(results) < limit:
                _add_candidates(
                    vector_store.query_scored(query_text, widget_id, limit=limit + 1)
                )
            return results

    @app.post(
        "/api/admin/widgets/{widget_id}/catalog-items",
        response_model=CatalogItemResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_catalog_item(
        widget_id: int,
        payload: CatalogItemCreate,
        admin: User = Depends(current_admin),
    ) -> CatalogItemResponse:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            item = create_catalog_item_service(session, vector_store, widget, payload)
            return catalog_item_response(item)

    @app.put(
        "/api/admin/widgets/{widget_id}/catalog-items/{catalog_item_id}",
        response_model=CatalogItemResponse,
    )
    async def update_catalog_item(
        widget_id: int,
        catalog_item_id: int,
        payload: CatalogItemCreate,
        admin: User = Depends(current_admin),
    ) -> CatalogItemResponse:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            item = _get_owned_catalog_item(session, widget_id, catalog_item_id)
            update_catalog_item_service(session, vector_store, widget_id, item, payload)
            return catalog_item_response(item)

    @app.delete(
        "/api/admin/widgets/{widget_id}/catalog-items/{catalog_item_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_catalog_item(
        widget_id: int, catalog_item_id: int, admin: User = Depends(current_admin)
    ) -> None:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            item = _get_owned_catalog_item(session, widget_id, catalog_item_id)
            delete_catalog_item_service(session, vector_store, widget_id, item)

    @app.post(
        "/api/admin/widgets/{widget_id}/catalog-items/bulk-upload",
        response_model=BulkImportResponse,
    )
    async def bulk_upload_catalog_items(
        widget_id: int,
        file: UploadFile = File(...),
        admin: User = Depends(current_admin),
    ) -> BulkImportResponse:
        content = await file.read()
        if len(content) > BULK_UPLOAD_MAX_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 2MB).")
        try:
            raw_rows = parse_catalog_file(file.filename or "", content)
        except CatalogParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            rows = import_catalog_rows(session, vector_store, widget, raw_rows)
        return BulkImportResponse(
            inserted=sum(1 for row in rows if row["status"] == "inserted"),
            skipped_duplicate=sum(
                1 for row in rows if row["status"] == "skipped_duplicate"
            ),
            invalid=sum(1 for row in rows if row["status"] == "invalid"),
            rows=rows,
        )

    @app.post(
        "/api/admin/widgets/{widget_id}/catalog-items/{catalog_item_id}/approve",
        response_model=CatalogItemResponse,
    )
    async def approve_catalog_item(
        widget_id: int, catalog_item_id: int, admin: User = Depends(current_admin)
    ) -> CatalogItemResponse:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
            item = _get_owned_catalog_item(session, widget_id, catalog_item_id)
            approve_catalog_item_service(session, vector_store, widget_id, item)
            return catalog_item_response(item)

    @app.post(
        "/api/admin/widgets/{widget_id}/ingestion/scrape/preview",
        response_model=ScrapePreviewResponse,
    )
    async def preview_scrape(
        widget_id: int,
        payload: ScrapePreviewRequest,
        admin: User = Depends(current_admin),
    ) -> ScrapePreviewResponse:
        with session_factory() as session:
            _get_owned_widget(session, widget_id, admin)
        try:
            result = scrape_preview(str(payload.url), payload.selectors or None)
        except ScrapeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return ScrapePreviewResponse(**result)

    @app.post(
        "/api/admin/widgets/{widget_id}/ingestion/scrape/confirm",
        response_model=ScrapeConfirmResponse,
    )
    async def confirm_scrape(
        widget_id: int,
        payload: ScrapeConfirmRequest,
        admin: User = Depends(current_admin),
    ) -> ScrapeConfirmResponse:
        with session_factory() as session:
            widget = _get_owned_widget(session, widget_id, admin)
            rows = scrape_confirm(
                session,
                vector_store,
                widget,
                str(payload.url),
                payload.markup_type,
                payload.rows,
            )
        return ScrapeConfirmResponse(rows=rows)

    # --- Tenant governance (platform admin) -------------------------------------

    @app.post("/api/tenants", response_model=TenantCreateResponse)
    async def create_tenant_endpoint(
        payload: TenantCreateRequest,
        platform_admin: User = Depends(current_platform_admin),
    ) -> TenantCreateResponse:
        with session_factory() as session:
            tenant = create_tenant(session, payload.name)
            return TenantCreateResponse(
                id=tenant.id, name=tenant.name, status=tenant.status
            )

    def _tenant_summary(session, tenant: Tenant) -> dict[str, object]:
        widget_count = session.scalar(
            select(func.count())
            .select_from(Widget)
            .where(Widget.tenant_id == tenant.id)
        )
        return {
            "id": tenant.id,
            "name": tenant.name,
            "status": tenant.status,
            "created_at": as_utc(tenant.created_at).isoformat(),
            "widget_count": widget_count,
        }

    @app.get("/api/tenants")
    async def list_tenants(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        platform_admin: User = Depends(current_platform_admin),
    ) -> dict[str, object]:
        """Platform-admin tenant roster — newest-onboarded first."""
        with session_factory() as session:
            rows = session.scalars(
                select(Tenant)
                .order_by(Tenant.created_at.desc(), Tenant.id.desc())
                .offset(offset)
                .limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "tenants": [_tenant_summary(session, tenant) for tenant in page],
                "has_more": has_more,
            }

    @app.get("/api/tenants/{tenant_id}")
    async def get_tenant_detail(
        tenant_id: int, platform_admin: User = Depends(current_platform_admin)
    ) -> dict[str, object]:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            summary = _tenant_summary(session, tenant)
            widgets = session.scalars(
                select(Widget)
                .where(Widget.tenant_id == tenant_id)
                .order_by(Widget.created_at.desc(), Widget.id.desc())
            ).all()
            summary["widgets"] = [
                widget_response(session, widget).model_dump(mode="json")
                for widget in widgets
            ]
            return summary

    @app.post(
        "/api/tenants/{tenant_id}/suspend", status_code=status.HTTP_204_NO_CONTENT
    )
    async def suspend_tenant_endpoint(
        tenant_id: int, platform_admin: User = Depends(current_platform_admin)
    ) -> None:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            suspend_tenant(session, tenant)

    @app.post(
        "/api/tenants/{tenant_id}/reactivate", status_code=status.HTTP_204_NO_CONTENT
    )
    async def reactivate_tenant_endpoint(
        tenant_id: int, platform_admin: User = Depends(current_platform_admin)
    ) -> None:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            reactivate_tenant(session, tenant)

    @app.post(
        "/api/tenants/{tenant_id}/approve", status_code=status.HTTP_204_NO_CONTENT
    )
    async def approve_tenant_endpoint(
        tenant_id: int, platform_admin: User = Depends(current_platform_admin)
    ) -> None:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            approve_tenant(session, tenant)

    @app.post("/api/tenants/{tenant_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
    async def reject_tenant_endpoint(
        tenant_id: int, platform_admin: User = Depends(current_platform_admin)
    ) -> None:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if not tenant:
                raise HTTPException(status_code=404, detail="Tenant not found")
            reject_tenant(session, tenant)

    @app.get("/api/admin/observability/runs")
    async def observability_runs(
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        visitor_id: str | None = Query(default=None),
        _: User = Depends(current_admin),
    ) -> dict[str, object]:
        """OBS-2: surfaces recent agent-pipeline traces inside the admin portal
        itself, so a curator never needs their own LangSmith login to see whether
        recent runs succeeded and how long they took. Read-only proxy over the
        LangSmith API — this app never writes trace data, `@traceable` (OBS-1)
        already does that."""
        try:
            runs, has_more = fetch_recent_runs(
                app_settings, limit=limit, offset=offset, visitor_id=visitor_id
            )
        except ObservabilityUnavailable as exc:
            return {
                "available": False,
                "message": str(exc),
                "runs": [],
                "has_more": False,
            }
        return {
            "available": True,
            "message": None,
            "has_more": has_more,
            "runs": [
                {
                    "id": run.id,
                    "name": run.name,
                    "run_type": run.run_type,
                    "status": run.status,
                    "start_time": as_utc(run.start_time).isoformat()
                    if run.start_time
                    else None,
                    "latency_ms": run.latency_ms,
                    "pipeline_latency_ms": run.pipeline_latency_ms,
                    "visitor_id": run.visitor_id,
                    "error": run.error,
                    "url": run.url,
                }
                for run in runs
            ],
        }

    @app.get("/api/admin/observability/runs/{run_id}")
    async def observability_run_detail(
        run_id: str,
        _: User = Depends(current_admin),
    ) -> dict[str, object]:
        """The step-by-step breakdown of a single run — brings the trace itself into
        the admin portal instead of only linking out to LangSmith (OBS-2 follow-up)."""
        try:
            detail = fetch_run_detail(app_settings, run_id)
        except ObservabilityUnavailable as exc:
            return {"available": False, "message": str(exc), "run": None}
        return {
            "available": True,
            "message": None,
            "run": {
                "id": detail.id,
                "name": detail.name,
                "status": detail.status,
                "start_time": as_utc(detail.start_time).isoformat()
                if detail.start_time
                else None,
                "latency_ms": detail.latency_ms,
                "pipeline_latency_ms": detail.pipeline_latency_ms,
                "url": detail.url,
                "steps": [
                    {
                        "name": step.name,
                        "run_type": step.run_type,
                        "status": step.status,
                        "start_time": as_utc(step.start_time).isoformat()
                        if step.start_time
                        else None,
                        "latency_ms": step.latency_ms,
                        "error": step.error,
                        "depth": step.depth,
                        "inputs": step.inputs,
                        "outputs": step.outputs,
                    }
                    for step in detail.steps
                ],
            },
        }

    @app.get("/api/admin/overview")
    async def admin_overview(
        admin: User = Depends(current_admin),
    ) -> dict[str, object]:
        """Platform-usage summary for the admin landing page — totals, event-type
        breakdown, and explicit-feedback sentiment. Tenant-wide (across every widget
        under the tenant), distinct from /api/admin/observability/* (AI-pipeline/
        LangSmith technical health); this is business/usage metrics, computed straight
        from our own tables."""
        with session_factory() as session:
            return {
                "totals": usage_totals(session, admin.tenant_id),
                "event_type_counts": event_type_counts(session, admin.tenant_id),
                "feedback": feedback_sentiment(session, admin.tenant_id),
            }

    @app.get("/api/admin/overview/activity")
    async def admin_overview_activity(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        admin: User = Depends(current_admin),
    ) -> dict[str, object]:
        """The admin-wide live activity feed — every visitor's events, newest first."""
        with session_factory() as session:
            events, has_more = recent_activity(
                session, admin.tenant_id, limit=limit, offset=offset
            )
        return {
            "events": [
                {**event, "created_at": as_utc(event["created_at"])} for event in events
            ],
            "has_more": has_more,
        }

    @app.get("/api/admin/users")
    async def list_users(
        limit: int = Query(default=500, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        admin: User = Depends(current_admin),
    ) -> dict[str, object]:
        """Newest-registered first. `limit` defaults high enough that the
        Observability page's "filter by user" dropdown (which wants every user, not
        a page of them) can keep calling this with no params; the Users table page
        passes an explicit limit=20 for real pagination."""
        with session_factory() as session:
            rows = session.scalars(
                select(User)
                .where(User.tenant_id == admin.tenant_id)
                .order_by(User.created_at.desc(), User.id.desc())
                .offset(offset)
                .limit(limit + 1)
            ).all()
            has_more = len(rows) > limit
            page = rows[:limit]
            return {
                "users": [
                    {
                        "id": user.id,
                        "email": user.email,
                        "role": user.role,
                        "telegram_chat_id": user.telegram_chat_id,
                        "created_at": as_utc(user.created_at).isoformat(),
                    }
                    for user in page
                ],
                "has_more": has_more,
            }

    @app.delete("/api/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_user(user_id: int, admin: User = Depends(current_admin)) -> None:
        if user_id == admin.id:
            raise HTTPException(
                status_code=400, detail="Cannot delete the account you're signed in as"
            )
        with session_factory() as session:
            user = session.get(User, user_id)
            if not user or user.tenant_id != admin.tenant_id:
                raise HTTPException(status_code=404, detail="User not found")
            # Admin accounts (User rows) are no longer linked to Event/Recommendation
            # at all — those are keyed by anonymous visitor_id, not a User's id — so
            # there's nothing left to cascade-clean here (see the visitor_id rename,
            # docs/design/09-Platform-Pivot-Decision.md).
            session.delete(user)
            session.commit()

    @app.get("/api/admin/observability/costs")
    async def observability_costs(
        admin: User = Depends(current_admin),
    ) -> dict[str, object]:
        """Mesh cost/latency/token rollup, aggregated straight from our own DB
        (`Recommendation.mesh_*` columns, captured in app/services/mesh.py at
        generation time) — deliberately not another LangSmith query, so this stays
        available even without tracing configured. Tenant-wide (across every widget
        under the tenant), consistent with the rest of the admin Overview surface."""
        with session_factory() as session:
            rollup = mesh_cost_rollup(session, admin.tenant_id)
        return {
            "call_count": rollup["call_count"],
            "avg_latency_ms": rollup["avg_latency_ms"],
            "total_prompt_tokens": rollup["total_prompt_tokens"],
            "total_completion_tokens": rollup["total_completion_tokens"],
            "total_cost_usd": rollup["total_cost_usd"],
            "recent": [
                {
                    "id": row["id"],
                    "created_at": as_utc(row["created_at"]).isoformat()
                    if row["created_at"]
                    else None,
                    "latency_ms": row["latency_ms"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "cost_usd": row["cost_usd"],
                }
                for row in rollup["recent"]
            ],
        }

    def _request_origin(request: Request) -> str:
        return request.headers.get("origin") or request.headers.get("referer") or ""

    def _authorize_widget(
        session: Session,
        raw_key: str,
        request: Request,
        *,
        allowed_widget_statuses: frozenset[str] = ACTIVE_WIDGET_STATUSES,
    ) -> Widget:
        """The single authorization path for every widget-facing endpoint (tracking
        ingestion, latest-recommendation polling, SSE stream, widget Q&A, widget
        activity) — see resolve_authorized_widget's docstring for the policy itself.
        Translates WidgetAuthError into the matching HTTP status here, at the
        framework boundary, rather than in the (framework-agnostic) service layer."""
        try:
            return resolve_authorized_widget(
                session,
                raw_key,
                _request_origin(request),
                allowed_widget_statuses=allowed_widget_statuses,
            )
        except WidgetAuthError as exc:
            raise HTTPException(
                status_code=_WIDGET_AUTH_STATUS_CODES[exc.code], detail=exc.message
            ) from exc

    def _resolve_widget(widget_key: str, request: Request) -> Widget:
        """Thin wrapper for the three endpoints that resolve the widget in its own
        short-lived session and hand back a detached `Widget` for a second session
        to do the actual work in (SSE stream, widget Q&A, widget activity)."""
        with session_factory() as session:
            widget = _authorize_widget(session, widget_key, request)
            session.expunge(widget)
            return widget

    async def _get_visitor_lock(widget_id: int, visitor_id: str) -> asyncio.Lock:
        key = (widget_id, visitor_id)
        async with app.state.pipeline_locks_guard:
            lock = app.state.pipeline_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                app.state.pipeline_locks[key] = lock
            return lock

    async def _register_widget_connection(
        widget_id: int, visitor_id: str
    ) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with app.state.widget_connections_guard:
            app.state.widget_connections.setdefault((widget_id, visitor_id), []).append(
                queue
            )
        return queue

    async def _unregister_widget_connection(
        widget_id: int, visitor_id: str, queue: asyncio.Queue
    ) -> None:
        async with app.state.widget_connections_guard:
            queues = app.state.widget_connections.get((widget_id, visitor_id))
            if queues and queue in queues:
                queues.remove(queue)
                if not queues:
                    del app.state.widget_connections[(widget_id, visitor_id)]

    def _broadcast_widget_update(
        widget_id: int, visitor_id: str, payload: dict
    ) -> None:
        # Runs on the event loop thread (scheduled via loop.call_soon_threadsafe from
        # the background-thread pipeline run below) — safe to touch asyncio.Queue
        # objects and app.state directly here, unlike from the worker thread itself.
        for queue in app.state.widget_connections.get((widget_id, visitor_id), []):
            queue.put_nowait(payload)

    async def run_tracker_pipeline_in_background(
        widget_id: int, tenant_id: int, visitor_id: str
    ) -> None:
        """Same shape/reasoning as the retired cookie-session
        run_pipeline_in_background (NFR-1: keep the Mesh round trip off the ingestion
        request path; per-(widget, visitor) asyncio.Lock to prevent a duplicate
        Recommendation row from two near-simultaneous qualifying batches)."""
        lock = await _get_visitor_lock(widget_id, visitor_id)
        loop = asyncio.get_running_loop()

        def push_callback(payload: dict) -> bool:
            # prepare_retrieval_recommendation runs this from a worker thread
            # (asyncio.to_thread below) — asyncio.Queue isn't thread-safe, so the
            # actual put has to happen back on the loop thread.
            has_connection = bool(
                app.state.widget_connections.get((widget_id, visitor_id))
            )
            if has_connection:
                loop.call_soon_threadsafe(
                    _broadcast_widget_update, widget_id, visitor_id, payload
                )
            return has_connection

        async with lock:
            with session_factory() as session:
                await asyncio.to_thread(
                    prepare_retrieval_recommendation,
                    session,
                    vector_store,
                    widget_id,
                    tenant_id,
                    visitor_id,
                    app.state.mesh_generator,
                    "event_threshold",
                    push_callback,
                )

    # No explicit OPTIONS handler needed: CORSMiddleware intercepts and answers every
    # preflight request itself, before it ever reaches route dispatch.
    @app.post("/api/track/events")
    async def track_events(
        batch: TrackEventBatch,
        background_tasks: BackgroundTasks,
        request: Request,
    ) -> dict[str, object]:
        """TRK-4: the tracker SDK's ingestion endpoint. Authenticated by widget API
        key (`widget_key`, travels in the body — see TrackEventBatch), not a cookie
        session; identity is an anonymous, client-generated `visitor_id`, not a
        `User` row."""
        with session_factory() as session:
            widget = _authorize_widget(
                session,
                batch.widget_key,
                request,
                allowed_widget_statuses=INGESTION_WIDGET_STATUSES,
            )

            # A catalog_item_id that doesn't belong to this widget is dropped rather
            # than stored — an event referencing another widget's catalog item id
            # must never let that item's title/category leak into this visitor's
            # behavior summary later (TEN-3/NFR-8).
            catalog_item_ids = {
                event.catalog_item_id for event in batch.events if event.catalog_item_id
            }
            valid_catalog_item_ids = (
                set(
                    session.scalars(
                        select(CatalogItem.id).where(
                            CatalogItem.id.in_(catalog_item_ids),
                            CatalogItem.widget_id == widget.id,
                        )
                    ).all()
                )
                if catalog_item_ids
                else set()
            )
            events = [
                Event(
                    tenant_id=widget.tenant_id,
                    widget_id=widget.id,
                    visitor_id=batch.visitor_id,
                    event_type=event.event_type,
                    catalog_item_id=event.catalog_item_id
                    if event.catalog_item_id in valid_catalog_item_ids
                    else None,
                    metadata_json=event.metadata,
                )
                for event in batch.events
            ]
            session.add_all(events)
            if widget.first_event_at is None:
                # Client-side timestamp, not an Event's own server-generated
                # created_at — that column is a server_default (func.now()), so it's
                # not populated on the Python object until after commit/refresh.
                widget.first_event_at = datetime.utcnow()
            session.commit()

            triggered = should_trigger(session, widget.id, batch.visitor_id)
            if triggered:
                tenant = session.get(Tenant, widget.tenant_id)
                if tenant_rate_limited(session, tenant):
                    # TEN-6: the tenant-aggregate ceiling wins over an individually
                    # qualifying visitor — see tenant_rate_limited's docstring for why
                    # a per-visitor check alone isn't enough (the key is public).
                    triggered = False
            if triggered:
                lock = app.state.pipeline_locks.get((widget.id, batch.visitor_id))
                if lock is not None and lock.locked():
                    triggered = False
            widget_id, tenant_id = widget.id, widget.tenant_id
        if triggered:
            background_tasks.add_task(
                run_tracker_pipeline_in_background,
                widget_id,
                tenant_id,
                batch.visitor_id,
            )
        return {
            "accepted": len(events),
            "recommendation_triggered": triggered,
        }

    @app.get("/api/recommendations/latest")
    async def latest_recommendation_for_visitor(
        request: Request,
        widget_key: str = Query(...),
        visitor_id: str = Query(...),
    ) -> dict[str, object]:
        """Read fallback for when no real-time push connection is open — same
        widget authorization policy as every other widget-facing endpoint (key,
        origin, widget status, tenant status; see resolve_authorized_widget)."""
        with session_factory() as session:
            widget = _authorize_widget(session, widget_key, request)

            latest = session.scalar(
                select(Recommendation)
                .where(
                    Recommendation.widget_id == widget.id,
                    Recommendation.visitor_id == visitor_id,
                )
                .order_by(Recommendation.created_at.desc())
            )
            current_events = recent_events(session, widget.id, visitor_id)
            evidence = session_evidence(session, widget.id, current_events)
            evidence_payload = [
                {
                    "label": item["label"],
                    "action": item["action"],
                    "created_at": as_utc(item["created_at"]),
                }
                for item in evidence
            ]

            if latest:
                items = session.scalars(
                    select(CatalogItem).where(
                        CatalogItem.id.in_(latest.catalog_item_ids),
                        CatalogItem.widget_id == widget.id,
                    )
                ).all()
                catalog_items_by_id = {item.id: item for item in items}
                reason_by_id = {
                    entry["catalog_item_id"]: entry["reason"]
                    for entry in latest.retrieval_meta or []
                }
                return {
                    "id": latest.id,
                    "status": "ready" if latest.narrative else "retrieval_ready",
                    "narrative": latest.narrative,
                    "catalog_items": [
                        {
                            **catalog_item_response(
                                catalog_items_by_id[catalog_item_id]
                            ).model_dump(mode="json"),
                            "why_this": reason_by_id.get(catalog_item_id),
                        }
                        for catalog_item_id in latest.catalog_item_ids
                        if catalog_item_id in catalog_items_by_id
                    ],
                    "behavior_summary": latest.behavior_summary,
                    "activity_hash": latest.activity_hash,
                    "trigger_reason": latest.trigger_reason,
                    "created_at": as_utc(latest.created_at),
                    "evidence": evidence_payload,
                }
            if not current_events:
                return {
                    "status": "pending",
                    "narrative": None,
                    "catalog_items": [],
                    "evidence": [],
                }

            summary = activity_summary(session, widget.id, current_events)
            scored = vector_store.query_scored(summary, widget.id)
            if not scored:
                return {
                    "status": "pending",
                    "narrative": None,
                    "catalog_items": [],
                    "trigger_reason": "no_retrieval_candidates",
                    "evidence": evidence_payload,
                }
            candidate_ids = [catalog_item_id for catalog_item_id, _ in scored]
            catalog_items_by_id = {
                item.id: item
                for item in session.scalars(
                    select(CatalogItem).where(
                        CatalogItem.id.in_(candidate_ids),
                        CatalogItem.widget_id == widget.id,
                    )
                ).all()
            }
            candidates = [
                {
                    **catalog_item_response(
                        catalog_items_by_id[catalog_item_id]
                    ).model_dump(mode="json"),
                    "why_this": contextual_reason(
                        catalog_items_by_id[catalog_item_id], distance, False, evidence
                    ),
                }
                for catalog_item_id, distance in scored
                if catalog_item_id in catalog_items_by_id
            ]
            return {
                "status": "retrieval_ready",
                "narrative": None,
                "catalog_items": candidates,
                "trigger_reason": "activity_retrieval",
                "evidence": evidence_payload,
            }

    @app.get("/api/widget/stream")
    async def widget_stream(
        request: Request,
        widget_key: str = Query(...),
        visitor_id: str = Query(...),
    ) -> StreamingResponse:
        """DLV-2: server-sent-events push. A visitor's open widget gets a
        `recommendation` event the moment `_store_and_deliver` finishes generating one
        for them (app/services/agent_graph.py); GET /api/recommendations/latest stays
        the polling fallback for a visitor with no live connection open."""
        widget = _resolve_widget(widget_key, request)
        connection_id = secrets.token_hex(16)
        with session_factory() as session:
            widget_session = WidgetSession(
                tenant_id=widget.tenant_id,
                widget_id=widget.id,
                visitor_id=visitor_id,
                connection_id=connection_id,
            )
            session.add(widget_session)
            session.commit()
            session.refresh(widget_session)
            widget_session_id = widget_session.id

        queue = await _register_widget_connection(widget.id, visitor_id)

        async def event_generator():
            try:
                yield "event: open\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # SSE keep-alive comment — some proxies (and the browser
                        # itself) close an idle connection well before a real
                        # recommendation might fire.
                        yield ": keep-alive\n\n"
                    else:
                        yield f"event: recommendation\ndata: {json.dumps(payload)}\n\n"
            finally:
                await _unregister_widget_connection(widget.id, visitor_id, queue)
                with session_factory() as session:
                    row = session.get(WidgetSession, widget_session_id)
                    if row is not None:
                        row.closed_at = datetime.now(timezone.utc)
                        session.commit()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/widget/ask", response_model=WidgetAskResponse)
    async def widget_ask(
        payload: WidgetAskRequest, request: Request
    ) -> WidgetAskResponse:
        widget = _resolve_widget(payload.widget_key, request)
        with session_factory() as session:
            result = answer_visitor_question(
                session,
                vector_store,
                widget.id,
                payload.visitor_id,
                payload.question,
                app.state.mesh_generator,
            )
        return WidgetAskResponse(**result)

    @app.get("/api/widget/activity")
    async def widget_activity(
        request: Request,
        widget_key: str = Query(...),
        visitor_id: str = Query(...),
    ) -> dict[str, object]:
        """DLV-6: read-only view over already-persisted events/recommendations, no new
        backend logic — the same shape /api/recommendations/latest already assembles
        inline, exposed as its own endpoint for the widget's "why am I seeing this"
        panel."""
        widget = _resolve_widget(widget_key, request)
        with session_factory() as session:
            events = recent_events(session, widget.id, visitor_id)
            latest = session.scalar(
                select(Recommendation)
                .where(
                    Recommendation.widget_id == widget.id,
                    Recommendation.visitor_id == visitor_id,
                )
                .order_by(Recommendation.created_at.desc())
            )
            return {
                "events": [
                    {
                        "type": event.event_type,
                        "catalog_item_id": event.catalog_item_id,
                        "metadata": event.metadata_json,
                        "created_at": as_utc(event.created_at),
                    }
                    for event in events
                ],
                "pipeline": {
                    "trigger_reason": latest.trigger_reason,
                    "behavior_summary": latest.behavior_summary,
                    "activity_hash": latest.activity_hash,
                    "delivered_at": as_utc(latest.created_at),
                }
                if latest
                else None,
            }

    return app

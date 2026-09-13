from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class AuthCredentials(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)


class UserResponse(BaseModel):
    id: int
    email: str
    role: str
    telegram_chat_id: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AdminUserResponse(UserResponse):
    created_at: datetime


class LoginResponse(UserResponse):
    token: str = Field(
        description="Bearer token — send as `Authorization: Bearer <token>` on"
        " every subsequent request. Valid 12 hours."
    )


class SignupRequest(BaseModel):
    company_name: str = Field(min_length=1, max_length=255)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8)


class SignupResponse(BaseModel):
    tenant_id: int
    email: str
    status: str


class CatalogItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    story: str | None = Field(default=None)
    provider: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    price: str = Field(min_length=1, max_length=120)
    # Arbitrary label -> value highlight pairs (e.g. {"Rate": "10.5-16% p.a."}) —
    # replaces the old fixed latency_ms/context_window fields. See CatalogItem.
    specs: dict[str, str] = Field(default_factory=dict)
    use_case_tags: list[str] = Field(default_factory=list)
    source_url: HttpUrl | None = None


class CatalogItemResponse(CatalogItemCreate):
    id: int
    vector_synced: bool
    ingestion_adapter: str
    review_status: str
    last_synced_at: datetime | None = None
    sync_stale: bool
    ingestion_meta: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BulkImportRowResult(BaseModel):
    row: int
    title: str | None = None
    status: str
    errors: list[str] = Field(default_factory=list)


class BulkImportResponse(BaseModel):
    inserted: int
    skipped_duplicate: int
    invalid: int
    rows: list[BulkImportRowResult]


class FeedConfigRequest(BaseModel):
    feed_url: HttpUrl
    auth_token: str | None = Field(default=None, max_length=500)


class FeedSyncResponse(BaseModel):
    inserted: int
    skipped_duplicate: int
    invalid: int
    rows: list[BulkImportRowResult]


class ReindexResponse(BaseModel):
    scanned: int
    already_synced: int
    rebuilt: int
    failed: int
    skipped_after_max_attempts: int
    errors: list[str]


class RetentionPurgeResponse(BaseModel):
    events_deleted: int
    recommendations_deleted: int
    widget_sessions_deleted: int


class ScrapePreviewRequest(BaseModel):
    url: HttpUrl
    selectors: dict[str, str] = Field(default_factory=dict)


class ScrapePreviewResponse(BaseModel):
    markup_type: str
    rows: list[dict]


class ScrapeConfirmRequest(BaseModel):
    url: HttpUrl
    markup_type: str
    rows: list[dict] = Field(min_length=1, max_length=100)


class ScrapeConfirmRowResult(BaseModel):
    row: int
    title: str | None = None
    status: str
    errors: list[str] = Field(default_factory=list)
    catalog_item_id: int | None = None


class ScrapeConfirmResponse(BaseModel):
    rows: list[ScrapeConfirmRowResult]


class IngestionAdapterStatus(BaseModel):
    count: int
    last_synced_at: datetime | None = None
    sync_stale: bool
    pending_review: int


class IngestionStatusResponse(BaseModel):
    feed: IngestionAdapterStatus
    scrape: IngestionAdapterStatus


class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TenantCreateResponse(BaseModel):
    id: int
    name: str
    status: str


class ApiKeyResponse(BaseModel):
    api_key: str = Field(description="Raw widget API key — shown once.")


class OnboardingStatusResponse(BaseModel):
    widget_id: int
    tenant_id: int
    status: str
    tracker_verified: bool
    catalog_ready: bool
    ready: bool


class WidgetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    allowed_origins: list[str] = Field(default_factory=list)


class WidgetResponse(BaseModel):
    id: int
    tenant_id: int
    name: str
    status: str
    allowed_origins: list[str]
    first_event_at: datetime | None = None
    feed_url: str | None = None
    tracker_verified: bool
    catalog_ready: bool
    ready: bool
    created_at: datetime


class WidgetCreateResponse(BaseModel):
    widget: WidgetResponse
    api_key: str = Field(
        description="Raw widget API key — shown once, never retrievable again."
    )


class TrackEventInput(BaseModel):
    # Event *type names* (model_view, model_compare, …) are a separate taxonomy from
    # the CatalogItem entity rename — left as-is; changing these would reinterpret
    # every already-stored Event row's meaning, which the entity rename doesn't
    # require.
    event_type: str = Field(
        pattern="^(page_view|model_view|search|click|model_compare|dwell|catalog_filter"
        "|model_copy|model_watchlist|recommendation_feedback)$"
    )
    catalog_item_id: int | None = None
    metadata: dict = Field(default_factory=dict)


class WidgetAskRequest(BaseModel):
    # Same key-in-body reasoning as TrackEventBatch below — the widget authenticates
    # the same way the tracker does, over a plain POST, not a header.
    widget_key: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=500)


class WidgetAskResponse(BaseModel):
    answer: str
    catalog_item_ids: list[int] = Field(default_factory=list)


class TrackEventBatch(BaseModel):
    # The widget key travels in the body, not a header — navigator.sendBeacon (used
    # for the on-unload flush, app/static/js/tracker.js) can't set custom headers, so
    # a header-only scheme would silently lose every beacon-flushed batch.
    widget_key: str = Field(min_length=1)
    visitor_id: str = Field(min_length=1, max_length=64)
    events: list[TrackEventInput] = Field(min_length=1, max_length=100)

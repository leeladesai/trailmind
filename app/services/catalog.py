from datetime import datetime

from sqlalchemy.orm import Session

from app.models import CatalogItem, Widget
from app.schemas import CatalogItemCreate
from app.vector import CatalogItemVectorStore


def _apply_payload(item: CatalogItem, payload: CatalogItemCreate) -> None:
    values = payload.model_dump()
    if values["source_url"] is not None:
        values["source_url"] = str(values["source_url"])
    for field, value in values.items():
        setattr(item, field, value)


def create_catalog_item(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget: Widget,
    payload: CatalogItemCreate,
    *,
    ingestion_adapter: str = "manual",
    review_status: str = "approved",
    last_synced_at: datetime | None = None,
    ingestion_meta: dict | None = None,
) -> CatalogItem:
    """`ingestion_adapter`/`review_status`/`last_synced_at`/`ingestion_meta` are set by
    the ingestion adapters (app/services/ingestion.py) — manual admin creation (the only
    caller before M4) leaves them at their defaults. A row is only pushed into the
    vector store (and therefore eligible for retrieval) once `review_status ==
    "approved"` — a "pending_review" scrape row stays out of Chroma until
    `approve_catalog_item` runs. `tenant_id` is stamped from the widget for tenant-wide
    admin aggregation (e.g. Overview totals); `widget_id` is the real scope everything
    else (retrieval, tracking) isolates on.
    """
    item = CatalogItem(
        tenant_id=widget.tenant_id,
        widget_id=widget.id,
        vector_synced=False,
        ingestion_adapter=ingestion_adapter,
        review_status=review_status,
        last_synced_at=last_synced_at,
        ingestion_meta=ingestion_meta or {},
    )
    _apply_payload(item, payload)
    session.add(item)
    session.commit()
    session.refresh(item)
    if review_status == "approved":
        _sync_item(session, vector_store, widget.id, item)
    return item


def approve_catalog_item(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget_id: int,
    item: CatalogItem,
) -> CatalogItem:
    """ING-6: flips a "pending_review" scrape row to "approved", making it eligible
    for retrieval/vector-indexing for the first time."""
    item.review_status = "approved"
    session.commit()
    _sync_item(session, vector_store, widget_id, item)
    return item


def update_catalog_item(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget_id: int,
    item: CatalogItem,
    payload: CatalogItemCreate,
) -> CatalogItem:
    item.vector_synced = False
    _apply_payload(item, payload)
    session.commit()
    session.refresh(item)
    _sync_item(session, vector_store, widget_id, item)
    return item


def delete_catalog_item(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget_id: int,
    item: CatalogItem,
) -> None:
    vector_store.delete(item.id, widget_id)
    session.delete(item)
    session.commit()


def _sync_item(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget_id: int,
    item: CatalogItem,
) -> None:
    try:
        vector_store.upsert(item, widget_id)
        item.vector_synced = True
        item.vector_index_status = "synced"
        item.vector_index_error = None
        item.vector_indexed_at = datetime.utcnow()
        item.vector_index_attempts = 0
        session.commit()
    except Exception as exc:
        session.rollback()
        persisted_item = session.get(CatalogItem, item.id)
        if persisted_item:
            persisted_item.vector_synced = False
            persisted_item.vector_index_status = "failed"
            persisted_item.vector_index_error = str(exc)[:500]
            persisted_item.vector_index_attempts = (
                persisted_item.vector_index_attempts or 0
            ) + 1
            session.commit()

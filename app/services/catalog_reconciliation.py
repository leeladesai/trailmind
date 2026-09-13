"""Catalog/vector reconciliation (P0-1).

SQL is the source of truth for the catalog; Chroma is not guaranteed durable (an
ephemeral disk on Render's free tier — see README — wipes it on every redeploy
unless a persistent Disk is attached). Before this module existed, a catalog
item's `vector_synced` flag was set once, optimistically, at write time and never
re-checked — it could stay `True` long after the underlying Chroma data actually
disappeared, with retrieval silently degrading (fewer/no candidates) and nothing
in the admin console pointing at why.

This module scans approved catalog items and verifies each one's vector entry
actually exists in Chroma (`CatalogItemVectorStore.contains`), rebuilding it when
missing, and never marks an item "synced" unless the vector write itself
succeeded — a failure is recorded (`vector_index_status="failed"`,
`vector_index_error`) for a human, the next scheduled pass, or an explicit admin
reindex to retry, rather than silently pretending the catalog is healthy.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CatalogItem
from app.vector import CatalogItemVectorStore

logger = logging.getLogger(__name__)

# After this many consecutive failures for the same item, a routine (non-forced)
# reconciliation pass stops retrying it automatically — a persistently broken row
# (e.g. bad data that always fails embedding) shouldn't burn a scan on every pass
# forever. An explicit admin reindex (`force=True`) always retries regardless.
MAX_AUTO_RETRY_ATTEMPTS = 5


@dataclass
class ReconciliationReport:
    scanned: int = 0
    already_synced: int = 0
    rebuilt: int = 0
    failed: int = 0
    skipped_after_max_attempts: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "already_synced": self.already_synced,
            "rebuilt": self.rebuilt,
            "failed": self.failed,
            "skipped_after_max_attempts": self.skipped_after_max_attempts,
            "errors": self.errors,
        }


def _approved_items(session: Session, widget_id: int | None):
    query = select(CatalogItem).where(CatalogItem.review_status == "approved")
    if widget_id is not None:
        query = query.where(CatalogItem.widget_id == widget_id)
    return session.scalars(query).all()


def reconcile_catalog_index(
    session: Session,
    vector_store: CatalogItemVectorStore,
    *,
    widget_id: int | None = None,
    force: bool = False,
) -> ReconciliationReport:
    """Scans approved catalog items — every widget by default, or just `widget_id`
    (the per-widget Chroma collection stays isolated either way; this never
    touches another widget's collection) — and verifies/rebuilds their vector
    entries.

    `force=True` (the admin reindex endpoint) rebuilds every scanned item
    unconditionally, ignoring both the recorded status and the attempt limit —
    that's the deliberate, explicit override for when an operator already knows a
    rebuild is needed.
    """
    report = ReconciliationReport()
    for item in _approved_items(session, widget_id):
        report.scanned += 1
        if not force:
            if item.vector_index_status == "synced" and vector_store.contains(
                item.id, item.widget_id
            ):
                report.already_synced += 1
                continue
            if (
                item.vector_index_status == "failed"
                and item.vector_index_attempts >= MAX_AUTO_RETRY_ATTEMPTS
            ):
                report.skipped_after_max_attempts += 1
                continue

        try:
            vector_store.upsert(item, item.widget_id)
        except (
            Exception
        ) as exc:  # pragma: no cover - exercised via fake stores in tests
            item.vector_synced = False
            item.vector_index_status = "failed"
            item.vector_index_error = str(exc)[:500]
            item.vector_index_attempts = (item.vector_index_attempts or 0) + 1
            session.commit()
            report.failed += 1
            report.errors.append(f"catalog_item {item.id}: {exc}")
            logger.warning(
                "Vector reindex failed for catalog_item %s (widget %s): %s",
                item.id,
                item.widget_id,
                exc,
            )
            continue

        item.vector_synced = True
        item.vector_index_status = "synced"
        item.vector_index_error = None
        item.vector_indexed_at = datetime.utcnow()
        item.vector_index_attempts = 0
        session.commit()
        report.rebuilt += 1
    return report

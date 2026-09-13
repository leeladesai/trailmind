"""Visitor data retention/purge (P1-3).

Event, Recommendation, and WidgetSession all key off an anonymous
tracker-assigned `visitor_id`, not a User row, and — unlike CatalogItem — nothing
holds a foreign key into any of them (see app/models.py), so there's no
referential-integrity reason to keep a row around once it's outlived its
usefulness. Before this module existed, none of the three ever expired: every
tracked event, generated recommendation, and SSE session record accumulated
forever, growing the DB unbounded and keeping visitor behavioral data around far
longer than needed to serve the product.

This module purges rows older than a configurable retention window
(`Settings.event_retention_days`), hard-deleted rather than soft-flagged —
there's no downstream consumer that needs to distinguish "expired" from
"never existed" the way catalog retrieval needs to distinguish "delisted" from
"deleted" (see catalog_import.py's reactivation path).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Event, Recommendation, WidgetSession

logger = logging.getLogger(__name__)


@dataclass
class RetentionReport:
    events_deleted: int = 0
    recommendations_deleted: int = 0
    widget_sessions_deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "events_deleted": self.events_deleted,
            "recommendations_deleted": self.recommendations_deleted,
            "widget_sessions_deleted": self.widget_sessions_deleted,
        }


def purge_expired_visitor_data(
    session: Session,
    *,
    retention_days: int,
    tenant_id: int | None = None,
    now: datetime | None = None,
) -> RetentionReport:
    """Deletes Event/Recommendation/WidgetSession rows older than `retention_days`.

    `tenant_id=None` (the scheduled sweep) purges across every tenant; passing a
    tenant_id (an admin-triggered manual purge) scopes the delete to just that
    tenant, mirroring the per-widget/per-tenant scoping used everywhere else in
    this admin surface.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    # SQLite (this app's default) stores naive datetimes; created_at columns are
    # never timezone-aware, so compare against a naive cutoff to match.
    cutoff = cutoff.replace(tzinfo=None)

    report = RetentionReport()

    events_query = delete(Event).where(Event.created_at < cutoff)
    if tenant_id is not None:
        events_query = events_query.where(Event.tenant_id == tenant_id)
    report.events_deleted = session.execute(events_query).rowcount

    recs_query = delete(Recommendation).where(Recommendation.created_at < cutoff)
    if tenant_id is not None:
        recs_query = recs_query.where(Recommendation.tenant_id == tenant_id)
    report.recommendations_deleted = session.execute(recs_query).rowcount

    sessions_query = delete(WidgetSession).where(WidgetSession.opened_at < cutoff)
    if tenant_id is not None:
        sessions_query = sessions_query.where(WidgetSession.tenant_id == tenant_id)
    report.widget_sessions_deleted = session.execute(sessions_query).rowcount

    session.commit()
    if (
        report.events_deleted
        or report.recommendations_deleted
        or report.widget_sessions_deleted
    ):
        logger.info(
            "Retention purge (tenant_id=%s, retention_days=%s): "
            "%d events, %d recommendations, %d widget_sessions deleted",
            tenant_id,
            retention_days,
            report.events_deleted,
            report.recommendations_deleted,
            report.widget_sessions_deleted,
        )
    return report

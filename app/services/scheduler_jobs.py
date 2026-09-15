"""Durable scheduled jobs (P1-5).

Before this module existed, `app/main.py`'s three background jobs (feed sync,
catalog reconciliation, retention purge) were plain closures added to an in-memory
`BackgroundScheduler` inside `create_app`. That's fine for the jobs themselves, but
it means every process restart (a Render redeploy, a crash, the free-tier instance
waking from sleep) silently resets each job's schedule to "N hours from whenever
this boot happened" — on a host that redeploys or restarts more often than a job's
own interval, that job can go a very long time without ever actually firing.

APScheduler's SQLAlchemyJobStore fixes that by persisting each job's `next_run_time`
in the same database, so a restart resumes the existing schedule instead of
resetting it. The catch: a persistent job store pickles the job's callable and its
args to store them, and a nested closure (`def create_app(): def run_x(): ...`)
can't be pickled ("reference to its callable could not be determined"). So the job
bodies below are plain module-level functions — picklable by `module:function`
reference — that take a single picklable string arg (`context_id`) and look up the
actual live session_factory/vector_store/settings for this process out of the
in-memory `_CONTEXTS` registry below, rather than closing over them directly.
`context_id` itself is fine to persist across restarts: `upsert_scheduled_job`
below repoints an already-persisted job at this boot's fresh context_id via
`modify_job` (which leaves `next_run_time` untouched) instead of `add_job`
(which would recompute it), so the schedule survives even though the context
naturally doesn't.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Widget
from app.services.catalog_reconciliation import reconcile_catalog_index
from app.services.ingestion import FeedSyncError, sync_feed_with_retry
from app.services.retention import purge_expired_visitor_data
from app.vector import CatalogItemVectorStore

logger = logging.getLogger(__name__)


@dataclass
class JobContext:
    session_factory: sessionmaker[Session]
    vector_store: CatalogItemVectorStore
    settings: Settings


_CONTEXTS: dict[str, JobContext] = {}


def register_context(
    session_factory: sessionmaker[Session],
    vector_store: CatalogItemVectorStore,
    settings: Settings,
) -> str:
    """Registers this process's live resources under a fresh id and returns it —
    called once per `create_app()`, so each FastAPI app instance (including one per
    test) gets its own isolated context."""
    context_id = uuid.uuid4().hex
    _CONTEXTS[context_id] = JobContext(session_factory, vector_store, settings)
    return context_id


def unregister_context(context_id: str) -> None:
    """Called at lifespan shutdown — frees the entry so a long-lived process (the
    test suite creates many app instances in one Python process) doesn't accumulate
    stale contexts for the lifetime of that process."""
    _CONTEXTS.pop(context_id, None)


def run_feed_sync_job(context_id: str) -> None:
    """ING-1's cadence: sync-on-save (the manual endpoint) plus this sweep of every
    widget with a feed configured, so a feed that changes upstream without an admin
    manually re-triggering still stays current. Uses the retrying variant
    (`sync_feed_with_retry`) since nobody's waiting on this call the way an admin
    waits on the manual endpoint."""
    context = _CONTEXTS.get(context_id)
    if context is None:
        logger.warning("run_feed_sync_job: no context registered for %s", context_id)
        return
    with context.session_factory() as session:
        widgets = session.scalars(
            select(Widget).where(Widget.feed_url.is_not(None))
        ).all()
        for widget in widgets:
            try:
                sync_feed_with_retry(session, context.vector_store, widget)
            except FeedSyncError:
                logger.warning(
                    "Scheduled feed sync failed for widget_id=%s after retries",
                    widget.id,
                )


def run_catalog_reconciliation_job(context_id: str) -> None:
    """P0-1's scheduled sweep: rebuilds any vector entry missing for an approved
    catalog item (e.g. Chroma's ephemeral disk was wiped by a redeploy)."""
    context = _CONTEXTS.get(context_id)
    if context is None:
        logger.warning(
            "run_catalog_reconciliation_job: no context registered for %s", context_id
        )
        return
    try:
        with context.session_factory() as session:
            report = reconcile_catalog_index(session, context.vector_store)
            if report.failed:
                logger.warning(
                    "Scheduled catalog reconciliation: %d failed out of %d scanned",
                    report.failed,
                    report.scanned,
                )
            else:
                logger.info(
                    "Scheduled catalog reconciliation: %d scanned, %d rebuilt, "
                    "%d already synced",
                    report.scanned,
                    report.rebuilt,
                    report.already_synced,
                )
    except Exception:
        logger.exception("Scheduled catalog reconciliation failed")


def run_retention_purge_job(context_id: str) -> None:
    """P1-3's daily sweep purging Event/Recommendation/WidgetSession rows older than
    Settings.event_retention_days, across every tenant."""
    context = _CONTEXTS.get(context_id)
    if context is None:
        logger.warning(
            "run_retention_purge_job: no context registered for %s", context_id
        )
        return
    try:
        with context.session_factory() as session:
            report = purge_expired_visitor_data(
                session, retention_days=context.settings.event_retention_days
            )
            logger.info(
                "Scheduled retention purge: %d events, %d recommendations, "
                "%d widget_sessions deleted",
                report.events_deleted,
                report.recommendations_deleted,
                report.widget_sessions_deleted,
            )
    except Exception:
        logger.exception("Scheduled retention purge failed")


def upsert_scheduled_job(
    scheduler: BackgroundScheduler,
    job_id: str,
    func,
    context_id: str,
    **trigger_kwargs,
) -> None:
    """Adds `job_id` if it's never existed, or repoints an already-persisted one
    (from a prior boot, found in the durable job store) at this boot's fresh
    `context_id` — deliberately via `modify_job`, not `add_job(..., replace_existing
    =True)`, because `add_job` always recomputes `next_run_time` even when
    replacing, which would silently reset the schedule on every single restart and
    defeat the entire point of a persistent job store. Must be called after
    `scheduler.start()` — `get_job`/`modify_job` only see what's actually in the job
    store, which isn't populated until the store itself has started."""
    if scheduler.get_job(job_id) is not None:
        scheduler.modify_job(job_id, func=func, args=(context_id,))
    else:
        scheduler.add_job(func, id=job_id, args=(context_id,), **trigger_kwargs)

"""P1-5: durable scheduled jobs. The core guarantee under test is that a job's
`next_run_time` survives a process restart against a persistent (SQLAlchemy-backed)
job store — see app/services/scheduler_jobs.py's module docstring for why that
requires `upsert_scheduled_job`'s modify-in-place-if-already-persisted approach
rather than a plain `add_job(replace_existing=True)`, which always recomputes it.
"""

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

import app.services.ingestion as ingestion_module
from app.config import Settings
from app.db import build_session_factory
from app.models import CatalogItem, Tenant
from app.services.scheduler_jobs import (
    register_context,
    run_catalog_reconciliation_job,
    run_feed_sync_job,
    unregister_context,
    upsert_scheduled_job,
)
from app.services.widgets import configure_feed, create_widget
from app.vector import CatalogItemVectorStore, build_embedding_function


def _make_settings(tmp_path, name: str = "test.db") -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / name}",
        chroma_db_path=str(tmp_path / "chroma"),
        # Overrides whatever real MESH_API_KEY a developer's local .env has, same as
        # conftest's `client` fixture — otherwise build_embedding_function reaches for
        # real Mesh embeddings, which the hermetic-network guard (tests/conftest.py)
        # then turns into a connection error instead of the deterministic local
        # fallback these tests actually want.
        mesh_api_key=None,
    )


def noop_job(context_id: str) -> None:
    pass


def test_upsert_scheduled_job_adds_a_new_job_with_a_computed_next_run_time(
    tmp_path,
) -> None:
    settings = _make_settings(tmp_path)
    session_factory = build_session_factory(settings)
    scheduler = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=session_factory.kw["bind"])}
    )
    scheduler.start()
    try:
        upsert_scheduled_job(
            scheduler, "test_job", noop_job, "ctx-1", trigger="interval", hours=1
        )
        job = scheduler.get_job("test_job")
        assert job is not None
        assert job.args == ("ctx-1",)
        assert job.next_run_time is not None
    finally:
        scheduler.shutdown(wait=False)


def test_upsert_scheduled_job_preserves_next_run_time_across_a_restart(
    tmp_path,
) -> None:
    """The whole point of a persistent job store: a second "boot" against the same
    durable store must not reset the schedule, even though it repoints the job at a
    brand new (this-process-only) context id."""
    settings = _make_settings(tmp_path)
    session_factory = build_session_factory(settings)
    engine = session_factory.kw["bind"]

    first_boot = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine)}
    )
    first_boot.start()
    upsert_scheduled_job(
        first_boot, "test_job", noop_job, "ctx-boot-1", trigger="interval", hours=1
    )
    original_next_run_time = first_boot.get_job("test_job").next_run_time
    first_boot.shutdown(wait=False)

    second_boot = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(engine=engine)}
    )
    second_boot.start()
    try:
        upsert_scheduled_job(
            second_boot, "test_job", noop_job, "ctx-boot-2", trigger="interval", hours=1
        )
        job = second_boot.get_job("test_job")
        assert job.next_run_time == original_next_run_time
        assert job.args == ("ctx-boot-2",)
    finally:
        second_boot.shutdown(wait=False)


def test_run_feed_sync_job_is_a_noop_when_context_is_not_registered() -> None:
    """A job persisted from a prior process whose context id was never (re-)
    registered in this one must log and return, never raise — the scheduler thread
    must not die over a stale reference."""
    run_feed_sync_job("nonexistent-context-id")


def test_register_context_then_unregister_removes_it(tmp_path) -> None:
    settings = _make_settings(tmp_path)
    session_factory = build_session_factory(settings)
    vector_store = CatalogItemVectorStore(
        settings.chroma_db_path,
        collection_name=settings.chroma_collection_name,
        embedding_function=build_embedding_function(settings),
    )
    context_id = register_context(session_factory, vector_store, settings)

    # Registered: run_feed_sync_job must be able to look it up without complaint on
    # a widget-less DB (nothing to sync, but the context lookup itself must succeed).
    run_feed_sync_job(context_id)

    unregister_context(context_id)
    # Unregistered: same call now hits the "no context registered" branch, which is
    # itself covered by test_run_feed_sync_job_is_a_noop_when_context_is_not_registered
    # above — this just proves unregister actually removed the entry.
    from app.services.scheduler_jobs import _CONTEXTS

    assert context_id not in _CONTEXTS


def test_run_feed_sync_job_end_to_end_via_registered_context(
    tmp_path, monkeypatch
) -> None:
    """Proves the whole picklable-function + context-registry mechanism actually
    performs a real feed sync, not just that the plumbing type-checks."""
    settings = _make_settings(tmp_path)
    session_factory = build_session_factory(settings)
    vector_store = CatalogItemVectorStore(
        settings.chroma_db_path,
        collection_name=settings.chroma_collection_name,
        embedding_function=build_embedding_function(settings),
    )
    context_id = register_context(session_factory, vector_store, settings)

    def fake_get(url, headers=None, timeout=None, **_kwargs):
        class _Resp:
            content = (
                b'{"models": [{"title": "Feed Item", "provider": "Feed Co", '
                b'"category": "LLM", "price": "$1", "description": "d"}]}'
            )

            def raise_for_status(self):
                pass

        return _Resp()

    monkeypatch.setattr(ingestion_module.httpx, "get", fake_get)

    with session_factory() as session:
        tenant = Tenant(name="Test Tenant")
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
        widget, _raw_key = create_widget(session, tenant, "Test Widget")
        configure_feed(session, widget, "https://vendor.example.com/feed.json")

    run_feed_sync_job(context_id)

    with session_factory() as session:
        item = session.query(CatalogItem).filter_by(title="Feed Item").one()
        assert item.ingestion_adapter == "feed"

    unregister_context(context_id)


def test_run_catalog_reconciliation_job_end_to_end_via_registered_context(
    tmp_path,
) -> None:
    settings = _make_settings(tmp_path)
    session_factory = build_session_factory(settings)
    vector_store = CatalogItemVectorStore(
        settings.chroma_db_path,
        collection_name=settings.chroma_collection_name,
        embedding_function=build_embedding_function(settings),
    )
    context_id = register_context(session_factory, vector_store, settings)

    with session_factory() as session:
        tenant = Tenant(name="Test Tenant")
        session.add(tenant)
        session.commit()
        session.refresh(tenant)
        widget, _raw_key = create_widget(session, tenant, "Test Widget")
        item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Manual Item",
            provider="Acme",
            category="LLM",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        session.add(item)
        session.commit()
        item_id = item.id

    run_catalog_reconciliation_job(context_id)

    with session_factory() as session:
        item = session.get(CatalogItem, item_id)
        assert item.vector_synced is True
        assert vector_store.contains(item.id, item.widget_id)

    unregister_context(context_id)

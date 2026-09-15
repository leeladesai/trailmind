import inspect
from datetime import datetime

from app.config import Settings
from app.db import build_session_factory
from app.models import CatalogItem, Event, Tenant, Widget
from app.services.agent_graph import (
    _story_snippet,
    apply_feedback_adjustment,
    contextual_reason,
    prepare_retrieval_recommendation,
    rerank_by_lexical_overlap,
)
from app.services.mesh import NarrativeResult
from app.services.recommendation import FeedbackRecord
from app.services.widgets import create_widget


class FakeVectorStore:
    """Returns a weak match on the first call and a strong match on the second — proves
    grade_refine (AGT-4) actually retries with a broadened query rather than accepting a
    poor first result."""

    def __init__(self, weak_id: int, strong_id: int) -> None:
        self.weak_id = weak_id
        self.strong_id = strong_id
        self.calls: list[str] = []

    def query_scored(
        self, text: str, widget_id: int, limit: int = 5, where: dict | None = None
    ):
        self.calls.append(text)
        if len(self.calls) == 1:
            return [(self.weak_id, 1.9)]
        return [(self.strong_id, 0.3)]


def _make_session_factory(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        chroma_db_path=str(tmp_path / "chroma"),
    )
    return build_session_factory(settings)


def _make_tenant(session) -> Tenant:
    tenant = Tenant(name="Test Tenant")
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return tenant


def _make_widget(session, tenant: Tenant) -> Widget:
    widget, _raw_key = create_widget(session, tenant, "Test Widget")
    return widget


def test_grade_refine_retries_on_weak_retrieval(tmp_path) -> None:
    session_factory = _make_session_factory(tmp_path)
    with session_factory() as session:
        tenant = _make_tenant(session)
        widget = _make_widget(session, tenant)
        visitor_id = "v-grade"
        weak_item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Weak Match",
            provider="Test",
            category="LLM",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        strong_item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Strong Match",
            provider="Test",
            category="LLM",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        session.add_all([weak_item, strong_item])
        session.commit()

        session.add_all(
            [
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="search",
                    metadata_json={"query": "test"},
                ),
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="model_view",
                    catalog_item_id=weak_item.id,
                    metadata_json={},
                ),
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="model_compare",
                    catalog_item_id=weak_item.id,
                    metadata_json={"explicit": True},
                ),
            ]
        )
        session.commit()

        fake_store = FakeVectorStore(weak_item.id, strong_item.id)
        recommendation = prepare_retrieval_recommendation(
            session, fake_store, widget.id, tenant.id, visitor_id, mesh_generator=None
        )

        assert (
            len(fake_store.calls) == 2
        ), "grade_refine should retry once on a weak match"
        assert recommendation is not None
        assert recommendation.catalog_item_ids == [strong_item.id]
        # 0.3 raw, minus the rerank_candidates lexical-overlap bonus against this
        # item's own document text (title/provider/category/description/tags) —
        # retrieval_meta stores the final, re-ranked distance, not the raw one.
        assert recommendation.retrieval_meta == [
            {
                "catalog_item_id": strong_item.id,
                "distance": 0.23333333333333334,
                "reason": "Matched after broadening your activity signal",
            }
        ]


def test_retrieval_meta_reason_reflects_distance_without_retry(tmp_path) -> None:
    """ "Why this" tags (DLV-2/Iteration 2) should read a plain match-strength reason off
    the retrieval distance when grade_refine never had to broaden the query."""
    session_factory = _make_session_factory(tmp_path)
    with session_factory() as session:
        tenant = _make_tenant(session)
        widget = _make_widget(session, tenant)
        visitor_id = "v-strong"
        item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Immediate Match",
            provider="Test",
            category="LLM",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        session.add(item)
        session.commit()

        session.add(
            Event(
                tenant_id=tenant.id,
                widget_id=widget.id,
                visitor_id=visitor_id,
                event_type="search",
                metadata_json={"query": "test"},
            )
        )
        session.commit()

        class StrongFirstTryStore:
            def query_scored(
                self,
                text: str,
                tenant_id: int,
                limit: int = 5,
                where: dict | None = None,
            ):
                return [(item.id, 0.4)]

        recommendation = prepare_retrieval_recommendation(
            session,
            StrongFirstTryStore(),
            widget.id,
            tenant.id,
            visitor_id,
            mesh_generator=None,
        )

        assert recommendation is not None
        # 0.4 raw, minus the rerank_candidates lexical-overlap bonus (the search query
        # "test" matches this item's provider "Test" in its document text).
        assert recommendation.retrieval_meta == [
            {
                "catalog_item_id": item.id,
                "distance": 0.30000000000000004,
                "reason": "Strong match to your recent activity",
            }
        ]


def test_retrieval_applies_category_filter_on_first_pass_only(tmp_path) -> None:
    """Retrieval polish (Iteration 3): browsing 2+ Voice items in one session should
    pre-filter the first retrieval call to `category=Voice`, but a retry (triggered here by
    a weak first-pass match) drops the filter again since the query text is already being
    broadened."""
    session_factory = _make_session_factory(tmp_path)
    with session_factory() as session:
        tenant = _make_tenant(session)
        widget = _make_widget(session, tenant)
        visitor_id = "v-filter"
        voice_a = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Voice A",
            provider="Test",
            category="Voice",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        voice_b = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Voice B",
            provider="Test",
            category="Voice",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        session.add_all([voice_a, voice_b])
        session.commit()

        session.add_all(
            [
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="model_view",
                    catalog_item_id=voice_a.id,
                    metadata_json={},
                ),
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="model_view",
                    catalog_item_id=voice_b.id,
                    metadata_json={},
                ),
            ]
        )
        session.commit()

        class RecordingStore:
            def __init__(self) -> None:
                self.wheres: list[dict | None] = []

            def query_scored(
                self,
                text: str,
                tenant_id: int,
                limit: int = 5,
                where: dict | None = None,
            ):
                self.wheres.append(where)
                if len(self.wheres) == 1:
                    return [(voice_a.id, 1.9)]  # weak -> forces a retry
                return [(voice_a.id, 0.3)]

        store = RecordingStore()
        recommendation = prepare_retrieval_recommendation(
            session, store, widget.id, tenant.id, visitor_id, mesh_generator=None
        )

        assert recommendation is not None
        assert store.wheres == [{"category": "Voice"}, None]


def test_grade_refine_stops_after_max_retries_with_no_candidates(tmp_path) -> None:
    session_factory = _make_session_factory(tmp_path)
    with session_factory() as session:
        tenant = _make_tenant(session)
        widget = _make_widget(session, tenant)
        visitor_id = "v-empty"

        session.add_all(
            [
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="search",
                    metadata_json={"query": "a b c d e"},
                ),
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="search",
                    metadata_json={"query": "f g h"},
                ),
                Event(
                    tenant_id=tenant.id,
                    widget_id=widget.id,
                    visitor_id=visitor_id,
                    event_type="search",
                    metadata_json={"query": "i j k"},
                ),
            ]
        )
        session.commit()

        class EmptyVectorStore:
            def __init__(self) -> None:
                self.calls = 0

            def query_scored(
                self,
                text: str,
                tenant_id: int,
                limit: int = 5,
                where: dict | None = None,
            ):
                self.calls += 1
                return []

        empty_store = EmptyVectorStore()
        recommendation = prepare_retrieval_recommendation(
            session, empty_store, widget.id, tenant.id, visitor_id, mesh_generator=None
        )

        # Initial attempt + MAX_RETRIES(=2) retries, then give up without storing anything.
        assert empty_store.calls == 3
        assert recommendation is None


def _item(
    id,
    title,
    category,
    use_case_tags=None,
    description="d",
    story=None,
):
    return CatalogItem(
        id=id,
        title=title,
        category=category,
        provider="Test",
        price="$0",
        description=description,
        use_case_tags=use_case_tags or [],
        story=story,
    )


def test_contextual_reason_matches_search_term_to_use_case_tag() -> None:
    candidate = _item(1, "Multilingual TTS", "Voice", use_case_tags=["multilingual"])
    evidence = [
        {
            "action": "searched",
            "label": '"multilingual"',
            "catalog_item": None,
            "created_at": datetime.utcnow(),
        },
    ]
    assert (
        contextual_reason(candidate, 0.5, False, evidence)
        == 'matches your "multilingual" search'
    )


def test_contextual_reason_falls_back_to_distance_reason() -> None:
    candidate = _item(1, "Plain Item", "LLM")
    assert (
        contextual_reason(candidate, 0.5, False, [])
        == "Strong match to your recent activity"
    )
    assert (
        contextual_reason(candidate, 0.5, True, [])
        == "Matched after broadening your activity signal"
    )


def test_contextual_reason_prefers_story_over_distance_fallback() -> None:
    candidate = _item(
        1, "Plain Item", "LLM", story="Pick this when cost matters more than speed."
    )
    assert (
        contextual_reason(candidate, 0.5, False, [])
        == "Pick this when cost matters more than speed."
    )


def test_contextual_reason_prefers_search_match_over_story() -> None:
    candidate = _item(
        1,
        "Multilingual TTS",
        "Voice",
        use_case_tags=["multilingual"],
        story="Some curator story that should be ranked lower than a search match.",
    )
    evidence = [
        {
            "action": "searched",
            "label": '"multilingual"',
            "catalog_item": None,
            "created_at": datetime.utcnow(),
        },
    ]
    assert (
        contextual_reason(candidate, 0.5, False, evidence)
        == 'matches your "multilingual" search'
    )


def test_story_snippet_truncates_to_word_boundary() -> None:
    long_story = "A" * 40 + " " + "B" * 40
    snippet = _story_snippet(long_story, max_len=45)
    assert snippet == "A" * 40 + "…"
    assert len(snippet) <= 46


def test_story_snippet_returns_none_for_missing_or_blank_story() -> None:
    assert _story_snippet(None) is None
    assert _story_snippet("   ") is None


def test_rerank_promotes_lexically_matching_candidate() -> None:
    # candidate 2 starts behind candidate 1 on raw distance, but its document text
    # exactly matches every query term — the lexical bonus should promote it ahead.
    scored = [(1, 0.5), (2, 0.6)]
    documents_by_id = {
        1: "Generic Item. SomeCo. LLM. A general purpose assistant.",
        2: "Voice Fast. Cartesia. Voice. Low-latency real-time voice synthesis.",
    }
    reranked = rerank_by_lexical_overlap(
        scored, "real-time voice synthesis", documents_by_id
    )
    assert [catalog_item_id for catalog_item_id, _ in reranked] == [2, 1]


def test_rerank_leaves_order_unchanged_with_no_lexical_overlap() -> None:
    scored = [(1, 0.5), (2, 0.6)]
    documents_by_id = {
        1: "Alpha. Providerone. LLM. Something.",
        2: "Beta. Providertwo. LLM. Something else.",
    }
    reranked = rerank_by_lexical_overlap(scored, "zzz nonexistent qqq", documents_by_id)
    assert reranked == scored


def test_rerank_returns_unchanged_for_blank_query() -> None:
    scored = [(1, 0.5), (2, 0.6)]
    assert rerank_by_lexical_overlap(scored, "   ", {}) == scored


def test_rerank_bonus_is_capped_at_configured_weight() -> None:
    scored = [(1, 1.0)]
    documents_by_id = {1: "voice real time"}
    reranked = rerank_by_lexical_overlap(
        scored, "voice real time", documents_by_id, bonus_weight=0.3
    )
    assert reranked == [(1, 0.7)]


def test_feedback_downvote_penalizes_and_reorders() -> None:
    # Candidate 1 starts ahead on raw distance, but was previously downvoted — the
    # penalty should push it behind candidate 2. No context_query recorded, so it
    # applies regardless of the current query (fail-open for legacy feedback).
    scored = [(1, 0.5), (2, 0.6)]
    reranked = apply_feedback_adjustment(
        scored, {1: FeedbackRecord(rating="down", context_query="")}
    )
    assert [catalog_item_id for catalog_item_id, _ in reranked] == [2, 1]


def test_feedback_upvote_gives_a_smaller_bonus_than_downvote_penalty() -> None:
    scored = [(1, 0.5)]
    up = apply_feedback_adjustment(
        scored, {1: FeedbackRecord(rating="up", context_query="")}
    )
    down = apply_feedback_adjustment(
        scored, {1: FeedbackRecord(rating="down", context_query="")}
    )
    assert up == [(1, 0.5 - 0.4)]
    assert down == [(1, 0.5 + 1.0)]
    # Asymmetric on purpose: a downvote is meant to weigh more than an upvote.
    assert (down[0][1] - 0.5) > (0.5 - up[0][1])


def test_feedback_adjustment_ignores_unrated_candidates() -> None:
    scored = [(1, 0.5), (2, 0.6)]
    assert apply_feedback_adjustment(scored, {}) == scored
    assert (
        apply_feedback_adjustment(
            scored, {99: FeedbackRecord(rating="down", context_query="")}
        )
        == scored
    )


def test_feedback_does_not_carry_over_to_a_dissimilar_query() -> None:
    # A downvote given while searching for a "rack based server item" should not
    # suppress the same item when the user later genuinely searches for "voice".
    scored = [(1, 0.5)]
    feedback = {
        1: FeedbackRecord(rating="down", context_query="rack based server item")
    }
    assert (
        apply_feedback_adjustment(scored, feedback, "voice assistant realtime")
        == scored
    )


def test_feedback_carries_over_to_a_similar_query() -> None:
    scored = [(1, 0.5)]
    feedback = {1: FeedbackRecord(rating="down", context_query="voice assistant item")}
    reranked = apply_feedback_adjustment(scored, feedback, "looking for a voice item")
    assert reranked == [(1, 0.5 + 1.0)]


def test_generated_recommendation_persists_mesh_audit_fields(tmp_path) -> None:
    """P1-4: model + raw prompt/response must land on the stored Recommendation row,
    not just the retrieval-only fields — this is the durable audit trail when
    LangSmith tracing (opt-in, off by default) isn't turned on."""
    session_factory = _make_session_factory(tmp_path)
    with session_factory() as session:
        tenant = _make_tenant(session)
        widget = _make_widget(session, tenant)
        visitor_id = "v-audit"
        item = CatalogItem(
            tenant_id=tenant.id,
            widget_id=widget.id,
            title="Travel Card",
            provider="Acme",
            category="LLM",
            price="$0",
            description="d",
            use_case_tags=[],
        )
        session.add(item)
        session.commit()
        session.add(
            Event(
                tenant_id=tenant.id,
                widget_id=widget.id,
                visitor_id=visitor_id,
                event_type="search",
                metadata_json={"query": "test"},
            )
        )
        session.commit()

        class NarrativeMeshGenerator:
            enabled = True

            def generate(self, behavior_summary, candidates):
                return NarrativeResult(
                    narrative="ok",
                    catalog_item_ids=[item.id],
                    model="gpt-4o-mini",
                    raw_prompt='[{"role": "system", "content": "..."}]',
                    raw_response='{"activity_understanding": "ok"}',
                )

        store = FakeVectorStore(item.id, item.id)
        recommendation = prepare_retrieval_recommendation(
            session, store, widget.id, tenant.id, visitor_id, NarrativeMeshGenerator()
        )

        assert recommendation is not None
        assert recommendation.mesh_model == "gpt-4o-mini"
        assert (
            recommendation.mesh_raw_prompt == '[{"role": "system", "content": "..."}]'
        )
        assert recommendation.mesh_raw_response == '{"activity_understanding": "ok"}'


def test_agent_pipeline_trace_never_receives_secrets_as_traced_inputs(
    tmp_path, monkeypatch
) -> None:
    """Regression test for a real credential leak: LangSmith's @traceable serializes
    every one of a decorated function's own bound arguments as that run's "inputs" —
    confirmed live that a mesh_generator object passed directly put a real Mesh
    api_key in plaintext into every agent_pipeline trace. session/vector_store/
    mesh_generator must never be parameters of the traced function; only
    tenant_id/visitor_id/trigger_reason (safe primitives) may be, and the run must be
    tagged both visitor:<id> and tenant:<id>."""
    captured: dict = {}

    def fake_traceable(*_args, **kwargs):
        def decorator(func):
            if kwargs.get("name") == "agent_pipeline":
                captured["params"] = list(inspect.signature(func).parameters)
                captured["tags"] = kwargs.get("tags")
            return func

        return decorator

    import app.services.agent_graph as agent_graph_module

    monkeypatch.setattr(agent_graph_module, "traceable", fake_traceable)

    session_factory = _make_session_factory(tmp_path)
    with session_factory() as session:
        tenant = _make_tenant(session)
        widget = _make_widget(session, tenant)
        tenant_id = tenant.id  # captured before the session closes below
        widget_id = widget.id
        visitor_id = "v-secret-check"

        class LeakyMeshGenerator:
            enabled = False
            api_key = "sentinel-should-never-be-traced"

            def generate(self, *args, **kwargs):
                # Never actually called (enabled=False short-circuits before this) —
                # LangGraph's own static analysis of _generate_narrative's closure
                # (get_function_nonlocals) needs this attribute to exist regardless.
                raise AssertionError("should not be called when enabled=False")

        prepare_retrieval_recommendation(
            session,
            FakeVectorStore(1, 1),
            widget_id,
            tenant_id,
            visitor_id,
            mesh_generator=LeakyMeshGenerator(),
        )

    assert captured["params"] == [
        "widget_id",
        "tenant_id",
        "visitor_id",
        "trigger_reason",
    ]
    assert "session" not in captured["params"]
    assert "vector_store" not in captured["params"]
    assert "mesh_generator" not in captured["params"]
    assert captured["tags"] == [f"visitor:{visitor_id}", f"widget:{widget_id}"]

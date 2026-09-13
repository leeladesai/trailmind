"""The 6-node recommendation pipeline:
analyze -> retrieve -> rerank -> grade/refine -> generate -> store.

Each stage is a plain function over a shared state dict, wired together with LangGraph so the
pipeline is a named, testable graph (NFR-5) rather than one monolithic function. `should_trigger`
(app/services/recommendation.py) stays outside this graph — it's the cheap, synchronous SQL check
that decides whether to run the graph at all (AGT-1), so a per-event LLM call never happens.
"""

import logging
import re
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, StateGraph
from langsmith import traceable
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CatalogItem, Recommendation
from app.services.mesh import NarrativeResult
from app.services.recommendation import (
    FeedbackRecord,
    _summarize_bucket,
    activity_hash,
    activity_summary,
    dominant_category,
    is_recommendation_stale,
    recent_events,
    recent_feedback_by_catalog_item,
    session_bucket_events,
    session_evidence,
)
from app.vector import CatalogItemVectorStore


logger = logging.getLogger(__name__)

RETRIEVAL_TOP_K = 8
MAX_RETRIES = 2

# Empirically calibrated against the configured embedding function (app/vector.py) —
# real Mesh embeddings when configured, the deterministic hashed bag-of-words fallback
# otherwise. Checked live against both: exact-text matches score ~0.3, close
# paraphrases ~0.8-0.9, genuinely unrelated queries 1.5+. Not a universal constant —
# re-verify if the embedding model changes.
WEAK_RETRIEVAL_DISTANCE = 1.5
STRONG_RETRIEVAL_DISTANCE = 0.9

# Retrieval polish: how much a perfect lexical term match can shave off a candidate's
# embedding distance during re-ranking. Additive, not a rescale, so results stay in the
# same distance units the WEAK/STRONG thresholds above are calibrated against.
RERANK_LEXICAL_BONUS = 0.3

# Explicit feedback loop: asymmetric on purpose — a downvote ("don't show me this
# again") is a stronger, more deliberate signal than an upvote ("yes, more like
# this"), so the penalty is larger than the bonus. Same additive distance units as
# the lexical rerank above.
FEEDBACK_DOWN_PENALTY = 1.0
FEEDBACK_UP_BONUS = 0.4

# A rating only carries forward to a future query if that query shares at least this
# fraction of its terms with the query the feedback was originally given on (see
# `_feedback_context_matches`) — e.g. a downvote on a voice model shown for a
# "rack-based" search shouldn't suppress that same model when the user later
# genuinely searches for voice models. Deliberately low: this is a floor to catch
# genuinely unrelated scenarios, not a strict topic match.
FEEDBACK_CONTEXT_OVERLAP_THRESHOLD = 0.2

# AGT-8, via answer_visitor_question: the fixed response for a widget follow-up
# question with no groundable catalog match — deliberately never reaches the LLM
# (see answer_visitor_question's docstring), so this exact string is also the
# contract test coverage checks for, not just a UX default.
NO_ANSWER_MESSAGE = "I don't have that information in this catalog."


class AgentState(TypedDict, total=False):
    widget_id: int
    # Carried alongside widget_id only for _store_and_deliver's Recommendation row,
    # which keeps both columns (widget_id is the real retrieval scope; tenant_id is
    # for tenant-wide admin aggregation — see Recommendation's docstring).
    tenant_id: int
    visitor_id: str
    trigger_reason: str
    behavior_summary: str
    retrieval_query: str
    refined_query: str
    activity_hash: str
    retry_count: int
    category_filter: str | None
    evidence: list[dict]
    candidates_scored: list[tuple[int, float]]
    query_refined: bool
    grade_action: str
    narrative: str | None
    catalog_item_ids: list[int]
    retrieval_meta: list[dict]
    mesh_latency_ms: float | None
    mesh_prompt_tokens: int | None
    mesh_completion_tokens: int | None
    mesh_cost_usd: float | None
    mesh_model: str | None
    mesh_raw_prompt: str | None
    mesh_raw_response: str | None
    short_circuit: bool
    recommendation: Recommendation | None


def retrieval_reason(distance: float, query_refined: bool) -> str:
    """AGT-4 grounding, surfaced to the user: a plain-language "why this" tag derived
    from the Chroma distance (lower = more similar) rather than a canned string."""
    if query_refined:
        return "Matched after broadening your activity signal"
    if distance <= STRONG_RETRIEVAL_DISTANCE:
        return "Strong match to your recent activity"
    if distance <= WEAK_RETRIEVAL_DISTANCE:
        return "Related to your recent activity"
    return "Broader catalog match"


def _story_snippet(story: str | None, max_len: int = 60) -> str | None:
    """The curator's own "why this item" copy (`CatalogItem.story`) is a genuine,
    authored reason — a better fallback than a vague distance label when nothing in
    the user's own activity grounds this pick (see the "related to your recent
    activity" feedback this was added from). Truncated to a word boundary so every
    card's badge stays a consistent width regardless of how long a given curator wrote
    their story."""
    if not story or not story.strip():
        return None
    text = story.strip()
    if len(text) <= max_len:
        return text
    truncated = text[:max_len].rsplit(" ", 1)[0]
    return f"{truncated}…" if truncated else None


def contextual_reason(
    candidate: CatalogItem, distance: float, query_refined: bool, evidence: list[dict]
) -> str:
    """Grounds `why_this` in what the user actually did this session, computed
    deterministically from real fields (never asked of the LLM — see the "story" field
    grounding discussion this was added from). Tries, in order: a search term matching
    this candidate's own use-case tags/description, then the curator's own authored
    "story" for this item. Falls back to the distance-only `retrieval_reason` only when
    none of that exists — never invents a reason with no real backing.

    A past version of this also tried a "beats X on latency" comparison first, backed
    by the old fixed `latency_ms` field — removed along with that field (see
    CatalogItem's docstring): `specs` is now arbitrary per-tenant label/value pairs
    with no guaranteed numeric "latency" key to compare candidates on.
    """
    tags = [tag.lower() for tag in (candidate.use_case_tags or [])]
    description = (candidate.description or "").lower()
    for item in evidence:
        if item["action"] != "searched":
            continue
        term = item["label"].strip('"').lower()
        if not term:
            continue
        if any(term in tag or tag in term for tag in tags) or term in description:
            return f"matches your {item['label']} search"

    story_reason = _story_snippet(candidate.story)
    if story_reason:
        return story_reason

    return retrieval_reason(distance, query_refined)


def _analyze_activity(session: Session):
    @traceable(run_type="chain", name="analyze_activity")
    def node(state: AgentState) -> AgentState:
        widget_id = state["widget_id"]
        events = recent_events(session, widget_id, state["visitor_id"])
        summary = activity_summary(session, widget_id, events)
        event_hash = activity_hash(events)
        latest = session.scalar(
            select(Recommendation)
            .where(
                Recommendation.widget_id == widget_id,
                Recommendation.visitor_id == state["visitor_id"],
            )
            .order_by(Recommendation.created_at.desc())
        )
        # AGT-6: unchanged behavior (hash match) or still inside the last run's cooldown
        # window skips a redundant run — same check `should_trigger` (recommendation.py)
        # already runs before even scheduling this background task, so the two stay
        # consistent by construction rather than by keeping duplicated logic in sync.
        if not is_recommendation_stale(events, latest):
            return {**state, "short_circuit": True}

        buckets = session_bucket_events(events)
        current_bucket = buckets[0] if buckets else []
        older_events = [event for bucket in buckets[1:] for event in bucket]
        current_text = _summarize_bucket(session, widget_id, current_bucket)
        older_text = _summarize_bucket(session, widget_id, older_events)
        # Weight the current session 2x relative to older sessions in the retrieval
        # query text (via repetition) — the deterministic hashed bag-of-words embedding
        # in app/vector.py has no real semantics to lean on, so query-text weighting via
        # repetition is how the current session dominates retrieval.
        retrieval_parts = [current_text, current_text]
        if older_text:
            retrieval_parts.append(older_text)
        retrieval_query = " ".join(part for part in retrieval_parts if part) or summary

        return {
            **state,
            "behavior_summary": summary,
            "retrieval_query": retrieval_query,
            "activity_hash": event_hash,
            "retry_count": 0,
            "category_filter": dominant_category(session, widget_id, events),
            "evidence": session_evidence(session, widget_id, events),
            "short_circuit": False,
        }

    return node


def _retrieve_catalog_items(vector_store: CatalogItemVectorStore):
    @traceable(run_type="retriever", name="retrieve_catalog_items")
    def node(state: AgentState) -> AgentState:
        query = (
            state.get("refined_query")
            or state.get("retrieval_query")
            or state["behavior_summary"]
        )
        # Only pre-filter on the first pass — a retry already broadens the query text
        # because the narrower search came back weak, so keep the candidate pool wide too.
        category_filter = (
            state.get("category_filter") if state.get("retry_count") == 0 else None
        )
        where = {"category": category_filter} if category_filter else None
        scored = vector_store.query_scored(
            query, state["widget_id"], limit=RETRIEVAL_TOP_K, where=where
        )
        return {**state, "candidates_scored": scored}

    return node


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def rerank_by_lexical_overlap(
    scored: list[tuple[int, float]],
    query: str,
    documents_by_id: dict[int, str],
    bonus_weight: float = RERANK_LEXICAL_BONUS,
) -> list[tuple[int, float]]:
    """Retrieval polish: hybrid dense+sparse re-ranking. Chroma's embedding distance
    (dense) already ranks candidates by semantic similarity; this nudges that ranking
    using lexical term overlap (sparse) against each candidate's own catalog text — a
    candidate that shares an exact, specific term with the query (e.g. a search term
    matching its use-case tags) isn't outranked by one that's only broadly semantically
    similar. The bonus is additive and capped at `bonus_weight`, not a full rescale, so
    the result stays in the same distance units grade_refine's WEAK/STRONG thresholds
    are calibrated against — no separate recalibration needed. Pure and deterministic:
    no LLM or network call, so NFR-2 (>=1 LLM call per trigger) is unaffected.
    """
    query_terms = _tokenize(query)
    if not query_terms:
        return scored
    reranked = [
        (
            catalog_item_id,
            distance
            - bonus_weight
            * (
                len(query_terms & _tokenize(documents_by_id.get(catalog_item_id, "")))
                / len(query_terms)
            ),
        )
        for catalog_item_id, distance in scored
    ]
    reranked.sort(key=lambda pair: pair[1])
    return reranked


def _feedback_context_matches(
    current_query_terms: set[str], context_query: str
) -> bool:
    """Whether a past rating's context is close enough to the current query to carry
    forward. Fails open (applies the rating) when there's nothing to compare — a
    feedback event recorded before recommendations were linked (no context_query), or
    a current query with no extractable terms — so old behavior is preserved rather
    than silently going inert.
    """
    if not context_query or not current_query_terms:
        return True
    context_terms = _tokenize(context_query)
    if not context_terms:
        return True
    overlap = len(current_query_terms & context_terms) / len(current_query_terms)
    return overlap >= FEEDBACK_CONTEXT_OVERLAP_THRESHOLD


def apply_feedback_adjustment(
    scored: list[tuple[int, float]],
    feedback_by_catalog_item_id: dict[int, FeedbackRecord],
    current_query: str = "",
) -> list[tuple[int, float]]:
    """Closes the recommendation loop: an explicit thumbs up/down on a past
    recommendation card (recorded as an `Event`, see
    `recommendation.recent_feedback_by_catalog_item`) adjusts this candidate's
    distance before final selection — a downvoted item doesn't just get relabeled, it
    genuinely ranks worse (and, past WEAK_RETRIEVAL_DISTANCE, effectively drops out)
    the next time it's retrieved. Additive, same distance units as the lexical rerank
    above; asymmetric (down penalizes more than up rewards) — see the constants'
    docstring for why.

    Scoped to context: a rating only applies if `current_query` is similar enough to
    the query it was originally given under (see `_feedback_context_matches`) — a
    rating is an opinion about "this item for that kind of ask", not a verdict on the
    item in general.
    """
    if not feedback_by_catalog_item_id:
        return scored
    current_query_terms = _tokenize(current_query)
    adjusted = []
    for catalog_item_id, distance in scored:
        record = feedback_by_catalog_item_id.get(catalog_item_id)
        if record is not None and _feedback_context_matches(
            current_query_terms, record.context_query
        ):
            if record.rating == "down":
                distance += FEEDBACK_DOWN_PENALTY
            elif record.rating == "up":
                distance -= FEEDBACK_UP_BONUS
        adjusted.append((catalog_item_id, distance))
    adjusted.sort(key=lambda pair: pair[1])
    return adjusted


def _rerank_candidates(session: Session):
    @traceable(run_type="chain", name="rerank_candidates")
    def node(state: AgentState) -> AgentState:
        scored = state.get("candidates_scored", [])
        if not scored:
            return state
        catalog_item_ids = [catalog_item_id for catalog_item_id, _ in scored]
        documents_by_id = {
            item.id: CatalogItemVectorStore.document(item)
            for item in session.scalars(
                select(CatalogItem).where(
                    CatalogItem.id.in_(catalog_item_ids),
                    CatalogItem.widget_id == state["widget_id"],
                )
            ).all()
        }
        query = (
            state.get("refined_query")
            or state.get("retrieval_query")
            or state["behavior_summary"]
        )
        reranked = rerank_by_lexical_overlap(scored, query, documents_by_id)
        feedback_by_catalog_item_id = recent_feedback_by_catalog_item(
            session, state["widget_id"], state["visitor_id"]
        )
        reranked = apply_feedback_adjustment(
            reranked, feedback_by_catalog_item_id, query
        )
        return {**state, "candidates_scored": reranked}

    return node


@traceable(run_type="chain", name="grade_refine")
def _grade_refine(state: AgentState) -> AgentState:
    """AGT-4: bounded retry (max 2) when retrieval quality is weak."""
    scored = state.get("candidates_scored", [])
    retry_count = state.get("retry_count", 0)
    best_distance = min((distance for _, distance in scored), default=None)
    weak = best_distance is None or best_distance > WEAK_RETRIEVAL_DISTANCE

    if weak and retry_count < MAX_RETRIES:
        # Broaden the query for the next retrieval pass by dropping its most specific
        # clause (the tail of the summary tends to carry the narrowest detail). Broaden
        # from the same weighted retrieval_query text that's actually driving retrieval,
        # not the display-only behavior_summary.
        base_query = state.get("retrieval_query") or state["behavior_summary"]
        words = base_query.split()
        broadened = (
            " ".join(words[: max(3, len(words) * 2 // 3)])
            if len(words) > 3
            else base_query
        )
        logger.info(
            "Weak retrieval (best_distance=%s) for visitor_id=%s; retry %s/%s with a broadened query",
            best_distance,
            state["visitor_id"],
            retry_count + 1,
            MAX_RETRIES,
        )
        return {
            **state,
            "retry_count": retry_count + 1,
            "refined_query": broadened,
            "query_refined": True,
            "grade_action": "retry",
        }
    return {**state, "grade_action": "proceed"}


def _generate_narrative(session: Session, mesh_generator):
    @traceable(run_type="chain", name="generate_narrative")
    def node(state: AgentState) -> AgentState:
        catalog_item_ids = [
            catalog_item_id for catalog_item_id, _ in state.get("candidates_scored", [])
        ]
        catalog_items_by_id = (
            {
                item.id: item
                for item in session.scalars(
                    select(CatalogItem).where(
                        CatalogItem.id.in_(catalog_item_ids),
                        CatalogItem.widget_id == state["widget_id"],
                    )
                ).all()
            }
            if catalog_item_ids
            else {}
        )
        ordered_ids = [
            catalog_item_id
            for catalog_item_id in catalog_item_ids
            if catalog_item_id in catalog_items_by_id
        ]

        narrative: str | None = None
        final_ids = ordered_ids
        mesh_latency_ms: float | None = None
        mesh_prompt_tokens: int | None = None
        mesh_completion_tokens: int | None = None
        mesh_cost_usd: float | None = None
        mesh_model: str | None = None
        mesh_raw_prompt: str | None = None
        mesh_raw_response: str | None = None
        if mesh_generator is not None and mesh_generator.enabled and ordered_ids:
            candidates = [
                {
                    "id": catalog_items_by_id[catalog_item_id].id,
                    "title": catalog_items_by_id[catalog_item_id].title,
                    "provider": catalog_items_by_id[catalog_item_id].provider,
                    "category": catalog_items_by_id[catalog_item_id].category,
                    "price": catalog_items_by_id[catalog_item_id].price,
                    "specs": catalog_items_by_id[catalog_item_id].specs,
                    "use_case_tags": catalog_items_by_id[catalog_item_id].use_case_tags,
                    "description": catalog_items_by_id[catalog_item_id].description,
                    "story": catalog_items_by_id[catalog_item_id].story,
                }
                for catalog_item_id in ordered_ids
            ]
            try:
                result = mesh_generator.generate(state["behavior_summary"], candidates)
            except Exception:
                logger.exception(
                    "Mesh narrative generation failed for visitor_id=%s; "
                    "leaving recommendation retrieval-only",
                    state["visitor_id"],
                )
            else:
                if isinstance(result, NarrativeResult):
                    narrative = result.narrative
                    generated_ids = result.catalog_item_ids
                    mesh_latency_ms = result.latency_ms
                    mesh_prompt_tokens = result.prompt_tokens
                    mesh_completion_tokens = result.completion_tokens
                    mesh_cost_usd = result.cost_usd
                    mesh_model = result.model
                    mesh_raw_prompt = result.raw_prompt
                    mesh_raw_response = result.raw_response
                elif isinstance(result, dict):
                    narrative = str(result.get("narrative", ""))
                    generated_ids = result.get("catalog_item_ids", [])
                else:
                    narrative, generated_ids = str(result), []
                candidate_id_set = set(ordered_ids)
                filtered = [
                    int(catalog_item_id)
                    for catalog_item_id in generated_ids
                    if str(catalog_item_id).isdigit()
                    and int(catalog_item_id) in candidate_id_set
                ]
                final_ids = filtered or ordered_ids

        distances = dict(state.get("candidates_scored", []))
        query_refined = state.get("query_refined", False)
        evidence = state.get("evidence", [])
        retrieval_meta = [
            {
                "catalog_item_id": catalog_item_id,
                "distance": distances.get(catalog_item_id),
                "reason": contextual_reason(
                    catalog_items_by_id[catalog_item_id],
                    distances.get(catalog_item_id, WEAK_RETRIEVAL_DISTANCE),
                    query_refined,
                    evidence,
                ),
            }
            for catalog_item_id in final_ids
            if catalog_item_id in catalog_items_by_id
        ]

        return {
            **state,
            "catalog_item_ids": final_ids,
            "narrative": narrative,
            "retrieval_meta": retrieval_meta,
            "mesh_latency_ms": mesh_latency_ms,
            "mesh_prompt_tokens": mesh_prompt_tokens,
            "mesh_completion_tokens": mesh_completion_tokens,
            "mesh_cost_usd": mesh_cost_usd,
            "mesh_model": mesh_model,
            "mesh_raw_prompt": mesh_raw_prompt,
            "mesh_raw_response": mesh_raw_response,
        }

    return node


def _store_and_deliver(session: Session, push_callback=None):
    @traceable(run_type="chain", name="store_and_deliver")
    def node(state: AgentState) -> AgentState:
        recommendation = Recommendation(
            tenant_id=state["tenant_id"],
            widget_id=state["widget_id"],
            visitor_id=state["visitor_id"],
            catalog_item_ids=state.get("catalog_item_ids") or [],
            retrieval_meta=state.get("retrieval_meta") or [],
            narrative=state.get("narrative"),
            mesh_latency_ms=state.get("mesh_latency_ms"),
            mesh_prompt_tokens=state.get("mesh_prompt_tokens"),
            mesh_completion_tokens=state.get("mesh_completion_tokens"),
            mesh_cost_usd=state.get("mesh_cost_usd"),
            mesh_model=state.get("mesh_model"),
            mesh_raw_prompt=state.get("mesh_raw_prompt"),
            mesh_raw_response=state.get("mesh_raw_response"),
            behavior_summary=state["behavior_summary"],
            activity_hash=state["activity_hash"],
            trigger_reason=state["trigger_reason"],
        )
        session.add(recommendation)
        session.commit()
        session.refresh(recommendation)
        # DLV-2: best-effort real-time push to any open widget connection for this
        # tenant+visitor. `push_callback` is closed over rather than a graph-state
        # value for the same reason `mesh_generator` is (see
        # prepare_retrieval_recommendation's docstring) — it's a live callable, not
        # traceable-serializable data, and has nothing to do with the pipeline's own
        # inputs/outputs.
        if push_callback is not None:
            try:
                delivered = push_callback(
                    {
                        "recommendation_id": recommendation.id,
                        "narrative": recommendation.narrative,
                        "catalog_item_ids": recommendation.catalog_item_ids,
                        "retrieval_meta": recommendation.retrieval_meta,
                    }
                )
            except Exception:
                logger.exception(
                    "Widget push callback failed for widget_id=%s visitor_id=%s",
                    state["widget_id"],
                    state["visitor_id"],
                )
            else:
                if delivered:
                    recommendation.pushed_at = datetime.now(timezone.utc)
                    session.commit()
        return {**state, "recommendation": recommendation}

    return node


def build_agent_graph(
    session: Session,
    vector_store: CatalogItemVectorStore,
    mesh_generator=None,
    push_callback=None,
):
    graph = StateGraph(AgentState)
    graph.add_node("analyze", _analyze_activity(session))
    graph.add_node("retrieve", _retrieve_catalog_items(vector_store))
    graph.add_node("rerank", _rerank_candidates(session))
    graph.add_node("grade_refine", _grade_refine)
    graph.add_node("generate", _generate_narrative(session, mesh_generator))
    graph.add_node("store", _store_and_deliver(session, push_callback))

    graph.set_entry_point("analyze")
    graph.add_conditional_edges(
        "analyze",
        lambda state: "short_circuit" if state.get("short_circuit") else "proceed",
        {"short_circuit": END, "proceed": "retrieve"},
    )
    graph.add_edge("retrieve", "rerank")
    graph.add_edge("rerank", "grade_refine")
    graph.add_conditional_edges(
        "grade_refine",
        lambda state: (
            "retrieve"
            if state.get("grade_action") == "retry"
            else ("generate" if state.get("candidates_scored") else END)
        ),
        {"retrieve": "retrieve", "generate": "generate", END: END},
    )
    graph.add_edge("generate", "store")
    graph.add_edge("store", END)
    return graph.compile()


def prepare_retrieval_recommendation(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget_id: int,
    tenant_id: int,
    visitor_id: str,
    mesh_generator=None,
    trigger_reason: str = "event_threshold",
    push_callback=None,
) -> Recommendation | None:
    # `session`/`vector_store`/`mesh_generator`/`push_callback` are deliberately NOT
    # parameters of the @traceable-decorated function below — LangSmith's traceable
    # wrapper serializes every one of a traced function's *own* bound arguments (via
    # inspect.signature, never closure variables) as that run's "inputs". mesh_generator
    # in particular carries a live MeshNarrativeGenerator with a real `api_key`
    # attribute; passed directly, that key would land in plaintext in every
    # agent_pipeline trace sent to LangSmith (confirmed live — caught via a real
    # trace's raw "inputs" JSON showing the key). `push_callback` is a live callable
    # closing over the FastAPI app's event loop/connection registry — not
    # trace-serializable data either. Closing over them here instead of passing them
    # as traced params means they're used by the pipeline but never
    # introspected/serialized into the trace. `visitor_id`/`trigger_reason` stay as
    # real params — safe, and useful to see per run.
    @traceable(
        run_type="chain",
        name="agent_pipeline",
        # Tags a run's owner natively in LangSmith (queryable via
        # `list_runs(filter='has(tags, "visitor:<id>")')`, verified live against the
        # real API) rather than only being visible by re-reading raw trace inputs —
        # powers the admin observability page's per-visitor filter.
        tags=[f"visitor:{visitor_id}", f"widget:{widget_id}"],
    )
    def _run(
        widget_id: int, tenant_id: int, visitor_id: str, trigger_reason: str
    ) -> Recommendation | None:
        graph = build_agent_graph(session, vector_store, mesh_generator, push_callback)
        # LangGraph's internal step-counting consumes recursion budget faster than the
        # visible node count suggests (each named node compiles to several internal
        # supersteps) — the default limit of 25 was tight enough that adding the 6th
        # node (rerank_candidates) into the bounded retry loop (MAX_RETRIES=2, so up to
        # 3 full retrieve->rerank->grade_refine cycles) blew past it even with tracing
        # off. Sized with real headroom rather than the exact minimum so a future node
        # addition doesn't reintroduce this.
        result = graph.invoke(
            {
                "widget_id": widget_id,
                "tenant_id": tenant_id,
                "visitor_id": visitor_id,
                "trigger_reason": trigger_reason,
            },
            {"recursion_limit": 60},
        )
        return result.get("recommendation")

    return _run(widget_id, tenant_id, visitor_id, trigger_reason)


def answer_visitor_question(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget_id: int,
    visitor_id: str,
    question: str,
    mesh_generator=None,
) -> dict:
    """DLV-4: a stateless, catalog-grounded follow-up answer — not a new triggered
    recommendation (no Recommendation row, no re-run of the behavior pipeline).
    Reuses the same retrieval + lexical-rerank + grounding discipline as the main
    pipeline (AGT-5/AGT-8): whether the question is even groundable is decided by
    retrieval quality (the same WEAK_RETRIEVAL_DISTANCE threshold that gates a retry
    in the main pipeline), not left to the LLM's discretion — a question with no
    relevant catalog match never reaches the LLM at all.
    """

    @traceable(
        run_type="chain",
        name="widget_ask",
        tags=[f"visitor:{visitor_id}", f"widget:{widget_id}"],
    )
    def _run(widget_id: int, visitor_id: str, question: str) -> dict:
        scored = vector_store.query_scored(question, widget_id, limit=RETRIEVAL_TOP_K)
        best_distance = min((distance for _, distance in scored), default=None)
        if best_distance is None or best_distance > WEAK_RETRIEVAL_DISTANCE:
            return {"answer": NO_ANSWER_MESSAGE, "catalog_item_ids": []}

        catalog_item_ids = [catalog_item_id for catalog_item_id, _ in scored]
        items = session.scalars(
            select(CatalogItem).where(
                CatalogItem.id.in_(catalog_item_ids), CatalogItem.widget_id == widget_id
            )
        ).all()
        catalog_items_by_id = {item.id: item for item in items}
        documents_by_id = {
            item.id: CatalogItemVectorStore.document(item) for item in items
        }
        reranked = rerank_by_lexical_overlap(scored, question, documents_by_id)
        ordered_ids = [
            catalog_item_id
            for catalog_item_id, _ in reranked
            if catalog_item_id in catalog_items_by_id
        ]

        if mesh_generator is None or not mesh_generator.enabled or not ordered_ids:
            return {"answer": NO_ANSWER_MESSAGE, "catalog_item_ids": []}

        candidates = [
            {
                "id": catalog_items_by_id[catalog_item_id].id,
                "title": catalog_items_by_id[catalog_item_id].title,
                "provider": catalog_items_by_id[catalog_item_id].provider,
                "category": catalog_items_by_id[catalog_item_id].category,
                "price": catalog_items_by_id[catalog_item_id].price,
                "specs": catalog_items_by_id[catalog_item_id].specs,
                "use_case_tags": catalog_items_by_id[catalog_item_id].use_case_tags,
                "description": catalog_items_by_id[catalog_item_id].description,
                "story": catalog_items_by_id[catalog_item_id].story,
            }
            for catalog_item_id in ordered_ids
        ]
        try:
            result = mesh_generator.answer_question(question, candidates)
        except Exception:
            logger.exception("Mesh Q&A generation failed for visitor_id=%s", visitor_id)
            return {"answer": NO_ANSWER_MESSAGE, "catalog_item_ids": []}

        candidate_id_set = set(ordered_ids)
        filtered_ids = [
            mid for mid in result.catalog_item_ids if mid in candidate_id_set
        ]
        return {"answer": result.answer, "catalog_item_ids": filtered_ids}

    return _run(widget_id, visitor_id, question)

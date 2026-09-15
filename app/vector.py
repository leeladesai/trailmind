import hashlib
import logging
import re
from collections.abc import Sequence
from pathlib import Path

import chromadb
from openai import OpenAI

# This pinned chromadb version calls posthog's old positional capture(distinct_id, event,
# properties) signature; the installed posthog major version rewrote that to capture(event,
# **kwargs), so the call now raises a TypeError before posthog's own code ever runs — meaning
# chromadb.Settings(anonymized_telemetry=False) can't prevent it (that flag is checked inside
# posthog, past the point where the mismatched call already failed). Silencing the logger it
# reports through is what actually stops the noise; the failure itself was always harmless
# (chromadb catches it internally either way).
logging.getLogger("chromadb.telemetry.product.posthog").setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)


class DeterministicEmbeddingFunction:
    """Offline fallback embedding — used whenever Mesh isn't configured (no API key),
    so the catalog/retrieval loop still works without any external dependency. Also
    what tests use, since they deliberately run with mesh_api_key=None."""

    def __init__(self, dimension: int = 64) -> None:
        self.dimension = dimension

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in input]

    def _embed(self, text: str) -> list[float]:
        values = [0.0] * self.dimension
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:2], "big") % self.dimension
            values[index] += 1.0
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [value / norm for value in values]


class MeshEmbeddingFunction:
    """Real semantic embeddings via the Mesh API — same Chroma EmbeddingFunction
    interface as DeterministicEmbeddingFunction, so it's a drop-in replacement. One
    batched call regardless of how many texts Chroma passes in; the API preserves
    input order, so results map back to documents by position."""

    def __init__(self, client: OpenAI, model: str) -> None:
        self.client = client
        self.model = model

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=list(input))
        return [item.embedding for item in response.data]


def build_embedding_function(settings):
    """Mesh-backed embeddings when configured, the deterministic fallback otherwise —
    mirrors MeshNarrativeGenerator's own enabled/disabled pattern (app/services/mesh.py)
    so the app degrades the same way in both places rather than two different stories.
    """
    if settings.mesh_api_key:
        client = OpenAI(api_key=settings.mesh_api_key, base_url=settings.mesh_base_url)
        return MeshEmbeddingFunction(client, settings.mesh_embedding_model)
    return DeterministicEmbeddingFunction(settings.embedding_dimension)


class CatalogItemVectorStore:
    """Manages one Chroma collection per widget (docs/design/09-Platform-Pivot-Decision.md
    §5, updated for the per-widget cutover: isolation now needs to stop a "Personal
    Loans" widget from ever recommending a "Credit Cards" item, not just stop
    cross-tenant leakage — a shared collection + metadata filter would still risk that
    on a filter bug, so separate collections stay the stronger guarantee, just scoped
    one level deeper). `collection_name` is the shared prefix; the actual collection a
    call touches is always `{collection_name}_widget_{widget_id}`, created lazily on
    first use."""

    def __init__(
        self,
        path: str,
        # Matches Settings.chroma_collection_name's default ("models", unchanged by
        # the CatalogItem rename) — this is a Chroma collection name on disk, not a
        # Python identifier, and renaming it would orphan every already-synced
        # widget's vectors under the old name until a full re-sync.
        collection_name: str = "models",
        embedding_function=None,
        embedding_dimension: int = 64,
    ) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)
        self.collection_name = collection_name
        self.embedding_function = embedding_function or DeterministicEmbeddingFunction(
            embedding_dimension
        )
        # anonymized_telemetry=False: this pinned chromadb version calls posthog's old
        # positional capture() signature, which the installed posthog major version no longer
        # accepts — chromadb swallows the resulting TypeError and just logs it every client
        # init. Harmless, but disabling telemetry removes the noise (and the outbound call).
        self.client = chromadb.PersistentClient(
            path=path, settings=chromadb.Settings(anonymized_telemetry=False)
        )
        self._collections: dict[int, object] = {}

    def _collection_for(self, widget_id: int):
        collection = self._collections.get(widget_id)
        if collection is None:
            collection = self.client.get_or_create_collection(
                name=f"{self.collection_name}_widget_{widget_id}",
                embedding_function=self.embedding_function,
            )
            self._collections[widget_id] = collection
        return collection

    @staticmethod
    def document(item) -> str:
        tags = ", ".join(item.use_case_tags or [])
        story = f" {item.story}." if getattr(item, "story", None) else ""
        return (
            f"{item.title}. {item.provider}. {item.category}. "
            f"{item.description}.{story} {tags}"
        )

    def upsert(self, item, widget_id: int) -> None:
        self._collection_for(widget_id).upsert(
            ids=[str(item.id)],
            documents=[self.document(item)],
            metadatas=[
                {
                    "provider": item.provider,
                    "category": item.category,
                    "price": item.price,
                }
            ],
        )

    def delete(self, catalog_item_id: int, widget_id: int) -> None:
        self._collection_for(widget_id).delete(ids=[str(catalog_item_id)])

    def contains(self, catalog_item_id: int, widget_id: int) -> bool:
        """Whether this item's vector entry actually exists in Chroma right now —
        the reconciliation service's ground truth check. A row can have
        `vector_index_status == "synced"` from a previous successful upsert while
        Chroma's own on-disk data has since vanished (ephemeral disk on Render's
        free tier — see README); this is what catches that drift instead of
        trusting the stale flag."""
        result = self._collection_for(widget_id).get(ids=[str(catalog_item_id)])
        return len(result.get("ids", [])) > 0

    def query_scored(
        self, text: str, widget_id: int, limit: int = 5, where: dict | None = None
    ) -> list[tuple[int, float]]:
        """Like `query`, but also returns each result's distance (lower = more similar) —
        used by the grade/refine node to detect weak retrieval. `where` applies Chroma
        metadata filtering (e.g. `{"category": "Voice"}`) before the ANN search runs, not
        as a post-hoc re-rank filter. Always scoped to `widget_id`'s own collection —
        never searches across widgets, let alone tenants."""
        collection = self._collection_for(widget_id)
        if not text.strip() or collection.count() == 0:
            return []
        try:
            results = collection.query(
                query_texts=[text], n_results=limit, where=where, include=["distances"]
            )
        except Exception:
            # A transient Mesh embedding failure here must degrade to "no candidates
            # this round" (same as an empty collection), not crash the whole
            # background pipeline run — retrieval is core to every recommendation,
            # unlike narrative generation, which already fails this gracefully.
            logger.exception("Vector store query failed; returning no candidates")
            return []
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        return [
            (int(catalog_item_id), float(distance))
            for catalog_item_id, distance in zip(ids, distances)
        ]

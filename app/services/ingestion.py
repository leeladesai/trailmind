"""Catalog ingestion adapters beyond manual entry (CAT-1..5) and the plain file
bulk-upload (app/services/catalog_import.py): a feed/API pull (ING-1) and a
DOM-scrape with preview/confirm (ING-2), on top of the existing manual adapter.

Both adapters funnel through catalog_import.import_catalog_rows /
catalog.create_catalog_item so a row ingested this way gets the exact same
validation, dedupe-by-title, and vector-store sync path as a manually entered one —
only `ingestion_adapter` and `review_status` differ per-row. All three are scoped per
widget now (feed/scrape config lives on `Widget`, not `Tenant`) — two widgets under
the same tenant can sync from two different feeds.
"""

import json
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import CatalogItem, Widget
from app.schemas import CatalogItemCreate
from app.services.catalog import create_catalog_item
from app.services.catalog_import import (
    CatalogParseError,
    _coerce_row,
    import_catalog_rows,
    parse_catalog_file,
)
from app.services.url_safety import UnsafeURLError, assert_url_is_safe
from app.vector import CatalogItemVectorStore

FETCH_TIMEOUT_SECONDS = 10.0
MAX_REDIRECTS = 5


def _safe_get(
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    follow_redirects: bool = False,
) -> httpx.Response:
    """SSRF-guarded httpx.get: re-validates the target before every fetch,
    including each redirect hop — a URL that resolves safely can still redirect
    to an internal address, and blindly following it would fetch that hop with
    no check at all."""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        assert_url_is_safe(current_url)
        response = httpx.get(
            current_url, headers=headers or {}, timeout=timeout, follow_redirects=False
        )
        if follow_redirects and getattr(response, "is_redirect", False):
            location = response.headers.get("location")
            if not location:
                return response
            current_url = str(httpx.URL(current_url).join(location))
            continue
        return response
    raise UnsafeURLError(f"Too many redirects while fetching {url!r}.")


class FeedSyncError(ValueError):
    """The feed URL couldn't be fetched or parsed. Existing feed-sourced rows are
    flagged `sync_stale` rather than removed (ING-5: serve last-known-good) —
    a row is only ever delisted by a *successful* sync that confirms it's gone
    (see _reconcile_removed_feed_rows), never by a failure."""


class ScrapeError(ValueError):
    """The page couldn't be fetched, or no Product markup/matching selector was
    found — nothing is persisted in either case (scrape/preview never writes)."""


def _set_feed_rows_stale(session: Session, widget_id: int, stale: bool) -> None:
    session.execute(
        update(CatalogItem)
        .where(
            CatalogItem.widget_id == widget_id, CatalogItem.ingestion_adapter == "feed"
        )
        .values(sync_stale=stale)
    )
    session.commit()


def _reconcile_removed_feed_rows(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget: Widget,
    raw_rows: list[dict],
) -> int:
    """A successful sync confirms the full current state of the upstream feed, so
    any previously-synced feed row whose title is no longer present has actually
    been removed upstream — not just a transient fetch failure (that case is
    `sync_stale`, set on error, never here). Such a row is "delisted" (removed from
    the vector store, excluded from retrieval) rather than hard-deleted: an Event
    or Recommendation may still reference its id for historical analytics, and
    import_catalog_rows reactivates a delisted row if its title reappears in a
    later sync instead of inserting a duplicate.
    """
    current_titles = set()
    for raw in raw_rows:
        title = _coerce_row(raw).get("title")
        if title:
            current_titles.add(str(title).strip().lower())

    stale_rows = session.scalars(
        select(CatalogItem).where(
            CatalogItem.widget_id == widget.id,
            CatalogItem.ingestion_adapter == "feed",
            CatalogItem.review_status == "approved",
        )
    ).all()
    delisted = 0
    for row in stale_rows:
        if row.title.strip().lower() in current_titles:
            continue
        vector_store.delete(row.id, widget.id)
        row.review_status = "delisted"
        row.vector_synced = False
        row.vector_index_status = "pending"
        delisted += 1
    if delisted:
        session.commit()
    return delisted


def sync_feed(
    session: Session, vector_store: CatalogItemVectorStore, widget: Widget
) -> list[dict]:
    """ING-1: fetches `widget.feed_url`, parses it with the same CSV/JSON reader the
    manual bulk-upload endpoint uses, and imports every row exactly like a manual
    bulk-upload would — just tagged `ingestion_adapter="feed"` with `last_synced_at`
    stamped now. On any fetch/parse failure, existing feed-sourced rows for this
    widget are marked `sync_stale=True` (and left in place, still serving) rather than
    raising past the caller silently — callers surface `FeedSyncError` to the admin.
    On success, any previously-synced feed row no longer present in the fresh feed
    is delisted (see _reconcile_removed_feed_rows) rather than left live indefinitely.
    """
    if not widget.feed_url:
        raise FeedSyncError("No feed URL configured for this widget.")

    headers = (
        {"Authorization": f"Bearer {widget.feed_auth_token}"}
        if widget.feed_auth_token
        else {}
    )
    try:
        response = _safe_get(
            widget.feed_url, headers=headers, timeout=FETCH_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        raw_rows = parse_catalog_file("feed.json", response.content)
    except UnsafeURLError as exc:
        _set_feed_rows_stale(session, widget.id, True)
        raise FeedSyncError(str(exc)) from exc
    except httpx.HTTPError as exc:
        _set_feed_rows_stale(session, widget.id, True)
        raise FeedSyncError(f"Could not fetch feed: {exc}") from exc
    except CatalogParseError as exc:
        _set_feed_rows_stale(session, widget.id, True)
        raise FeedSyncError(str(exc)) from exc

    results = import_catalog_rows(
        session,
        vector_store,
        widget,
        raw_rows,
        ingestion_adapter="feed",
        last_synced_at=datetime.now(timezone.utc),
    )
    _reconcile_removed_feed_rows(session, vector_store, widget, raw_rows)
    _set_feed_rows_stale(session, widget.id, False)
    return results


def _extract_json_ld_products(soup: BeautifulSoup) -> list[dict]:
    """Prefers schema.org `Product` markup — the highest-confidence signal a page
    actually describes a catalog item, and structured enough to map straight onto
    CatalogItemCreate's fields without guessing at page layout."""
    rows: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for entry in candidates:
            if isinstance(entry, dict) and entry.get("@graph"):
                candidates = candidates + entry["@graph"]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            if entry.get("@type") not in ("Product", "Offer"):
                continue
            offers = entry.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            offers = offers if isinstance(offers, dict) else {}
            brand = entry.get("brand")
            brand_name = (brand.get("name") if isinstance(brand, dict) else brand) or ""
            price = offers.get("price")
            currency = offers.get("priceCurrency", "")
            row = {
                "title": entry.get("name"),
                "description": entry.get("description") or entry.get("name") or "",
                "provider": brand_name,
                "price": f"{currency} {price}".strip() if price else "",
                "source_url": entry.get("url"),
                "category": entry.get("category") or "",
            }
            if row["title"]:
                rows.append(row)
    return rows


def _extract_via_selectors(
    soup: BeautifulSoup, selectors: dict[str, str]
) -> list[dict]:
    """Fallback for pages with no schema.org markup: a tenant-configured CSS
    selector per field, applied to a single-item page (each selector's first match)."""
    row: dict = {}
    for field, selector in selectors.items():
        element = soup.select_one(selector)
        if element is not None:
            row[field] = element.get_text(strip=True)
    return [row] if row.get("title") else []


def _default_missing_fields(row: dict) -> dict:
    row = dict(row)
    row.setdefault("category", "general")
    row["category"] = row["category"] or "general"
    row.setdefault("provider", "")
    row["provider"] = row["provider"] or "Unknown"
    row.setdefault("price", "")
    row["price"] = row["price"] or "Not listed"
    return row


def scrape_preview(url: str, selectors: dict[str, str] | None = None) -> dict:
    """ING-2: fetches `url` and extracts candidate catalog rows without persisting
    anything — the admin reviews these before `scrape_confirm` writes them. Returns
    `{"rows": [...], "markup_type": "json-ld" | "css-selector"}`.
    """
    try:
        response = _safe_get(url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
    except UnsafeURLError as exc:
        raise ScrapeError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise ScrapeError(f"Could not fetch page: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    rows = _extract_json_ld_products(soup)
    markup_type = "json-ld"
    if not rows and selectors:
        rows = _extract_via_selectors(soup, selectors)
        markup_type = "css-selector"
    if not rows:
        raise ScrapeError(
            "No schema.org Product markup found, and no matching CSS selectors "
            "configured for this page."
        )
    rows = [_default_missing_fields(row) for row in rows]
    return {"rows": rows, "markup_type": markup_type}


def scrape_confirm(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget: Widget,
    source_url: str,
    markup_type: str,
    rows: list[dict],
) -> list[dict]:
    """Persists a previewed scrape result with `review_status="pending_review"` —
    nothing here is eligible for retrieval until an admin calls
    `catalog.approve_catalog_item` (via POST /api/admin/catalog-items/{id}/approve)."""
    results = []
    for index, raw in enumerate(rows, start=1):
        fallback_title = str(raw.get("title") or "").strip() or None
        try:
            coerced = _coerce_row(raw)
        except (TypeError, ValueError) as exc:
            results.append(
                {
                    "row": index,
                    "title": fallback_title,
                    "status": "invalid",
                    "errors": [str(exc)],
                }
            )
            continue
        try:
            payload = CatalogItemCreate(**coerced)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            ]
            results.append(
                {
                    "row": index,
                    "title": fallback_title,
                    "status": "invalid",
                    "errors": errors,
                }
            )
            continue

        existing = session.scalar(
            select(CatalogItem).where(
                CatalogItem.title.ilike(payload.title),
                CatalogItem.widget_id == widget.id,
            )
        )
        if existing:
            results.append(
                {
                    "row": index,
                    "title": payload.title,
                    "status": "skipped_duplicate",
                    "errors": [],
                }
            )
            continue

        item = create_catalog_item(
            session,
            vector_store,
            widget,
            payload,
            ingestion_adapter="scrape",
            review_status="pending_review",
            last_synced_at=datetime.now(timezone.utc),
            ingestion_meta={"source_url": source_url, "markup_type": markup_type},
        )
        results.append(
            {
                "row": index,
                "title": item.title,
                "status": "pending_review",
                "errors": [],
                "catalog_item_id": item.id,
            }
        )
    return results


def ingestion_status(session: Session, widget_id: int) -> dict:
    """GET /api/admin/ingestion/status: per-adapter `last_synced_at`/`sync_stale`,
    folded into the onboarding-readiness check."""
    status: dict[str, dict] = {}
    for adapter in ("feed", "scrape"):
        rows = session.scalars(
            select(CatalogItem).where(
                CatalogItem.widget_id == widget_id,
                CatalogItem.ingestion_adapter == adapter,
            )
        ).all()
        last_synced = max(
            (row.last_synced_at for row in rows if row.last_synced_at is not None),
            default=None,
        )
        status[adapter] = {
            "count": len(rows),
            "last_synced_at": last_synced,
            "sync_stale": any(row.sync_stale for row in rows),
            "pending_review": sum(
                1 for row in rows if row.review_status == "pending_review"
            ),
        }
    return status

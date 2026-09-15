import csv
import io
import json
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CatalogItem, Widget
from app.schemas import CatalogItemCreate
from app.services.catalog import create_catalog_item, update_catalog_item
from app.vector import CatalogItemVectorStore


class CatalogParseError(ValueError):
    """The uploaded file itself couldn't be read (bad encoding, malformed JSON, no CSV
    header, empty/oversized) — distinct from a single row failing validation, which is
    reported per-row in import_catalog_rows instead of aborting the whole import.
    """


# A few hundred rows is already a large catalog for this app; caps the per-row loop
# (and the Chroma upsert calls it triggers) at something a single admin request can
# process synchronously without a background job.
MAX_BULK_IMPORT_ROWS = 500

_ROW_FIELDS = (
    "title",
    "provider",
    "category",
    "price",
    "description",
    "story",
    "source_url",
)

# Optional spec_N_label/spec_N_value column pairs a CSV can provide — collapsed into
# CatalogItem.specs (see _coerce_row). JSON uploads can instead just provide a
# "specs" object directly (handled inline in _coerce_row).
_SPEC_COLUMN_COUNT = 4


def _split_tags(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    for sep in (";", "|"):
        if sep in text:
            return [tag.strip() for tag in text.split(sep) if tag.strip()]
    # No semicolon/pipe present — fall back to comma-splitting for a single-tag CSV
    # cell or a JSON source that already used commas.
    return [tag.strip() for tag in text.split(",") if tag.strip()]


def _coerce_row(raw: dict) -> dict:
    """Normalizes one raw row into the shape CatalogItemCreate expects. CSV cells
    always arrive as strings (or missing); JSON rows may already be correctly typed.
    """
    row = {str(key).strip().lower(): value for key, value in raw.items() if key}
    coerced: dict = {}
    for field in _ROW_FIELDS:
        value = row.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
        if value == "":
            continue
        coerced[field] = value

    tags = row.get("use_case_tags")
    if isinstance(tags, list):
        coerced["use_case_tags"] = [
            str(tag).strip() for tag in tags if str(tag).strip()
        ]
    elif isinstance(tags, str) and tags.strip():
        coerced["use_case_tags"] = _split_tags(tags)

    specs_field = row.get("specs")
    if isinstance(specs_field, dict):
        coerced["specs"] = {str(k): str(v) for k, v in specs_field.items()}
    else:
        specs: dict[str, str] = {}
        for n in range(1, _SPEC_COLUMN_COUNT + 1):
            label = row.get(f"spec_{n}_label")
            value = row.get(f"spec_{n}_value")
            if isinstance(label, str):
                label = label.strip()
            if isinstance(value, str):
                value = value.strip()
            if label and value:
                specs[str(label)] = str(value)
        if specs:
            coerced["specs"] = specs

    return coerced


def parse_catalog_file(filename: str, content: bytes) -> list[dict]:
    """Accepts a CSV or JSON catalog file and returns a list of raw row dicts, not yet
    validated against CatalogItemCreate (see import_catalog_rows for that). JSON may
    be a bare array of item objects, or {"models": [...]} (envelope key kept as-is —
    matches scripts/expand_catalog_via_mesh.py's existing output format; not part of
    the CatalogItem entity/API rename).
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogParseError("File must be UTF-8 encoded text.") from exc

    if (filename or "").lower().endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CatalogParseError(f"Invalid JSON: {exc}") from exc
        if isinstance(data, dict):
            data = data.get("models")
        if not isinstance(data, list):
            raise CatalogParseError(
                'Expected a JSON array of items, or {"models": [...]}.'
            )
        rows = [row for row in data if isinstance(row, dict)]
    else:
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise CatalogParseError("CSV file has no header row.")
        rows = list(reader)

    if not rows:
        raise CatalogParseError("No rows found in the uploaded file.")
    if len(rows) > MAX_BULK_IMPORT_ROWS:
        raise CatalogParseError(f"Too many rows (max {MAX_BULK_IMPORT_ROWS}).")
    return rows


def import_catalog_rows(
    session: Session,
    vector_store: CatalogItemVectorStore,
    widget: Widget,
    raw_rows: list[dict],
    *,
    ingestion_adapter: str = "manual",
    last_synced_at: datetime | None = None,
) -> list[dict]:
    """Validates and inserts each row independently — reuses
    catalog.create_catalog_item so a bulk import writes through the exact same
    DB+vector-store path (and the same resiliency around a failed Chroma upsert) as
    the single-item admin API. One bad row never aborts the rest of the batch,
    mirroring scripts/expand_catalog_via_mesh.py's per-row validate/dedupe/insert
    pattern. Each result dict is `{row, title, status, errors}` with status one of
    "inserted", "skipped_duplicate", "invalid".

    `ingestion_adapter`/`last_synced_at` are passed straight through to
    `create_catalog_item` for every inserted row — the manual admin bulk-upload
    endpoint leaves them at their defaults, while the feed adapter
    (app/services/ingestion.py) tags its rows `ingestion_adapter="feed"` and stamps
    the sync time.
    """
    results = []
    for index, raw in enumerate(raw_rows, start=1):
        fallback_title = str(raw.get("title") or raw.get("Title") or "").strip() or None
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

        existing = session.scalars(
            select(CatalogItem).where(
                CatalogItem.title.ilike(payload.title),
                CatalogItem.widget_id == widget.id,
            )
        ).first()
        if existing:
            # A previously feed-sourced row that disappeared from an earlier sync
            # (see ingestion.py's feed reconciliation) is "delisted" rather than
            # deleted — if the same title reappears in a later sync, reactivate the
            # existing row (and its history) instead of treating it as a duplicate.
            if existing.review_status == "delisted":
                existing.ingestion_adapter = ingestion_adapter
                existing.last_synced_at = last_synced_at
                existing.review_status = "approved"
                update_catalog_item(session, vector_store, widget.id, existing, payload)
                results.append(
                    {
                        "row": index,
                        "title": payload.title,
                        "status": "reactivated",
                        "errors": [],
                    }
                )
                continue
            results.append(
                {
                    "row": index,
                    "title": payload.title,
                    "status": "skipped_duplicate",
                    "errors": [],
                }
            )
            continue

        try:
            create_catalog_item(
                session,
                vector_store,
                widget,
                payload,
                ingestion_adapter=ingestion_adapter,
                last_synced_at=last_synced_at,
            )
        except (
            Exception
        ) as exc:  # noqa: BLE001 — one row's failure must not abort the batch
            results.append(
                {
                    "row": index,
                    "title": payload.title,
                    "status": "invalid",
                    "errors": [f"Could not save: {exc}"],
                }
            )
            continue

        results.append(
            {"row": index, "title": payload.title, "status": "inserted", "errors": []}
        )
    return results

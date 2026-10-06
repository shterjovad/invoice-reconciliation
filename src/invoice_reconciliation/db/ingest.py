"""Load fixtures (purchase orders, receipts, invoices) into the database.

The database is a rebuildable artifact; ``tasks/invoices/seed.json`` is the
source of truth. This module loads the reference tables —
``purchase_orders`` and ``receipts`` — and the seeded invoice records —
``invoices`` and ``extracted_fields``.

Two ways to populate invoice field values exist side by side:

- ``ingest_invoices`` — the prepared-record path. Values are read straight
  from the fixture, as if the model had already read them. This is the
  default: the 82-test regression suite and the Slice 4 seed check depend
  on it, and it remains the path a plain batch run takes.
- ``ingest_invoices_via_extraction`` — the live path. Values come from a
  real Bedrock call against each invoice's image
  (``extraction.client.extract_invoice_fields``), parsed by
  ``extraction.parser.parse``. Selected with the CLI's ``--extract`` flag.
  One invoice's ``ExtractionError`` (or a missing image file) marks that
  invoice ``failed`` and does not stop the rest of the batch — the same
  failure-isolation contract ``pipeline.recalculate_one`` already applies
  to malformed stored money.

Idempotency: ingest is idempotent by resetting first. ``ingest_reference_data``
clears ``reconciliation_results``, then ``receipts``, then
``purchase_orders`` (in that order, respecting the foreign keys
``reconciliation_results`` holds to both receipts and purchase_orders, and
the foreign key from receipts to purchase_orders) before inserting,
and ``ingest_invoices``/``ingest_invoices_via_extraction`` clear
``reconciliation_results``, ``discrepancy_notes``, ``corrections``,
``extracted_fields``, and then ``invoices`` (in that order, respecting the
foreign keys each of those tables holds to invoices) before inserting.
Running either repeatedly against the same database file reloads the same
rows rather than raising a ``UNIQUE``/``PRIMARY KEY`` violation or
duplicating data.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.db.schema import init_db
from invoice_reconciliation.extraction.cache import (
    DEFAULT_CACHE_DIR,
    CacheMissError,
    ResponseCache,
)
from invoice_reconciliation.extraction.client import build_client, extract_invoice_fields
from invoice_reconciliation.extraction.parser import ExtractionError, parse
from invoice_reconciliation.money import MoneyFormatError, dollars_to_cents

logger = logging.getLogger(__name__)

DEFAULT_SEED_PATH = Path("tasks/invoices/seed.json")
DEFAULT_DB_PATH = Path("invoice_reconciliation.sqlite")
DEFAULT_IMAGES_DIR = Path("tasks/invoices/images")

# extraction_source values. "live" marks a real model call; "cache" marks
# a replayed one; the seeded path keeps its own distinct marker (see
# SEEDED_EXTRACTION_SOURCE below) so a reviewer can always tell, per
# invoice, whether a value came from the fixture, a live call, or a
# replayed response.
LIVE_EXTRACTION_SOURCE = "live"
CACHE_EXTRACTION_SOURCE = "cache"

# Seeded invoices have no real capture timestamp — extraction has not run
# yet. ``received_at`` must still be deterministic so the duplicate rule
# (earliest received copy of a given invoice number stays payable) has a
# fixed, reproducible answer rather than depending on wall-clock time or
# the order concurrent test runs happen to execute in. Each invoice in the
# fixture's array gets a synthetic timestamp one second after the previous
# one, in fixture order, so "clean" (index 0) is strictly earlier than
# "duplicate" (index 2).
_SEEDED_RECEIVED_AT_BASE = "2025-01-01T00:00:00"

# These values were read straight from the fixture, not extracted by a
# model call — recorded on the invoice so the distinction stays visible.
SEEDED_EXTRACTION_SOURCE = "seed_fixture"

# Field names written to extracted_fields, in the order the task lists them.
_EXTRACTED_FIELD_NAMES = (
    "invoice_number",
    "supplier_id",
    "po_id",
    "sku",
    "quantity",
    "unit_cents",
    "total_cents",
)


def _seeded_received_at(index: int) -> str:
    """Deterministic ``received_at`` for the invoice at ``index`` in the fixture array.

    Derived from fixture array order (not ``datetime.now()``) so that
    earlier entries in ``seed.json`` always receive an earlier timestamp.
    This is what makes ``clean`` (listed before ``duplicate``) the invoice
    the duplicate rule treats as the first, payable copy.
    """
    base = datetime.fromisoformat(_SEEDED_RECEIVED_AT_BASE)
    return (base + timedelta(seconds=index)).isoformat()


def _field_value_to_text(value: object) -> str | None:
    """Render a fixture field value as the TEXT the model would have returned.

    ``None`` stays ``None`` (a real SQL NULL) rather than becoming the
    string ``"None"`` or ``""`` — the ``missing-reference`` fixture's null
    ``po_id`` must survive as NULL for the ``unresolved`` classification to
    work. Numbers (``quantity``, ``unit_cents``, ``total_cents``) are
    rendered as plain text; the fixture already supplies them as integer
    cents, never dollar strings, so this never runs them through
    ``dollars_to_cents``.
    """
    if value is None:
        return None
    return str(value)


def _load_seed(seed_path: Path) -> dict:
    """Read and parse the seed fixture JSON."""
    with seed_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _clear_reference_data(conn: sqlite3.Connection) -> None:
    """Delete existing reference rows so re-ingest does not duplicate.

    ``reconciliation_results`` holds foreign keys to both
    ``purchase_orders`` (``matched_po_id``) and ``receipts``
    (``matched_receipt_id``), so it must be cleared before either of them.
    Receipts are then cleared before purchase_orders, since receipts holds
    the foreign key to purchase_orders.
    """
    conn.execute("DELETE FROM reconciliation_results")
    conn.execute("DELETE FROM receipts")
    conn.execute("DELETE FROM purchase_orders")


def ingest_reference_data(
    conn: sqlite3.Connection, *, seed_path: Path = DEFAULT_SEED_PATH
) -> None:
    """Load purchase orders and receipts from ``seed_path`` into ``conn``.

    Clears any existing reference rows first, so calling this twice against
    the same database leaves identical rows rather than erroring or
    duplicating.

    ``unit_cents`` in the fixture is already integer USD cents — it is
    inserted as-is, never passed through a dollars-to-cents conversion.
    """
    seed = _load_seed(seed_path)

    _clear_reference_data(conn)

    for po in seed["purchase_orders"]:
        repository.insert_purchase_order(
            conn,
            po_id=po["po_id"],
            supplier_id=po["supplier_id"],
            sku=po["sku"],
            quantity=po["quantity"],
            unit_cents=po["unit_cents"],
        )

    for receipt in seed["receipts"]:
        repository.insert_receipt(
            conn,
            receipt_id=receipt["receipt_id"],
            po_id=receipt["po_id"],
            sku=receipt["sku"],
            quantity=receipt["quantity"],
        )


def _clear_invoice_data(conn: sqlite3.Connection) -> None:
    """Delete existing invoice rows so re-ingest does not duplicate.

    Every table with a foreign key to ``invoices`` —
    ``reconciliation_results``, ``discrepancy_notes``, ``corrections``, and
    ``extracted_fields`` — must be cleared before ``invoices`` itself.
    ``reconciliation_results`` is also cleared here (in addition to
    ``_clear_reference_data``) since re-ingesting invoices alone (without
    reference data) must not leave stale reconciliation rows pointing at
    deleted invoice ids either.
    """
    conn.execute("DELETE FROM reconciliation_results")
    conn.execute("DELETE FROM discrepancy_notes")
    conn.execute("DELETE FROM corrections")
    conn.execute("DELETE FROM extracted_fields")
    conn.execute("DELETE FROM invoices")


def ingest_invoices(
    conn: sqlite3.Connection,
    *,
    seed_path: Path = DEFAULT_SEED_PATH,
    images_dir: Path = DEFAULT_IMAGES_DIR,
) -> None:
    """Load the seeded invoice records from ``seed_path`` into ``conn``.

    Clears any existing invoice rows first, so calling this twice against
    the same database leaves identical rows rather than erroring or
    duplicating.

    For each invoice in the fixture, inserts one ``invoices`` row and seven
    ``extracted_fields`` rows (``invoice_number``, ``supplier_id``, ``po_id``,
    ``sku``, ``quantity``, ``unit_cents``, ``total_cents``). ``original_value``
    and ``current_value`` are written identically: nothing has been corrected
    yet. ``extraction_source`` is recorded as ``SEEDED_EXTRACTION_SOURCE``
    rather than a model identifier, since these values come straight from the
    fixture, not a model call — extraction is wired in by a later slice.

    ``received_at`` is derived deterministically from the invoice's position
    in the fixture array (see ``_seeded_received_at``), not from
    ``datetime.now()``, so the duplicate rule's ordering of ``clean`` before
    ``duplicate`` (both ``INV-1``/``S1``) is reproducible rather than
    arbitrary or tied.

    A null ``po_id`` in the fixture (the ``missing-reference`` invoice) is
    written as a real SQL NULL to ``extracted_fields``, never the string
    ``"None"`` or ``""``.
    """
    seed = _load_seed(seed_path)

    _clear_invoice_data(conn)

    for index, invoice in enumerate(seed["invoices"]):
        file_id = invoice["file_id"]
        invoice_id = repository.insert_invoice(
            conn,
            file_id=file_id,
            image_path=str(images_dir / f"{file_id}.png"),
            layout=invoice["layout"],
            received_at=_seeded_received_at(index),
            extraction_source=SEEDED_EXTRACTION_SOURCE,
        )

        for field_name in _EXTRACTED_FIELD_NAMES:
            value = _field_value_to_text(invoice[field_name])
            repository.insert_extracted_field(
                conn,
                invoice_id=invoice_id,
                field_name=field_name,
                original_value=value,
                current_value=value,
            )


@dataclass(frozen=True, slots=True)
class ExtractionIngestOutcome:
    """What happened extracting one invoice's fields from its image.

    ``succeeded`` is ``False`` for a missing image file, a cache miss
    (``CacheMissError``), or an ``ExtractionError`` — the failure modes
    this path isolates. ``model_id``, ``input_tokens`` and ``output_tokens``
    come from the real response body (``raw_response["model"]``,
    ``raw_response["usage"]``), never from ``ModelConfig`` — a replayed
    entry must report the model that truly served it, not the model the
    caller asked for. They are ``None`` when extraction did not succeed, or
    when the response carried no usage block.

    ``source`` records whether this invoice's raw response came from a
    live Bedrock call (``"live"``) or the saved cache (``"cache"``) — the
    exercise manifest requires this documented per invoice, not just for
    the batch as a whole.
    """

    file_id: str
    succeeded: bool
    error: str | None = None
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    source: str | None = None


def _usage_from_raw_response(raw_response: dict) -> tuple[int | None, int | None]:
    """Pull input/output token counts out of the raw response's usage block.

    Returns ``(None, None)`` if the response carries no ``usage`` block —
    this is metadata for reporting only, never required for correctness.
    """
    usage = raw_response.get("usage") if isinstance(raw_response, dict) else None
    if not isinstance(usage, dict):
        return None, None
    return usage.get("input_tokens"), usage.get("output_tokens")


def ingest_invoices_via_extraction(
    conn: sqlite3.Connection,
    *,
    seed_path: Path = DEFAULT_SEED_PATH,
    images_dir: Path = DEFAULT_IMAGES_DIR,
    model_config: ModelConfig | None = None,
    use_cache: bool = False,
    refresh_cache: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[ExtractionIngestOutcome]:
    """Load invoice rows by reading each fixture's image through the model.

    This is the live counterpart to ``ingest_invoices``: ``invoice_number``,
    ``supplier_id``, ``po_id``, ``sku``, ``quantity``, ``unit_price`` and
    ``total`` come from a real Bedrock call
    (``extraction.client.extract_invoice_fields``) against
    ``images_dir / f"{file_id}.png"``, parsed by ``extraction.parser.parse``,
    rather than from the fixture's prepared values. The fixture
    (``seed_path``) still supplies the ordered list of ``file_id``\\ s and
    each invoice's ``layout`` — only the seven field *values* are replaced.

    ``original_value`` and ``current_value`` are written identically, same
    as the prepared-record path: nothing has been corrected yet.
    ``extraction_source`` is recorded as ``LIVE_EXTRACTION_SOURCE`` or
    ``CACHE_EXTRACTION_SOURCE``, matching whichever path actually served
    this invoice's response.

    **Replay (`use_cache=True`).** The cache lookup (`cache.get`) wraps
    **only the network call**: every invoice still goes through
    ``image_path.read_bytes()`` (to verify the saved response's hash
    against the image in hand), then ``extraction.parser.parse`` on
    whatever raw response is in play, then the identical
    ``dollars_to_cents`` conversion and database write. No separate
    "replay" code path exists past the one line that chooses where
    ``raw_response`` comes from — this is what makes replay prove the real
    parsing and reconciliation path, not a parallel shortcut. A cache miss
    (no saved entry, or the saved entry's hash no longer matches the image)
    raises ``CacheMissError``, which is caught alongside the other
    per-invoice failure modes below and marks only that invoice failed.
    Replay never touches the AWS credential chain: ``build_client`` /
    ``boto3`` is never imported on this path, since ``extract_invoice_fields``
    is never called.

    **Refresh (`refresh_cache=True`).** Forces a live call for every
    invoice (bypassing any existing cache entry), and saves the raw
    response via ``cache.put``, overwriting whatever was there. This is
    the only path that writes to the cache. ``use_cache`` and
    ``refresh_cache`` are mutually exclusive from the caller's point of
    view (the CLI only ever sets one), but if both are passed,
    ``refresh_cache`` takes priority — a refresh always calls the model.

    Failure isolation: an invoice whose image file does not exist yet,
    whose call or cache lookup raises (``ExtractionError``,
    ``CacheMissError``), or whose money fields fail
    ``money.dollars_to_cents``, gets its ``invoices`` row inserted but no
    ``extracted_fields`` rows at all — ``repository.get_current_fields``
    then returns ``{}`` for it. Its ``invoices.extraction_failed`` flag is
    also set (``repository.mark_extraction_failed``), which is what
    ``pipeline.recalculate_one`` reads to classify the invoice ``failed``
    rather than ``unresolved`` — an empty field dict alone is indistinguishable
    from the ``missing-reference`` fixture, which extracts fine but
    genuinely has no ``po_id``. The batch continues with the next invoice.
    One bad image or one bad response never aborts the run.

    ``model_config`` defaults to ``ModelConfig()`` (reading
    ``BEDROCK_MODEL_ID`` / ``AWS_REGION`` from the environment, as that
    dataclass already does) when not supplied. The live Bedrock client is
    built lazily — only when a live call is actually about to happen (i.e.
    not on a pure ``use_cache`` replay) — so a credential-free replay run
    never touches the credential chain even indirectly.

    Returns one ``ExtractionIngestOutcome`` per invoice, in fixture order,
    so the caller (the CLI) can report successes and failures without a
    second read of the database.
    """
    seed = _load_seed(seed_path)

    _clear_invoice_data(conn)

    config = model_config if model_config is not None else ModelConfig()
    needs_live_client = refresh_cache or not use_cache
    client = build_client(config) if needs_live_client else None
    cache = ResponseCache(cache_dir)
    outcomes: list[ExtractionIngestOutcome] = []

    for index, invoice in enumerate(seed["invoices"]):
        file_id = invoice["file_id"]
        image_path = images_dir / f"{file_id}.png"

        extraction_source = (
            CACHE_EXTRACTION_SOURCE
            if (use_cache and not refresh_cache)
            else LIVE_EXTRACTION_SOURCE
        )
        invoice_id = repository.insert_invoice(
            conn,
            file_id=file_id,
            image_path=str(image_path),
            layout=invoice["layout"],
            received_at=_seeded_received_at(index),
            extraction_source=extraction_source,
        )

        if not image_path.exists():
            logger.warning(
                "no image file for invoice %r at %s; marking extraction failed",
                file_id,
                image_path,
            )
            repository.mark_extraction_failed(conn, invoice_id=invoice_id)
            outcomes.append(
                ExtractionIngestOutcome(
                    file_id=file_id,
                    succeeded=False,
                    error=f"image file not found: {image_path}",
                )
            )
            continue

        response_source = "cache" if (use_cache and not refresh_cache) else "live"
        try:
            image_bytes = image_path.read_bytes()
            if refresh_cache:
                raw_response = extract_invoice_fields(client, config, image_bytes)
                cache.put(file_id, image_bytes, config.model_id, raw_response)
            elif use_cache:
                raw_response = cache.get(file_id, image_bytes).raw_response
            else:
                raw_response = extract_invoice_fields(client, config, image_bytes)
            fields = parse(raw_response)
            # The rules engine's ``_parse_cents`` expects ``unit_cents`` and
            # ``total_cents`` to already hold plain integer-cents text
            # (``"2400"`` meaning 2400 cents) — it never calls
            # ``money.dollars_to_cents`` itself (see rules.py's module
            # docstring). The model instead returns dollar amounts
            # (``"24.00"``, or occasionally a number). This is the one
            # place that bridges the two: convert here, with the project's
            # single money-conversion function, before the value ever
            # reaches ``extracted_fields``.
            unit_cents = dollars_to_cents(fields.unit_price)
            total_cents = dollars_to_cents(fields.total)
        except (ExtractionError, CacheMissError, MoneyFormatError, OSError) as exc:
            # ExtractionError: the response failed shape/schema/domain
            # validation. CacheMissError: no usable saved response for
            # this file_id (absent, unreadable, or a stale hash against
            # the image in hand) — replay must fail this invoice rather
            # than fabricate or reuse a stale response. MoneyFormatError:
            # a money value passed the parser's own domain check but still
            # fails here (defence in depth). OSError: the image file
            # vanished or could not be read between the existence check
            # above and this read — all four isolate to this one invoice
            # rather than aborting the batch, per the project's
            # established failure-isolation contract
            # (pipeline.recalculate_one does the same for a malformed
            # stored money value).
            logger.warning(
                "extraction failed for invoice %r: %s: %s",
                file_id,
                type(exc).__name__,
                exc,
            )
            repository.mark_extraction_failed(conn, invoice_id=invoice_id)
            outcomes.append(
                ExtractionIngestOutcome(
                    file_id=file_id,
                    succeeded=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        input_tokens, output_tokens = _usage_from_raw_response(raw_response)
        response_model_id = (
            raw_response.get("model") if isinstance(raw_response, dict) else None
        )

        values = {
            "invoice_number": fields.invoice_number,
            "supplier_id": fields.supplier_id,
            "po_id": fields.po_id,
            "sku": fields.sku,
            "quantity": str(fields.quantity),
            "unit_cents": str(unit_cents),
            "total_cents": str(total_cents),
        }
        for field_name in _EXTRACTED_FIELD_NAMES:
            raw_value = values[field_name]
            value = None if raw_value is None else str(raw_value)
            repository.insert_extracted_field(
                conn,
                invoice_id=invoice_id,
                field_name=field_name,
                original_value=value,
                current_value=value,
            )

        outcomes.append(
            ExtractionIngestOutcome(
                file_id=file_id,
                succeeded=True,
                model_id=response_model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                source=response_source,
            )
        )

    return outcomes


def run_ingest(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    seed_path: Path = DEFAULT_SEED_PATH,
    images_dir: Path = DEFAULT_IMAGES_DIR,
    reset_db: bool = False,
    use_extraction: bool = False,
    model_config: ModelConfig | None = None,
    use_cache: bool = False,
    refresh_cache: bool = False,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> list[ExtractionIngestOutcome] | None:
    """Build/refresh the database at ``db_path`` from ``seed_path``.

    If ``reset_db`` is True, the database file is deleted first so the
    schema and data are rebuilt from scratch. Otherwise the existing file
    (if any) is reused: ``init_db`` is idempotent DDL, and
    ``ingest_reference_data`` clears reference rows before reinserting.

    The invoice-ingest path is chosen as follows:

    - ``use_extraction`` is ``False`` and neither cache flag is set (the
      default) — ``ingest_invoices``, the prepared-record path every
      existing test and the seed check depend on. Returns ``None``.
    - ``use_extraction`` is ``True``, or ``use_cache``/``refresh_cache`` is
      set — ``ingest_invoices_via_extraction``. ``use_cache`` replays saved
      responses with no live call and no credential-chain access;
      ``refresh_cache`` forces a live call for every invoice and saves the
      response. Returns the list of per-invoice
      ``ExtractionIngestOutcome`` so the caller can report successes and
      failures.
    """
    if reset_db and db_path != Path(":memory:") and Path(db_path).exists():
        Path(db_path).unlink()

    with connect(db_path) as conn:
        init_db(conn)
        ingest_reference_data(conn, seed_path=seed_path)
        if use_extraction or use_cache or refresh_cache:
            return ingest_invoices_via_extraction(
                conn,
                seed_path=seed_path,
                images_dir=images_dir,
                model_config=model_config,
                use_cache=use_cache,
                refresh_cache=refresh_cache,
                cache_dir=cache_dir,
            )
        ingest_invoices(conn, seed_path=seed_path, images_dir=images_dir)
        return None


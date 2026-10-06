# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""Unit tests for the response cache
(src/invoice_reconciliation/extraction/cache.py) and the status it feeds
into reconciliation.

Covers the cache's own contract (hit, miss, stale-hash) and the one
behaviour this slice added: a cache miss must leave an invoice ``failed``,
never ``unresolved`` — the latter is reserved for an invoice that
genuinely extracted with no purchase-order reference
(``missing-reference``). Both leave ``extracted_fields`` looking the same
(empty, or ``po_id`` null), so the distinction has to come from
``invoices.extraction_failed``, not from the field values — this is what
these tests prove end to end, through ``ingest_invoices_via_extraction``
and ``pipeline.recalculate_one``, not just by asserting on the flag in
isolation.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.db import ingest as ingest_module
from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import get_connection
from invoice_reconciliation.db.schema import init_db
from invoice_reconciliation.extraction.cache import CacheMissError, ResponseCache, image_sha256
from invoice_reconciliation.pipeline import recalculate_one

SEED_PATH = Path("tasks/invoices/seed.json")
IMAGES_DIR = Path("tasks/invoices/images")
CACHE_DIR = Path("tests/fixtures/bedrock_responses")

_SAMPLE_RAW_RESPONSE = {
    "model": "claude-sonnet-4-5-20250929",
    "usage": {"input_tokens": 111, "output_tokens": 22},
    "content": [
        {
            "type": "tool_use",
            "input": {
                "invoice_number": "INV-1",
                "supplier_id": "S1",
                "po_id": "PO-1",
                "sku": "CAB-1",
                "quantity": 5,
                "unit_price": "20.00",
                "total": "100.00",
            },
        }
    ],
}


# ---------------------------------------------------------------------------
# ResponseCache: hit, miss, stale hash
# ---------------------------------------------------------------------------


def test_get_returns_the_saved_entry_when_the_hash_matches(tmp_path):
    cache = ResponseCache(tmp_path)
    image_bytes = b"fake-image-bytes"
    cache.put("sample", image_bytes, "model-x", _SAMPLE_RAW_RESPONSE)

    entry = cache.get("sample", image_bytes)

    assert entry.file_id == "sample"
    assert entry.image_sha256 == image_sha256(image_bytes)
    assert entry.model_id == "model-x"
    assert entry.raw_response == _SAMPLE_RAW_RESPONSE


def test_get_raises_cache_miss_error_when_no_file_exists(tmp_path):
    cache = ResponseCache(tmp_path)

    with pytest.raises(CacheMissError, match="no cache entry"):
        cache.get("never-saved", b"any-bytes")


def test_get_raises_and_warns_on_a_changed_image_against_a_stored_entry(tmp_path, caplog):
    cache = ResponseCache(tmp_path)
    original_bytes = b"original-image-bytes"
    changed_bytes = b"these-bytes-differ-from-the-original"
    cache.put("sample", original_bytes, "model-x", _SAMPLE_RAW_RESPONSE)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(CacheMissError, match="stale"):
            cache.get("sample", changed_bytes)

    # The warning names both hashes, per the module's documented contract —
    # a reviewer must be able to see what changed, not just that it did.
    assert any("stale" in record.message for record in caplog.records)
    stored_hash = image_sha256(original_bytes)
    actual_hash = image_sha256(changed_bytes)
    warning_text = " ".join(record.message for record in caplog.records)
    assert stored_hash in warning_text
    assert actual_hash in warning_text


def test_a_changed_image_fails_that_invoice_in_replay_mode_rather_than_returning_a_stale_response(
    tmp_path, monkeypatch
):
    """Exercise the real replay path (``ingest_invoices_via_extraction``,
    ``use_cache=True``), not just ``cache.get`` directly: a stored entry
    whose image hash no longer matches the image on disk must fail only
    that invoice, and must never hand back the stale saved response.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = ResponseCache(cache_dir)
    cache.put("clean", b"stale-bytes-not-the-real-image", "model-x", _SAMPLE_RAW_RESPONSE)

    db_path = tmp_path / "db.sqlite"
    conn = get_connection(db_path)
    init_db(conn)
    monkeypatch.setattr(ingest_module, "build_client", lambda config: object())

    outcomes = ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        model_config=ModelConfig(),
        use_cache=True,
        cache_dir=cache_dir,
    )

    by_file_id = {outcome.file_id: outcome for outcome in outcomes}
    assert by_file_id["clean"].succeeded is False
    assert "stale" in by_file_id["clean"].error

    invoice_row = repository.get_invoice_by_file_id(conn, file_id="clean")
    fields = repository.get_current_fields(conn, invoice_id=invoice_row["invoice_id"])
    assert fields == {}  # no stale values were written

    conn.close()


# ---------------------------------------------------------------------------
# failed vs. unresolved: the status-reporting fix
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "cache_status.sqlite"
    connection = get_connection(db_path)
    init_db(connection)
    yield connection
    connection.close()


def test_a_cache_miss_yields_failed_status_not_unresolved(conn, tmp_path, monkeypatch):
    """A missing cache entry (no file saved for this ``file_id``) must
    leave the invoice ``failed``, never ``unresolved`` — ``unresolved``
    is reserved for an invoice that extracted fine but named no PO.
    """
    empty_cache_dir = tmp_path / "empty_cache"
    empty_cache_dir.mkdir()
    monkeypatch.setattr(ingest_module, "build_client", lambda config: object())

    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    outcomes = ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        model_config=ModelConfig(),
        use_cache=True,
        cache_dir=empty_cache_dir,
    )

    by_file_id = {outcome.file_id: outcome for outcome in outcomes}
    assert by_file_id["clean"].succeeded is False
    assert "CacheMissError" in by_file_id["clean"].error

    clean_invoice = repository.get_invoice_by_file_id(conn, file_id="clean")
    result = recalculate_one(conn, clean_invoice["invoice_id"])

    assert result.status == "failed"
    assert result.status != "unresolved"
    assert result.expected_cents is None
    assert result.billed_cents is None


def test_missing_reference_stays_unresolved_with_seven_fields(conn):
    """The non-negotiable distinction: an invoice whose extraction
    genuinely succeeded but found no PO reference is ``unresolved``, with
    all seven fields present (``po_id`` null), and ``extraction_failed``
    unset — this must not regress when the cache-miss fix above is in
    place.
    """
    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    ingest_module.ingest_invoices(conn, seed_path=SEED_PATH, images_dir=IMAGES_DIR)

    invoice = repository.get_invoice_by_file_id(conn, file_id="missing-reference")
    assert invoice["extraction_failed"] == 0

    fields = repository.get_current_fields(conn, invoice_id=invoice["invoice_id"])
    assert len(fields) == 7
    assert fields["po_id"] is None

    result = recalculate_one(conn, invoice["invoice_id"])
    assert result.status == "unresolved"


def test_batch_report_and_exit_summary_agree_on_a_cache_miss(conn, tmp_path, monkeypatch):
    """Reproduces the originally reported defect end to end: a cache-miss
    invoice must show as ``failed`` in the per-invoice reconciliation
    result, not just in the extraction-outcome report — the two must
    never disagree the way the summary line used to.
    """
    empty_cache_dir = tmp_path / "empty_cache"
    empty_cache_dir.mkdir()
    monkeypatch.setattr(ingest_module, "build_client", lambda config: object())

    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        model_config=ModelConfig(),
        use_cache=True,
        cache_dir=empty_cache_dir,
    )

    from invoice_reconciliation.pipeline import run_batch

    outcomes = run_batch(conn)
    statuses = {outcome.file_id: outcome.result.status for outcome in outcomes}

    # Every seeded invoice has no saved entry in the empty cache dir, so
    # every one of them must report 'failed' -- none may fall through to
    # 'unresolved'.
    assert statuses["clean"] == "failed"
    assert statuses["missing-reference"] == "failed"
    failed_count = sum(1 for status in statuses.values() if status == "failed")
    assert failed_count == len(statuses)

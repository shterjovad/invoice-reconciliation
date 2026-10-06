# @layer: integration
# @spec: 001-invoice-reconciliation-review
# @regression
"""Integration tests for Slice 8: one bad invoice must not cost a reviewer
the rest of the run.

These tests drive the real batch path end to end — ``ingest_invoices_via_extraction``
then ``pipeline.run_batch`` — against a scratch copy of the response cache
under ``tmp_path``. The six committed fixture files under
``tests/fixtures/bedrock_responses/`` are never written to; each test copies
them first and corrupts or removes an entry only in its own copy.

Two failure shapes are covered, both isolating to the one invoice that
triggers them:

- a corrupt cache entry (malformed JSON) for one ``file_id``;
- an absent cache entry (file removed) for one ``file_id``.

Both must leave every other invoice processed and reporting its correct
status, and must leave the failed invoice at ``status = failed`` with no
expected amount and no difference, carrying a readable reason naming the
cause. The regression guard at the end of this file reproduces the Slice 7
defect directly: ``missing-reference`` (a genuine extraction success with no
PO reference) must stay ``unresolved`` with all seven fields, never
``failed``, in the same run that fails a different invoice outright.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.db import ingest as ingest_module
from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import get_connection
from invoice_reconciliation.db.schema import init_db
from invoice_reconciliation.pipeline import run_batch

SEED_PATH = Path("tasks/invoices/seed.json")
IMAGES_DIR = Path("tasks/invoices/images")
COMMITTED_CACHE_DIR = Path("tests/fixtures/bedrock_responses")

ALL_FILE_IDS = {
    "clean",
    "wrong-price",
    "duplicate",
    "missing-reference",
    "quantity-overbill",
    "undercharge",
}


@pytest.fixture()
def scratch_cache_dir(tmp_path):
    """A writable copy of the committed cache, so a test can corrupt or
    remove one entry without ever touching the real fixture files."""
    scratch = tmp_path / "scratch_cache"
    shutil.copytree(COMMITTED_CACHE_DIR, scratch)
    return scratch


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "failure_isolation.sqlite"
    connection = get_connection(db_path)
    init_db(connection)
    yield connection
    connection.close()


def _run_full_batch(conn, cache_dir, monkeypatch):
    """Ingest via the replay (``use_cache``) path from ``cache_dir``, then
    run the batch, returning (ingest_outcomes, batch_outcomes)."""
    monkeypatch.setattr(ingest_module, "build_client", lambda config: object())

    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    ingest_outcomes = ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        model_config=ModelConfig(),
        use_cache=True,
        cache_dir=cache_dir,
    )
    batch_outcomes = run_batch(conn)
    return ingest_outcomes, batch_outcomes


# ---------------------------------------------------------------------------
# Corrupt cache entry: one bad invoice, the rest unaffected
# ---------------------------------------------------------------------------


def test_a_corrupt_cache_entry_leaves_every_other_invoice_processed(
    conn, scratch_cache_dir, monkeypatch
):
    """Malformed JSON in one invoice's saved response must fail only that
    invoice. Every other invoice in the batch must still be processed and
    listed with its correct status."""
    corrupt_path = scratch_cache_dir / "wrong-price.json"
    corrupt_path.write_text("{ this is not valid json ]", encoding="utf-8")

    ingest_outcomes, batch_outcomes = _run_full_batch(conn, scratch_cache_dir, monkeypatch)

    by_file_id = {outcome.file_id: outcome for outcome in ingest_outcomes}
    assert by_file_id["wrong-price"].succeeded is False
    assert "not valid JSON" in by_file_id["wrong-price"].error

    # Every other invoice still extracted and was processed.
    other_ids = ALL_FILE_IDS - {"wrong-price"}
    for file_id in other_ids:
        assert by_file_id[file_id].succeeded is True, f"{file_id} should still succeed"

    statuses = {outcome.file_id: outcome.result.status for outcome in batch_outcomes}
    assert set(statuses) == ALL_FILE_IDS, "every invoice must still appear in the batch"
    assert statuses["clean"] == "reconciled"
    assert statuses["duplicate"] == "duplicate"
    assert statuses["missing-reference"] == "unresolved"
    assert statuses["quantity-overbill"] == "discrepant"
    assert statuses["undercharge"] == "discrepant"


def test_a_corrupt_cache_entry_carries_no_amounts_and_a_readable_reason(
    conn, scratch_cache_dir, monkeypatch
):
    """The failed invoice itself must report ``status = failed``, no
    expected amount, no difference, and a reason naming the real cause."""
    corrupt_path = scratch_cache_dir / "quantity-overbill.json"
    corrupt_path.write_text("{ not json at all", encoding="utf-8")

    ingest_outcomes, batch_outcomes = _run_full_batch(conn, scratch_cache_dir, monkeypatch)

    by_file_id = {outcome.file_id: outcome for outcome in ingest_outcomes}
    assert by_file_id["quantity-overbill"].succeeded is False
    assert "CacheMissError" in by_file_id["quantity-overbill"].error
    assert "not valid JSON" in by_file_id["quantity-overbill"].error

    results_by_file_id = {outcome.file_id: outcome.result for outcome in batch_outcomes}
    failed_result = results_by_file_id["quantity-overbill"]
    assert failed_result.status == "failed"
    assert failed_result.expected_cents is None
    assert failed_result.billed_cents is None
    assert failed_result.difference_cents is None


# ---------------------------------------------------------------------------
# Absent cache entry: one bad invoice, the rest unaffected
# ---------------------------------------------------------------------------


def test_an_absent_cache_entry_leaves_every_other_invoice_processed(
    conn, scratch_cache_dir, monkeypatch
):
    """A removed cache file for one invoice (never corrupted, just gone)
    must fail only that invoice, leaving the rest of the run intact."""
    missing_path = scratch_cache_dir / "undercharge.json"
    missing_path.unlink()

    ingest_outcomes, batch_outcomes = _run_full_batch(conn, scratch_cache_dir, monkeypatch)

    by_file_id = {outcome.file_id: outcome for outcome in ingest_outcomes}
    assert by_file_id["undercharge"].succeeded is False
    assert "no cache entry" in by_file_id["undercharge"].error

    other_ids = ALL_FILE_IDS - {"undercharge"}
    for file_id in other_ids:
        assert by_file_id[file_id].succeeded is True, f"{file_id} should still succeed"

    statuses = {outcome.file_id: outcome.result.status for outcome in batch_outcomes}
    assert set(statuses) == ALL_FILE_IDS
    assert statuses["clean"] == "reconciled"
    assert statuses["wrong-price"] == "discrepant"
    assert statuses["duplicate"] == "duplicate"
    assert statuses["missing-reference"] == "unresolved"
    assert statuses["quantity-overbill"] == "discrepant"


def test_an_absent_cache_entry_carries_no_amounts_and_a_readable_reason(
    conn, scratch_cache_dir, monkeypatch
):
    missing_path = scratch_cache_dir / "clean.json"
    missing_path.unlink()

    ingest_outcomes, batch_outcomes = _run_full_batch(conn, scratch_cache_dir, monkeypatch)

    by_file_id = {outcome.file_id: outcome for outcome in ingest_outcomes}
    assert by_file_id["clean"].succeeded is False
    assert "CacheMissError" in by_file_id["clean"].error
    assert "no cache entry" in by_file_id["clean"].error

    results_by_file_id = {outcome.file_id: outcome.result for outcome in batch_outcomes}
    failed_result = results_by_file_id["clean"]
    assert failed_result.status == "failed"
    assert failed_result.expected_cents is None
    assert failed_result.billed_cents is None
    assert failed_result.difference_cents is None


# ---------------------------------------------------------------------------
# Regression guard: failed must never collapse into unresolved
# ---------------------------------------------------------------------------


def test_failed_invoice_never_reports_as_unresolved_in_the_same_run(
    conn, scratch_cache_dir, monkeypatch
):
    """The Slice 7 defect, reproduced directly: in one run that fails a
    different invoice outright (absent cache entry), ``missing-reference``
    — which genuinely extracts with no ``po_id`` — must still come out
    ``unresolved`` with all seven fields (``po_id`` null), never collapsing
    into ``failed`` just because some other invoice in the same batch did."""
    missing_path = scratch_cache_dir / "duplicate.json"
    missing_path.unlink()

    _, batch_outcomes = _run_full_batch(conn, scratch_cache_dir, monkeypatch)

    results_by_file_id = {outcome.file_id: outcome for outcome in batch_outcomes}

    failed_outcome = results_by_file_id["duplicate"]
    assert failed_outcome.result.status == "failed"

    unresolved_outcome = results_by_file_id["missing-reference"]
    assert unresolved_outcome.result.status == "unresolved"
    assert unresolved_outcome.result.status != "failed"

    invoice_row = repository.get_invoice_by_file_id(conn, file_id="missing-reference")
    assert invoice_row["extraction_failed"] == 0
    fields = repository.get_current_fields(conn, invoice_id=invoice_row["invoice_id"])
    assert len(fields) == 7
    assert fields["po_id"] is None

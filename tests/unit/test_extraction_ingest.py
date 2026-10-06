# @layer: unit
# @spec: 001-invoice-reconciliation-review
# @regression
"""Failure isolation for ``ingest_invoices_via_extraction``.

One invoice's ``ExtractionError`` (or a missing image file) must mark only
that invoice as failed and let every other invoice in the batch extract
and ingest normally -- the same contract
``pipeline.recalculate_one`` already applies to a malformed stored money
value (``MoneyFormatError``). This module proves that contract for the
live-extraction ingest path, using a fake client/``extract_invoice_fields``
so the test needs no AWS credentials and makes no network call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.db import ingest as ingest_module
from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import get_connection
from invoice_reconciliation.db.schema import init_db
from invoice_reconciliation.extraction.parser import ExtractionError

SEED_PATH = Path("tasks/invoices/seed.json")
IMAGES_DIR = Path("tasks/invoices/images")

# file_id -> raw Bedrock-shaped response, one real success shape reused for
# every invoice that is supposed to succeed in these tests. The content
# does not need to match each invoice's real printed values -- this test
# proves isolation, not extraction accuracy (that is proven separately,
# against the live model, in the Slice 6 verification run).
_FAKE_SUCCESS_RESPONSE = {
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


def _fake_extract_raising_for(file_id_to_fail: str):
    """Build a fake ``extract_invoice_fields`` that raises ``ExtractionError``
    for exactly one invoice (identified by the image bytes it was handed —
    the real image file's bytes for that ``file_id``) and returns a normal
    successful response for every other invoice.
    """
    failing_bytes = (IMAGES_DIR / f"{file_id_to_fail}.png").read_bytes()

    def _fake_extract_invoice_fields(client, config, image_bytes):
        if image_bytes == failing_bytes:
            raise ExtractionError(f"simulated extraction failure for {file_id_to_fail}")
        return _FAKE_SUCCESS_RESPONSE

    return _fake_extract_invoice_fields


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "extraction_isolation.sqlite"
    connection = get_connection(db_path)
    init_db(connection)
    yield connection
    connection.close()


def test_one_invoice_extraction_error_does_not_stop_the_batch(conn, monkeypatch):
    """``wrong-price`` raises ``ExtractionError``; every other invoice (with
    an image file) must still ingest successfully, and the batch must not
    raise.
    """
    monkeypatch.setattr(
        ingest_module,
        "build_client",
        lambda config: object(),
    )
    monkeypatch.setattr(
        ingest_module,
        "extract_invoice_fields",
        _fake_extract_raising_for("wrong-price"),
    )

    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    outcomes = ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        model_config=ModelConfig(),
    )

    by_file_id = {outcome.file_id: outcome for outcome in outcomes}

    # The invoice whose call raised is marked failed, with no amounts
    # persisted for it.
    assert by_file_id["wrong-price"].succeeded is False
    assert "simulated extraction failure" in by_file_id["wrong-price"].error

    wrong_price_invoice = repository.get_invoice_by_file_id(conn, file_id="wrong-price")
    assert wrong_price_invoice is not None  # the invoices row is still written
    wrong_price_fields = repository.get_current_fields(
        conn, invoice_id=wrong_price_invoice["invoice_id"]
    )
    assert wrong_price_fields == {}  # no extracted_fields rows for it

    # Every other invoice that has an image file succeeded and was ingested
    # with real field values -- the one failure did not abort the loop.
    for file_id in ("clean", "duplicate", "missing-reference"):
        assert by_file_id[file_id].succeeded is True, (
            f"{file_id} should have succeeded; got {by_file_id[file_id].error}"
        )
        invoice_row = repository.get_invoice_by_file_id(conn, file_id=file_id)
        assert invoice_row is not None
        fields = repository.get_current_fields(conn, invoice_id=invoice_row["invoice_id"])
        assert fields["invoice_number"] == "INV-1"
        assert fields["supplier_id"] == "S1"
        assert fields["unit_cents"] == "2000"  # "20.00" dollars converted to cents
        assert fields["total_cents"] == "10000"

    # Every invoice now has an image (Slice 7 rendered the last two), so the
    # whole batch is accounted for: one simulated failure, the rest ingested.
    for file_id in ("quantity-overbill", "undercharge"):
        assert by_file_id[file_id].succeeded is True, (
            f"{file_id} should have succeeded; got {by_file_id[file_id].error}"
        )


def test_a_missing_image_fails_only_that_invoice(conn, monkeypatch, tmp_path):
    """An invoice whose image file is absent fails on its own and never
    reaches the extraction call. The rest of the batch still ingests.

    This used to be covered incidentally, because ``quantity-overbill`` and
    ``undercharge`` had no rendered image. Slice 7 rendered them, so the
    condition is now created deliberately: every image is copied to a scratch
    directory except one. Testing it by accident of repository state meant the
    coverage disappeared the moment the fixtures were completed.
    """
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    absent = "undercharge"
    for image in IMAGES_DIR.glob("*.png"):
        if image.stem != absent:
            (images_dir / image.name).write_bytes(image.read_bytes())

    monkeypatch.setattr(ingest_module, "build_client", lambda config: object())
    monkeypatch.setattr(
        ingest_module,
        "extract_invoice_fields",
        lambda client, config, image_bytes: _FAKE_SUCCESS_RESPONSE,
    )

    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    outcomes = ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=images_dir,
        model_config=ModelConfig(),
    )

    by_file_id = {outcome.file_id: outcome for outcome in outcomes}

    assert by_file_id[absent].succeeded is False
    assert "image file not found" in by_file_id[absent].error

    # The one missing image did not stop anything else.
    for file_id, outcome in by_file_id.items():
        if file_id != absent:
            assert outcome.succeeded is True, (
                f"{file_id} should have succeeded; got {outcome.error}"
            )

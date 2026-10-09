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


# ---------------------------------------------------------------------------
# Model and API failures (simulated). The real ``extract_invoice_fields``
# runs against a fake Bedrock client, so the botocore errors pass through
# the same code a live run uses.
# ---------------------------------------------------------------------------

import base64
import json
from io import BytesIO

from botocore.exceptions import ClientError, NoCredentialsError

from invoice_reconciliation import cli
from invoice_reconciliation.extraction.client import ModelCallError

_SEED_FILE_IDS = (
    "clean",
    "wrong-price",
    "duplicate",
    "missing-reference",
    "quantity-overbill",
    "undercharge",
)


class _FakeBedrock:
    """A fake ``bedrock-runtime`` client. ``fail(body)`` returns an exception
    to raise for that request, or ``None`` to answer with a success."""

    def __init__(self, fail):
        self._fail = fail

    def invoke_model(self, *, modelId, body):
        error = self._fail(body)
        if error is not None:
            raise error
        return {"body": BytesIO(json.dumps(_FAKE_SUCCESS_RESPONSE).encode("utf-8"))}


def _throttle() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "InvokeModel"
    )


def _ingest_live(conn, monkeypatch, client):
    monkeypatch.setattr(ingest_module, "build_client", lambda config: client)
    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    outcomes = ingest_module.ingest_invoices_via_extraction(
        conn, seed_path=SEED_PATH, images_dir=IMAGES_DIR, model_config=ModelConfig()
    )
    return {outcome.file_id: outcome for outcome in outcomes}


def test_a_throttled_call_fails_only_that_invoice(conn, monkeypatch):
    """Bedrock throttles the call for wrong-price only. That invoice is
    failed with the Bedrock error code in its reason; the other five are
    extracted, and the batch does not raise."""
    wrong_price_b64 = base64.standard_b64encode(
        (IMAGES_DIR / "wrong-price.png").read_bytes()
    ).decode("ascii")
    client = _FakeBedrock(lambda body: _throttle() if wrong_price_b64 in body else None)

    by_file_id = _ingest_live(conn, monkeypatch, client)

    assert by_file_id["wrong-price"].succeeded is False
    assert "ThrottlingException" in by_file_id["wrong-price"].error
    for file_id in set(_SEED_FILE_IDS) - {"wrong-price"}:
        assert by_file_id[file_id].succeeded is True, by_file_id[file_id].error

    row = repository.get_invoice_by_file_id(conn, file_id="wrong-price")
    assert bool(row["extraction_failed"]) is True
    assert "ThrottlingException" in row["extraction_failure_reason"]


def test_missing_credentials_fail_every_invoice_without_stopping_the_batch(conn, monkeypatch):
    """With no AWS credentials every call raises NoCredentialsError. Every
    invoice is marked failed with that reason; nothing raises."""
    client = _FakeBedrock(lambda body: NoCredentialsError())

    by_file_id = _ingest_live(conn, monkeypatch, client)

    assert set(by_file_id) == set(_SEED_FILE_IDS)
    for outcome in by_file_id.values():
        assert outcome.succeeded is False
        assert "NoCredentialsError" in outcome.error


def test_a_client_that_cannot_be_built_fails_every_invoice_with_its_reason(conn, monkeypatch):
    def _no_client(config):
        raise ModelCallError("could not build the Bedrock client: NoRegionError")

    monkeypatch.setattr(ingest_module, "build_client", _no_client)
    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    outcomes = ingest_module.ingest_invoices_via_extraction(
        conn, seed_path=SEED_PATH, images_dir=IMAGES_DIR, model_config=ModelConfig()
    )

    assert len(outcomes) == len(_SEED_FILE_IDS)
    for outcome in outcomes:
        assert outcome.succeeded is False
        assert "could not build the Bedrock client" in outcome.error


def test_cli_extract_without_credentials_finishes_and_reports_failed(
    tmp_path, monkeypatch, capsys
):
    """``cli --extract`` with no credentials: no traceback. The batch
    completes, every invoice is saved as ``failed`` with a reason, and the
    exit code is EXIT_PROCESSING_FAILED, not the seed-check code."""
    monkeypatch.setattr(
        ingest_module, "build_client", lambda config: _FakeBedrock(lambda body: NoCredentialsError())
    )
    db_path = tmp_path / "no_credentials.sqlite"

    exit_code = cli.main(["--extract", "--reset-db", "--no-model-notes", "--db-path", str(db_path)])

    assert exit_code == cli.EXIT_PROCESSING_FAILED
    output = capsys.readouterr().out
    assert "Traceback" not in output
    assert "NoCredentialsError" in output

    connection = get_connection(db_path)
    try:
        rows = repository.list_invoices_with_results(connection)
    finally:
        connection.close()
    assert {row["file_id"] for row in rows} == set(_SEED_FILE_IDS)
    assert {row["status"] for row in rows} == {"failed"}

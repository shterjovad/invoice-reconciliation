# @layer: integration
# @spec: 001-invoice-reconciliation-review
"""Integration tests for Slice 9: the invoice queue, ``GET /invoices``.

Drives the real FastAPI app through ``fastapi.testclient.TestClient``
against a scratch database built by the same ``run_ingest`` +
``pipeline.run_batch`` path the headless batch command uses (replayed from
the committed Bedrock-response cache, so no AWS credentials are needed).
The web view is never a second implementation of the pipeline: this test
exercises the one pipeline through the queue route, not a stand-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.db.ingest import run_ingest
from invoice_reconciliation.pipeline import run_batch
from invoice_reconciliation.web.app import create_app

SEED_PATH = Path("tasks/invoices/seed.json")


def _table_body(html: str) -> str:
    """Extract just the ``<tbody>...</tbody>`` slice of the queue page.

    The page header also renders the status filter links (one per status
    name, e.g. "duplicate"), so a plain substring check against the whole
    page would find a status word there even when no row for that status
    appears in the table. Scoping to the table body is what the filter
    tests actually mean to assert.
    """
    start = html.index("<tbody>")
    end = html.index("</tbody>")
    return html[start:end]
IMAGES_DIR = Path("tasks/invoices/images")
CACHE_DIR = Path("tests/fixtures/bedrock_responses")

ALL_FILE_IDS = {
    "clean",
    "wrong-price",
    "duplicate",
    "missing-reference",
    "quantity-overbill",
    "undercharge",
}


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A scratch database built from the committed cache, exactly as the
    headless batch command builds it — reused here, never duplicated."""
    path = tmp_path / "web_queue.sqlite"
    run_ingest(
        db_path=path,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        reset_db=True,
        use_cache=True,
        cache_dir=CACHE_DIR,
    )
    with connect(path) as conn:
        run_batch(conn)
    return path


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    app = create_app(db_path=db_path)
    return TestClient(app)


# ---------------------------------------------------------------------------
# The unfiltered queue
# ---------------------------------------------------------------------------


def test_queue_lists_all_six_invoices_with_correct_statuses(client: TestClient) -> None:
    response = client.get("/invoices")
    assert response.status_code == 200
    body = response.text

    for file_id in ALL_FILE_IDS:
        assert file_id in body, f"{file_id} missing from the queue"

    # One status badge per invoice, matching the real run described in the
    # task brief.
    assert body.count('status-reconciled">reconciled') == 1  # clean
    assert body.count('status-discrepant">discrepant') == 3  # wrong-price, quantity-overbill, undercharge
    assert body.count('status-duplicate">duplicate') == 1
    assert body.count('status-unresolved">unresolved') == 1


def test_queue_renders_money_through_cents_to_display(client: TestClient) -> None:
    response = client.get("/invoices")
    body = response.text

    # clean: expected $100.00, billed $100.00, difference $0.00
    assert "100.00" in body
    # wrong-price: billed $120.00, difference $20.00
    assert "120.00" in body
    assert "20.00" in body
    # quantity-overbill: billed $160.00, difference $60.00
    assert "160.00" in body
    assert "60.00" in body


def test_queue_keeps_a_negative_difference_visibly_negative(client: TestClient) -> None:
    """undercharge: expected $100.00, billed $90.00, difference -$10.00.

    The minus sign must survive into the rendered page — a reviewer must
    be able to tell an undercharge from an overcharge at a glance.
    """
    response = client.get("/invoices")
    body = response.text
    assert "-10.00" in body
    assert "amount-negative" in body


def test_queue_renders_null_amounts_as_a_dash_not_the_string_none(client: TestClient) -> None:
    """duplicate and unresolved rows carry null cents in every amount column."""
    response = client.get("/invoices")
    body = response.text
    assert "None" not in body
    assert "–" in body  # en dash placeholder for a null amount


# ---------------------------------------------------------------------------
# The ?status= filter
# ---------------------------------------------------------------------------


def test_status_filter_narrows_to_exactly_the_discrepant_invoices(client: TestClient) -> None:
    response = client.get("/invoices", params={"status": "discrepant"})
    assert response.status_code == 200
    rows = _table_body(response.text)

    for file_id in ("wrong-price", "quantity-overbill", "undercharge"):
        assert file_id in rows
    for file_id in ("clean", "duplicate", "missing-reference"):
        assert file_id not in rows

    assert rows.count('status-discrepant">discrepant') == 3


def test_status_filter_reconciled_narrows_to_exactly_clean(client: TestClient) -> None:
    response = client.get("/invoices", params={"status": "reconciled"})
    rows = _table_body(response.text)
    assert "clean" in rows
    for file_id in ("wrong-price", "duplicate", "missing-reference", "quantity-overbill", "undercharge"):
        assert file_id not in rows


def test_unknown_status_returns_an_empty_list_not_a_500(client: TestClient) -> None:
    response = client.get("/invoices", params={"status": "not-a-real-status"})
    assert response.status_code == 200
    rows = _table_body(response.text)
    for file_id in ALL_FILE_IDS:
        assert file_id not in rows
    assert "No invoices match this filter" in rows

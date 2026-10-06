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

import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.db import ingest as ingest_module
from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect, get_connection
from invoice_reconciliation.db.ingest import run_ingest
from invoice_reconciliation.db.schema import init_db
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


# ---------------------------------------------------------------------------
# Slice 10: the invoice detail page, GET /invoices/{id}
# ---------------------------------------------------------------------------


def _invoice_id_for(client: TestClient, file_id: str) -> int:
    """Look up an invoice_id by file_id through the real queue page.

    Kept test-local (not a repository call) so these tests drive the
    detail route starting only from what a reviewer can see: the file_id
    link on the queue page.
    """
    response = client.get("/invoices")
    body = response.text
    start = body.index(f">{file_id}<")
    # Walk back to the nearest preceding href="/invoices/<id>"
    href_start = body.rindex('href="/invoices/', 0, start)
    href_value = body[href_start:start]
    invoice_id_str = href_value.split("/invoices/")[1].split('"')[0]
    return int(invoice_id_str)


def test_discrepant_detail_page_shows_the_calculation_and_matched_records(
    client: TestClient,
) -> None:
    """wrong-price: billed $120.00, expected $100.00, difference $20.00,
    matched against PO-2 and its receipt RC-2."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    response = client.get(f"/invoices/{invoice_id}")
    assert response.status_code == 200
    body = response.text

    assert "120.00" in body
    assert "100.00" in body
    assert "20.00" in body
    assert "PO-2" in body
    assert "RC-2" in body


def test_discrepant_detail_page_shows_all_seven_fields_with_original_and_current(
    client: TestClient,
) -> None:
    invoice_id = _invoice_id_for(client, "wrong-price")
    response = client.get(f"/invoices/{invoice_id}")
    body = response.text

    field_names = (
        "invoice_number",
        "supplier_id",
        "po_id",
        "sku",
        "quantity",
        "unit_cents",
        "total_cents",
    )
    for field_name in field_names:
        assert field_name in body, f"{field_name} missing from the detail page"
    # Original and current are equal pre-correction (Slice 11 adds
    # corrections) — both the agreed unit price and the billed total
    # appear, each exactly twice (original column + current column).
    assert body.count("2400") == 2  # unit_cents, as stored text
    assert body.count("12000") == 2  # total_cents, as stored text


def test_unresolved_detail_page_shows_no_expected_amount_and_no_difference(
    client: TestClient,
) -> None:
    """missing-reference: seven fields present (po_id null), but no
    matched purchase order, so no expected amount and no difference —
    never a zero, never an estimate."""
    invoice_id = _invoice_id_for(client, "missing-reference")
    response = client.get(f"/invoices/{invoice_id}")
    assert response.status_code == 200
    body = response.text

    # The page was read successfully and simply names no purchase order.
    assert "No matched purchase order" in body
    assert "No expected amount" in body

    # Absence, not just presence: no dollar amount of any kind renders,
    # and no zero stands in for "not computed".
    assert "$0.00" not in body
    assert "0.00" not in body
    for field_name in (
        "invoice_number",
        "supplier_id",
        "po_id",
        "sku",
        "quantity",
        "unit_cents",
        "total_cents",
    ):
        assert field_name in body


def test_nonexistent_invoice_id_returns_404(client: TestClient) -> None:
    response = client.get("/invoices/999999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# The failed invoice: no normal run produces one, so one is engineered
# here exactly as tests/integration/test_failure_isolation.py does — a
# scratch copy of the committed cache with one entry removed.
# ---------------------------------------------------------------------------

COMMITTED_CACHE_DIR = Path("tests/fixtures/bedrock_responses")


@pytest.fixture()
def failed_client(tmp_path: Path, monkeypatch) -> TestClient:
    """A client against a database with one invoice ("clean") forced to
    status = failed, by removing its cache entry before ingest — the same
    technique test_failure_isolation.py uses, never touching the
    committed cache files themselves."""
    scratch_cache = tmp_path / "scratch_cache"
    shutil.copytree(COMMITTED_CACHE_DIR, scratch_cache)
    (scratch_cache / "clean.json").unlink()

    monkeypatch.setattr(ingest_module, "build_client", lambda config: object())

    db_path = tmp_path / "web_failed.sqlite"
    conn = get_connection(db_path)
    init_db(conn)
    ingest_module.ingest_reference_data(conn, seed_path=SEED_PATH)
    ingest_module.ingest_invoices_via_extraction(
        conn,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        model_config=ModelConfig(),
        use_cache=True,
        cache_dir=scratch_cache,
    )
    run_batch(conn)
    conn.commit()
    conn.close()

    app = create_app(db_path=db_path)
    return TestClient(app)


def test_failed_invoice_detail_page_shows_its_reason(failed_client: TestClient) -> None:
    invoice_id = _invoice_id_for(failed_client, "clean")
    response = failed_client.get(f"/invoices/{invoice_id}")
    assert response.status_code == 200
    body = response.text

    assert "status-failed" in body
    assert "CacheMissError" in body
    assert "no cache entry" in body


# ---------------------------------------------------------------------------
# GET /invoices/{id}/image
# ---------------------------------------------------------------------------


def test_image_route_returns_the_real_png_bytes(client: TestClient) -> None:
    invoice_id = _invoice_id_for(client, "clean")
    response = client.get(f"/invoices/{invoice_id}/image")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")

    on_disk = (IMAGES_DIR / "clean.png").read_bytes()
    assert response.content == on_disk


def test_image_route_404s_for_a_path_escaping_the_base_directory(
    tmp_path: Path, db_path: Path
) -> None:
    """A stored image_path that escapes tasks/invoices/images/ must 404,
    never return file bytes from outside the base directory.

    A secret file outside the images base stands in for something that
    must never be served; the test asserts it never reaches the
    response body.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve this", encoding="utf-8")

    with connect(db_path) as conn:
        # A relative path (from the repository root, the same convention
        # every real image_path uses) that walks out of
        # tasks/invoices/images/ to a file well outside the repository —
        # the shape a malicious/corrupt image_path would take.
        relative_to_repo_root = os.path.relpath(secret.resolve(), Path.cwd())
        escaping_path = relative_to_repo_root
        conn.execute(
            "UPDATE invoices SET image_path = ? WHERE file_id = 'clean'",
            (str(escaping_path),),
        )
        conn.commit()
        invoice_row = repository.get_invoice_by_file_id(conn, file_id="clean")
        invoice_id = invoice_row["invoice_id"]

    app = create_app(db_path=db_path)
    escaping_client = TestClient(app)
    response = escaping_client.get(f"/invoices/{invoice_id}/image")
    assert response.status_code == 404
    assert b"do not serve this" not in response.content

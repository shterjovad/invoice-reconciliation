# @layer: integration
# @spec: 001-invoice-reconciliation-review
"""Integration tests for Slice 11: a reviewer's field correction.

Drives the real FastAPI app through ``fastapi.testclient.TestClient``
against a scratch database built by the same ``run_ingest`` +
``pipeline.run_batch`` path the headless batch command uses (replayed from
the committed Bedrock-response cache, so no AWS credentials are needed).

The correction case exercised here (``wrong-price``, field ``total_cents``,
``12000`` -> ``10000``) is hand-calculated in
``tasks/invoices/expected-correction-case.json``. Every assertion below
checks against the values stated in that file, not against whatever the
product happens to compute today.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.db.ingest import run_ingest
from invoice_reconciliation.pipeline import run_batch
from invoice_reconciliation.web.app import create_app

SEED_PATH = Path("tasks/invoices/seed.json")
IMAGES_DIR = Path("tasks/invoices/images")
CACHE_DIR = Path("tests/fixtures/bedrock_responses")
EXPECTED_CASE_PATH = Path("tasks/invoices/expected-correction-case.json")

_CASE = json.loads(EXPECTED_CASE_PATH.read_text(encoding="utf-8"))[0]
INVOICE_FILE_ID = _CASE["invoice_file_id"]
FIELD_NAME = _CASE["field_name"]
VALUE_BEFORE = _CASE["value_before"]
VALUE_AFTER = _CASE["value_after"]
BEFORE = _CASE["before"]
AFTER = _CASE["after"]


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A scratch database built from the committed cache, exactly as the
    headless batch command builds it — reused here, never duplicated."""
    path = tmp_path / "web_correction.sqlite"
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


def _invoice_id_for(db_path: Path, file_id: str) -> int:
    with connect(db_path) as conn:
        row = repository.get_invoice_by_file_id(conn, file_id=file_id)
    return row["invoice_id"]


def _result(db_path: Path, invoice_id: int):
    with connect(db_path) as conn:
        return repository.get_reconciliation_result(conn, invoice_id=invoice_id)


def _current_fields(db_path: Path, invoice_id: int) -> dict[str, str | None]:
    with connect(db_path) as conn:
        return repository.get_current_fields(conn, invoice_id=invoice_id)


def _original_fields(db_path: Path, invoice_id: int) -> dict[str, str | None]:
    with connect(db_path) as conn:
        return repository.get_original_fields(conn, invoice_id=invoice_id)


def _corrections(db_path: Path, invoice_id: int):
    with connect(db_path) as conn:
        return repository.list_corrections(conn, invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# Before the correction: confirm the hand-calculated starting state
# ---------------------------------------------------------------------------


def test_before_correction_the_invoice_is_discrepant_as_expected(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)
    result = _result(db_path, invoice_id)
    assert result["status"] == BEFORE["status"]
    assert result["expected_cents"] == BEFORE["expected_cents"]
    assert result["billed_cents"] == BEFORE["billed_cents"]
    assert result["difference_cents"] == BEFORE["difference_cents"]

    fields = _current_fields(db_path, invoice_id)
    assert fields[FIELD_NAME] == VALUE_BEFORE


# ---------------------------------------------------------------------------
# @regression: the correction case itself
# ---------------------------------------------------------------------------


def test_correcting_total_cents_turns_the_invoice_reconciled_with_zero_difference(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    response = client.post(
        f"/invoices/{invoice_id}/fields/{FIELD_NAME}",
        data={"value": VALUE_AFTER},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = _result(db_path, invoice_id)
    assert result["status"] == AFTER["status"]
    assert result["expected_cents"] == AFTER["expected_cents"]
    assert result["billed_cents"] == AFTER["billed_cents"]
    assert result["difference_cents"] == AFTER["difference_cents"]


def test_original_value_stays_readable_after_the_correction(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    client.post(
        f"/invoices/{invoice_id}/fields/{FIELD_NAME}",
        data={"value": VALUE_AFTER},
        follow_redirects=False,
    )

    original = _original_fields(db_path, invoice_id)
    current = _current_fields(db_path, invoice_id)
    assert original[FIELD_NAME] == VALUE_BEFORE
    assert current[FIELD_NAME] == VALUE_AFTER


def test_exactly_one_correction_row_is_recorded(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    client.post(
        f"/invoices/{invoice_id}/fields/{FIELD_NAME}",
        data={"value": VALUE_AFTER},
        follow_redirects=False,
    )

    rows = _corrections(db_path, invoice_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["field_name"] == FIELD_NAME
    assert row["value_before"] == VALUE_BEFORE
    assert row["value_after"] == VALUE_AFTER


def test_the_correction_survives_a_new_connection(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    client.post(
        f"/invoices/{invoice_id}/fields/{FIELD_NAME}",
        data={"value": VALUE_AFTER},
        follow_redirects=False,
    )

    # Open a brand-new connection, separate from any used above, and
    # re-read every fact the correction touched.
    with connect(db_path) as fresh_conn:
        result = repository.get_reconciliation_result(fresh_conn, invoice_id=invoice_id)
        current = repository.get_current_fields(fresh_conn, invoice_id=invoice_id)
        original = repository.get_original_fields(fresh_conn, invoice_id=invoice_id)
        corrections = repository.list_corrections(fresh_conn, invoice_id=invoice_id)

    assert result["status"] == AFTER["status"]
    assert result["difference_cents"] == AFTER["difference_cents"]
    assert current[FIELD_NAME] == VALUE_AFTER
    assert original[FIELD_NAME] == VALUE_BEFORE
    assert len(corrections) == 1


def test_the_correction_flows_through_to_the_queue_list_view(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    client.post(
        f"/invoices/{invoice_id}/fields/{FIELD_NAME}",
        data={"value": VALUE_AFTER},
        follow_redirects=False,
    )

    discrepant_response = client.get("/invoices", params={"status": "discrepant"})
    assert INVOICE_FILE_ID not in discrepant_response.text

    reconciled_response = client.get("/invoices", params={"status": "reconciled"})
    assert INVOICE_FILE_ID in reconciled_response.text


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_invalid_value_is_rejected_and_nothing_is_written(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    response = client.post(
        f"/invoices/{invoice_id}/fields/{FIELD_NAME}",
        data={"value": "not-a-number"},
        follow_redirects=False,
    )
    assert response.status_code == 422

    current = _current_fields(db_path, invoice_id)
    assert current[FIELD_NAME] == VALUE_BEFORE

    corrections = _corrections(db_path, invoice_id)
    assert len(corrections) == 0

    result = _result(db_path, invoice_id)
    assert result["status"] == BEFORE["status"]
    assert result["difference_cents"] == BEFORE["difference_cents"]


def test_unknown_field_name_is_404(client: TestClient, db_path: Path) -> None:
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    response = client.post(
        f"/invoices/{invoice_id}/fields/not_a_real_field",
        data={"value": "whatever"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_unknown_invoice_id_is_404(client: TestClient) -> None:
    response = client.post(
        "/invoices/999999/fields/total_cents",
        data={"value": "10000"},
        follow_redirects=False,
    )
    assert response.status_code == 404

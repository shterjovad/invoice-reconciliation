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
    # draft_notes_with_model=False: this suite must never reach the network.
    app = create_app(db_path=db_path, draft_notes_with_model=False)
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


# ---------------------------------------------------------------------------
# A correction redrafts a stale, unedited note so it describes the new
# figures — the second, smaller fix: a note is commentary on the
# reconciliation result, and a correction changes that result, so the old
# text must not survive describing amounts that no longer match the page
# around it. This is "force a redraft of this one invoice" — distinct from
# "drafting is once per invoice", which governs the batch's own drafting.
# ---------------------------------------------------------------------------


def test_a_correction_redrafts_a_stale_unedited_note_with_the_new_figures(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    # View once so wrong-price's note is drafted against its original
    # figures (billed $120.00 against agreed $100.00, difference $20.00).
    first_body = client.get(f"/invoices/{invoice_id}").text
    assert "20.00" in first_body

    with connect(db_path) as conn:
        note_before = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
    assert note_before is not None
    assert bool(note_before["is_reviewer_edited"]) is False
    assert "120.00" in note_before["current_text"]

    # A correction that changes the figures but keeps the invoice
    # discrepant, with a different, larger difference (13000 - 10000 =
    # 3000, not the fixture's 2000).
    response = client.post(
        f"/invoices/{invoice_id}/fields/total_cents",
        data={"value": "13000"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = _result(db_path, invoice_id)
    assert result["status"] == "discrepant"
    assert result["difference_cents"] == 3000

    with connect(db_path) as conn:
        note_after = repository.get_discrepancy_note(conn, invoice_id=invoice_id)

    # The note was redrafted to describe the new figures, not left stale.
    assert "130.00" in note_after["current_text"]
    assert note_after["current_text"] != note_before["current_text"]
    assert bool(note_after["is_reviewer_edited"]) is False

    detail_body = client.get(f"/invoices/{invoice_id}").text
    assert "130.00" in detail_body
    assert "30.00" in detail_body


# ---------------------------------------------------------------------------
# A reviewer's note edit survives a correction that recalculates the
# invoice's figures — "a human edit is the most trusted text. Never
# overwrite it" (technical-considerations.md).
#
# This correction changes total_cents to 13000 (not the fixture's 10000),
# a value that keeps wrong-price discrepant with a *different* difference
# (13000 - 10000 = 3000) rather than resolving it to reconciled — the
# edit-survives behavior only has something to prove while the invoice is
# still discrepant and still has a note to show.
# ---------------------------------------------------------------------------


def test_a_reviewer_note_edit_survives_a_recalculation(
    client: TestClient, db_path: Path
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, INVOICE_FILE_ID)

    # View once to draft the note, then edit it in the reviewer's own words.
    client.get(f"/invoices/{invoice_id}")
    edited_text = "Reviewer note: following up with the supplier directly."
    response = client.post(
        f"/invoices/{invoice_id}/note", data={"text": edited_text}, follow_redirects=False
    )
    assert response.status_code == 303

    with connect(db_path) as conn:
        before_note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
    assert before_note["current_text"] == edited_text
    assert bool(before_note["is_reviewer_edited"]) is True
    assert bool(before_note["edit_superseded"]) is False

    # A correction that changes the figures but keeps the invoice
    # discrepant (13000, not the fixture's 10000 which resolves it).
    response = client.post(
        f"/invoices/{invoice_id}/fields/total_cents",
        data={"value": "13000"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = _result(db_path, invoice_id)
    assert result["status"] == "discrepant"
    assert result["difference_cents"] == 3000

    with connect(db_path) as conn:
        after_note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)

    # The reviewer's exact words are untouched by the recalculation...
    assert after_note["current_text"] == edited_text
    assert bool(after_note["is_reviewer_edited"]) is True
    # ...and the previously-drafted text (what the edit was written
    # against) is also untouched, not silently redrafted to match 13000.
    assert after_note["drafted_text"] == before_note["drafted_text"]
    # ...but the note is now flagged as describing superseded figures, so
    # the reviewer can tell and decide whether to redraft.
    assert bool(after_note["edit_superseded"]) is True

    # The detail page surfaces this, not just the database row.
    detail_body = client.get(f"/invoices/{invoice_id}").text
    assert edited_text in detail_body
    assert "superseded" in detail_body.lower() or "out-of-date" in detail_body.lower()


def test_a_correction_changes_the_summary_source_and_groups_on_the_next_request(
    client: TestClient, db_path: Path
) -> None:
    """The summary reads the saved results on every request. quantity-overbill
    bills 8 units against 5 ordered: source Quantity. A reviewer corrects
    the quantity to 5 and leaves the total at 16000 cents. The unit price
    and the quantity now agree with PO-1, but the total does not, so the
    source becomes Total only and the difference stays 6000 cents."""
    invoice_id = _invoice_id_for(db_path, "quantity-overbill")
    before = client.get("/summary").text
    assert "Quantity</td>" in before

    response = client.post(
        f"/invoices/{invoice_id}/fields/quantity",
        data={"value": "5"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = client.get("/summary").text
    assert "Total only" in after
    assert "Quantity</td>" not in after
    assert _result(db_path, invoice_id)["difference_cents"] == 6000


def test_the_improvements_follow_the_issue_counts_after_a_correction(
    client: TestClient, db_path: Path
) -> None:
    """Fresh batch: unit price differs on 2 invoices (wrong-price,
    undercharge), so it is the top issue and recurring. Quantity,
    duplicate and missing reference have 1 invoice each; the tie order puts
    quantity second, as a single case.

    A reviewer corrects undercharge's unit price from 1800 to 2000 cents
    and leaves its total at 9000. Its source becomes Total only. Every
    issue type now has 1 invoice, so nothing recurs: the page says so and
    shows the top two single cases, unit price (wrong-price) and quantity
    (quantity-overbill)."""
    before = client.get("/summary").text
    section = before[before.index("Suggested improvement"):]
    assert "Unit price differs" in section
    assert "Recurring, 2 invoices" in section
    assert "wrong-price" in section and "undercharge" in section
    assert "Quantity differs" in section and "Single case, 1 invoice" in section

    invoice_id = _invoice_id_for(db_path, "undercharge")
    response = client.post(
        f"/invoices/{invoice_id}/fields/unit_cents", data={"value": "2000"}, follow_redirects=False
    )
    assert response.status_code == 303

    after = client.get("/summary").text
    section = after[after.index("Suggested improvement"):]
    assert "No issue recurs yet" in section
    assert "Recurring" not in section
    assert "wrong-price" in section and "quantity-overbill" in section
    assert "undercharge" not in section

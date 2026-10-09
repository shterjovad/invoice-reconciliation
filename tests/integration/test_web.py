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

import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    app = create_app(db_path=db_path, draft_notes_with_model=False)
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

    app = create_app(db_path=db_path, draft_notes_with_model=False)
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

    app = create_app(db_path=db_path, draft_notes_with_model=False)
    escaping_client = TestClient(app)
    response = escaping_client.get(f"/invoices/{invoice_id}/image")
    assert response.status_code == 404
    assert b"do not serve this" not in response.content


# ---------------------------------------------------------------------------
# Slice 12: the drafted note, POST /invoices/{id}/note, and GET /summary
# ---------------------------------------------------------------------------

# No control that would let the product send anything, on either page.
_SEND_WORDS = ("send", "email", "submit to supplier")


def _assert_no_send_control(body: str) -> None:
    lowered = body.lower()
    for word in _SEND_WORDS:
        assert word not in lowered, f"found a send control word {word!r} in the page"


def _result(db_path: Path, invoice_id: int):
    with connect(db_path) as conn:
        return repository.get_reconciliation_result(conn, invoice_id=invoice_id)


def test_discrepant_detail_page_shows_a_drafted_note_naming_invoice_and_po(
    client: TestClient,
) -> None:
    """wrong-price: INV-2 matched to PO-2, billed $120.00 against an
    agreed $100.00, difference $20.00 — the note must carry all of this,
    and must offer no send control."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    response = client.get(f"/invoices/{invoice_id}")
    assert response.status_code == 200
    body = response.text

    assert "Discrepancy note" in body
    assert "INV-2" in body
    assert "PO-2" in body
    assert "120.00" in body
    assert "100.00" in body
    assert "20.00" in body

    _assert_no_send_control(body)


def test_detail_page_note_contains_no_accusatory_wording(client: TestClient) -> None:
    invoice_id = _invoice_id_for(client, "wrong-price")
    response = client.get(f"/invoices/{invoice_id}")
    body = response.text.lower()

    for word in (
        "overcharged",
        "overcharge",
        "error",
        "mistake",
        "fraud",
        "wrongly",
        "deliberately",
    ):
        assert word not in body, f"found accusatory word {word!r} on the detail page"


def test_saving_a_note_edit_does_not_change_the_reconciliation_result(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id_for(client, "wrong-price")

    # View the detail page once first so the note is drafted (the lazy
    # draft-on-view path) before editing it.
    client.get(f"/invoices/{invoice_id}")
    before = _result(db_path, invoice_id)

    response = client.post(
        f"/invoices/{invoice_id}/note",
        data={"text": "A reviewer's own words about this invoice."},
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = _result(db_path, invoice_id)
    assert dict(before) == dict(after)


def test_a_note_edit_persists_and_the_draft_stays_readable_beside_it(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id_for(client, "wrong-price")
    client.get(f"/invoices/{invoice_id}")  # draft the note first

    with connect(db_path) as conn:
        original_draft = repository.get_discrepancy_note(conn, invoice_id=invoice_id)[
            "drafted_text"
        ]

    edited_text = "Reviewer note: following up with the supplier directly."
    response = client.post(
        f"/invoices/{invoice_id}/note", data={"text": edited_text}, follow_redirects=False
    )
    assert response.status_code == 303

    with connect(db_path) as conn:
        note_row = repository.get_discrepancy_note(conn, invoice_id=invoice_id)

    assert note_row["current_text"] == edited_text
    assert note_row["drafted_text"] == original_draft
    assert bool(note_row["is_reviewer_edited"]) is True
    assert note_row["edited_at"] is not None

    # The edited text, not the original draft, now shows on the detail page.
    detail_body = client.get(f"/invoices/{invoice_id}").text
    assert edited_text in detail_body


def test_summary_page_reports_counts_per_status(client: TestClient) -> None:
    response = client.get("/summary")
    assert response.status_code == 200
    body = response.text

    assert "reconciled" in body
    assert "discrepant" in body
    assert "duplicate" in body
    assert "unresolved" in body


def test_summary_counts_per_status_are_exact(db_path: Path) -> None:
    """reconciled 1 (clean), discrepant 3 (wrong-price, quantity-overbill,
    undercharge), duplicate 1, unresolved 1, failed 0 — read straight from
    the repository function the route uses, so this states the exact
    contract independently of the HTML rendering."""
    with connect(db_path) as conn:
        counts = repository.count_results_by_status(conn)

    assert counts.get("reconciled", 0) == 1
    assert counts.get("discrepant", 0) == 3
    assert counts.get("duplicate", 0) == 1
    assert counts.get("unresolved", 0) == 1
    assert counts.get("failed", 0) == 0


def test_summary_page_reports_the_recoverable_total_as_80_dollars_not_70(
    client: TestClient,
) -> None:
    """wrong-price (+2000) and quantity-overbill (+6000) are genuine
    overcharges: 2000 + 6000 = 8000 cents = $80.00. undercharge (-1000) is
    excluded because it is an underbill, not owed — summing every
    discrepant difference would wrongly give $70.00 (8000 - 1000 = 7000)."""
    response = client.get("/summary")
    assert response.status_code == 200
    body = response.text

    assert "80.00" in body
    assert "70.00" not in body


def test_summary_page_reports_duplicates_and_unresolved_as_counts_not_money(
    client: TestClient,
) -> None:
    response = client.get("/summary")
    body = response.text

    assert "1 duplicate invoice" in body
    assert "1 unresolved invoice" in body


def test_summary_page_lists_discrepant_differences_with_amounts(
    client: TestClient,
) -> None:
    response = client.get("/summary")
    body = response.text

    for file_id in ("wrong-price", "quantity-overbill", "undercharge"):
        assert file_id in body
    assert "20.00" in body  # wrong-price
    assert "60.00" in body  # quantity-overbill
    assert "-10.00" in body  # undercharge, still visibly negative


def test_summary_page_names_an_improvement_citing_specific_invoices(
    client: TestClient,
) -> None:
    response = client.get("/summary")
    body = response.text

    assert "wrong-price" in body
    assert "undercharge" in body


def test_summary_page_has_no_send_control(client: TestClient) -> None:
    response = client.get("/summary")
    _assert_no_send_control(response.text)


# ---------------------------------------------------------------------------
# Provenance citations on the detail page
# ---------------------------------------------------------------------------

import html as _html
import re as _re


def _tooltip_titles(body: str) -> list[str]:
    """Return every provenance tooltip's text, decoded, in document order."""
    return [_html.unescape(t) for t in _re.findall(r'title="([^"]*)"', body, flags=_re.S)]


def _all_hrefs(body: str) -> list[str]:
    return _re.findall(r'href="([^"]*)"', body)


def test_matched_po_citation_names_the_real_matching_rule_and_seed_file(
    client: TestClient,
) -> None:
    """wrong-price matches PO-2 under supplier S1 — the citation must name
    the actual matching rule (supplier_id AND po_id together, never
    amount) and the real seed file, not a paraphrase."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    body = client.get(f"/invoices/{invoice_id}").text

    titles = _tooltip_titles(body)
    po_citation = next(t for t in titles if "Matched purchase orders table" in t)

    assert "supplier_id='S1' AND po_id='PO-2'" in po_citation
    assert "never on amount alone" in po_citation
    assert "tasks/invoices/seed.json" in po_citation


def test_unresolved_citation_explains_no_reference_and_no_guess(
    client: TestClient,
) -> None:
    """missing-reference has no po_id at all — the citation must say so,
    and must say no nearest record was guessed. This is the citation that
    proves the system declines to invent a match."""
    invoice_id = _invoice_id_for(client, "missing-reference")
    body = client.get(f"/invoices/{invoice_id}").text

    titles = _tooltip_titles(body)
    no_match_citation = next(
        t for t in titles if "purchase-order reference was printed" in t
    )

    assert "No purchase-order reference was printed on this invoice" in no_match_citation
    assert "no nearest or similar record was guessed" in no_match_citation.lower()


def test_original_column_header_citation_names_the_image_and_model_id(
    client: TestClient,
) -> None:
    """The Original citation lives on the column header, not per cell —
    one icon explains every value in the column at once."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    body = client.get(f"/invoices/{invoice_id}").text

    assert body.count("Original") >= 1
    titles = _tooltip_titles(body)
    original_citation = next(t for t in titles if "Extracted from" in t)

    assert "wrong-price.png" in original_citation
    assert "us.anthropic.claude-sonnet-4-5-20250929-v1:0" in original_citation


def test_current_column_header_citation_explains_the_column(client: TestClient) -> None:
    """The Current citation also lives on its own column header, and
    states the general rule (identical to Original unless corrected,
    corrections recorded with who and when) rather than one field's
    specific history."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    body = client.get(f"/invoices/{invoice_id}").text

    titles = _tooltip_titles(body)
    current_citation = next(
        t for t in titles if "Identical to Original unless a reviewer corrected it" in t
    )
    assert "corrections table" in current_citation


def test_uncorrected_detail_page_does_not_drown_in_icons(client: TestClient) -> None:
    """A normal, uncorrected invoice renders only the handful of citations
    that matter: the two column headers plus the matched purchase order
    and receipt. Not one icon per cell — an upper bound so 48-icons-per-
    page cannot silently regress."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    body = client.get(f"/invoices/{invoice_id}").text

    icon_count = body.count('class="prov"')
    assert icon_count <= 8, f"expected at most 8 provenance icons, found {icon_count}"


def test_corrected_field_still_shows_its_own_citation_with_previous_value(
    client: TestClient,
) -> None:
    """A field that has actually been corrected keeps its own citation,
    naming who corrected it, when, and the previous value — that is a
    fact about this one row, not the column, so the column headers alone
    cannot carry it."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    client.get(f"/invoices/{invoice_id}")  # ensure fields are loaded first

    response = client.post(
        f"/invoices/{invoice_id}/fields/sku",
        data={"value": "CABLE-X"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    body = client.get(f"/invoices/{invoice_id}").text
    titles = _tooltip_titles(body)
    corrected_citation = next(t for t in titles if t.startswith("Corrected by"))

    assert "Corrected by reviewer on" in corrected_citation
    assert "Previous value: 'CAB-1'" in corrected_citation

    # Exactly one row citation beyond the five base icons (Original
    # header, Current header, matched PO, matched receipt, and — since
    # wrong-price is discrepant — the discrepancy note's drafted-by
    # citation) — the correction adds one icon, not one per cell.
    icon_count = body.count('class="prov"')
    assert icon_count == 6, f"expected exactly 6 provenance icons after one correction, found {icon_count}"


def _all_invoice_ids(client: TestClient) -> dict[str, int]:
    """Map every file_id to its invoice_id, reading the queue table body
    only (not the status-filter links, which repeat each status name —
    including "duplicate" — outside any table row and would otherwise be
    mistaken for a file_id by a naive substring search)."""
    rows = _table_body(client.get("/invoices").text)
    ids: dict[str, int] = {}
    for match in _re.finditer(r'href="/invoices/(\d+)"[^>]*>([^<]+)<', rows):
        invoice_id, file_id = match.group(1), match.group(2)
        ids[file_id] = int(invoice_id)
    return ids


def test_no_citation_renders_the_literal_string_none(client: TestClient) -> None:
    """Covers every invoice, since a cache-less or unmatched invoice is
    exactly the case most likely to let a bare None leak into a tooltip."""
    ids = _all_invoice_ids(client)
    for file_id in ALL_FILE_IDS:
        body = client.get(f"/invoices/{ids[file_id]}").text
        assert "None" not in body, f"{file_id} detail page rendered the literal string 'None'"


def test_every_link_on_the_detail_page_resolves(client: TestClient) -> None:
    ids = _all_invoice_ids(client)
    for file_id in ALL_FILE_IDS:
        body = client.get(f"/invoices/{ids[file_id]}").text
        for href in _all_hrefs(body):
            if not href.startswith("/"):
                continue
            response = client.get(href)
            assert response.status_code != 404, f"{href} (from {file_id}'s page) 404s"


# ---------------------------------------------------------------------------
# The discrepancy note's drafted-by provenance on the detail page.
#
# The default `client` fixture builds its app with draft_notes_with_model
# left at its default (False), so every test above this point already
# proves the calculated path's provenance renders correctly (every note
# shown so far was drafted calculated). These two tests cover the model
# path explicitly, mocking the one seam
# (invoice_reconciliation.extraction.client.build_client) so neither
# makes a live call.
# ---------------------------------------------------------------------------


def _fake_note_body(note_text: str) -> MagicMock:
    payload = {
        "content": [
            {"type": "tool_use", "name": "record_discrepancy_note", "input": {"note": note_text}}
        ]
    }
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode("utf-8")
    return body


def test_calculated_note_shows_the_calculated_provenance_on_the_detail_page(
    client: TestClient,
) -> None:
    """The default client never enables model drafting — its note's
    provenance must say so plainly, not leave the path unstated."""
    invoice_id = _invoice_id_for(client, "wrong-price")
    body = client.get(f"/invoices/{invoice_id}").text

    assert "calculated" in body.lower()
    titles = _tooltip_titles(body)
    note_citation = next(t for t in titles if "no model was used" in t.lower())
    assert "calculated" in note_citation.lower()


def test_model_drafted_note_shows_the_model_provenance_on_the_detail_page(
    db_path: Path,
) -> None:
    """A verified model draft, once stored, shows which model drafted it —
    not just that a model was used. Mocks the one Bedrock call this note
    would make; no live call happens in this test."""
    good_draft = (
        "Invoice INV-2 (purchase order PO-2) bills $120.00 against an "
        "agreed $100.00 for 5 units at $20.00 each. The billed amount is "
        "$20.00 above the agreed amount."
    )
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = {"body": _fake_note_body(good_draft)}

    app = create_app(db_path=db_path, draft_notes_with_model=True)
    model_client = TestClient(app)

    with patch(
        "invoice_reconciliation.extraction.client.build_client", return_value=fake_client
    ):
        invoice_id = _invoice_id_for(model_client, "wrong-price")
        body = model_client.get(f"/invoices/{invoice_id}").text

    assert "model-drafted" in body.lower()
    titles = _tooltip_titles(body)
    note_citation = next(t for t in titles if "drafted by the model" in t.lower())
    assert "us.anthropic.claude-sonnet-4-5-20250929-v1:0" in note_citation
    fake_client.invoke_model.assert_called_once()

    # Lazy and persistent: a second view must not call the model again.
    with patch(
        "invoice_reconciliation.extraction.client.build_client", return_value=fake_client
    ):
        model_client.get(f"/invoices/{invoice_id}")
    fake_client.invoke_model.assert_called_once()

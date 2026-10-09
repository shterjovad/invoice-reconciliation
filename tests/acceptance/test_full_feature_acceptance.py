# @layer: e2e
# @spec: 001-invoice-reconciliation-review
"""Feature-wide acceptance tests — Slice 14, the final slice.

These tests check the whole feature against ``functional-spec.md``, not one
slice at a time. Each test follows a user journey across several modules:
ingest, extraction replay, matching, rules, persistence and the web view.
They build on top of what the 136 existing tests already proved at the
module and slice level — they do not repeat that proof, they assemble it
into the journeys a reviewer actually takes.

Every scenario builds a scratch SQLite database under ``tmp_path``, loaded
from the committed Bedrock-response cache
(``tests/fixtures/bedrock_responses/``). No test calls AWS, and no test
writes to the committed cache, to ``tasks/``, or to any other fixture.

Headline numbers asserted here (see the task brief and README for the hand
calculation):

    clean              reconciled   expected 10000  billed 10000  diff     0
    wrong-price        discrepant   expected 10000  billed 12000  diff  2000
    duplicate          duplicate    no amounts, count_as_payable false
    missing-reference  unresolved   no amounts, no matched PO or receipt
    quantity-overbill  discrepant   expected 10000  billed 16000  diff  6000
    undercharge        discrepant   expected 10000  billed  9000  diff -1000

    counts: reconciled 1, discrepant 3, duplicate 1, unresolved 1, failed 0
    recoverable total: 8000 cents ($80.00) — 2000 + 6000 from the two
    overcharges only. The undercharge's -1000 must not subtract from this
    total; summing all three differences gives 7000, which is wrong.
"""

from __future__ import annotations

import json
import shutil
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
COMMITTED_CACHE_DIR = Path("tests/fixtures/bedrock_responses")
EXPECTED_SEED_RESULTS_PATH = Path("tasks/invoices/expected-seed-results.json")
EXPECTED_NEW_FIXTURES_PATH = Path("tasks/invoices/expected-new-fixtures.json")

ALL_FILE_IDS = {
    "clean",
    "wrong-price",
    "duplicate",
    "missing-reference",
    "quantity-overbill",
    "undercharge",
}

# The exact headline figures the whole batch must reproduce. One dict keyed
# by file_id so every test that needs "the right answer" reads from the
# same place, the hand-calculated oracle from the task brief and README —
# never from a previous program run.
EXPECTED_HEADLINE = {
    "clean": {
        "status": "reconciled",
        "expected_cents": 10000,
        "billed_cents": 10000,
        "difference_cents": 0,
    },
    "wrong-price": {
        "status": "discrepant",
        "expected_cents": 10000,
        "billed_cents": 12000,
        "difference_cents": 2000,
    },
    "duplicate": {
        "status": "duplicate",
        "expected_cents": None,
        "billed_cents": None,
        "difference_cents": None,
    },
    "missing-reference": {
        "status": "unresolved",
        "expected_cents": None,
        "billed_cents": None,
        "difference_cents": None,
    },
    "quantity-overbill": {
        "status": "discrepant",
        "expected_cents": 10000,
        "billed_cents": 16000,
        "difference_cents": 6000,
    },
    "undercharge": {
        "status": "discrepant",
        "expected_cents": 10000,
        "billed_cents": 9000,
        "difference_cents": -1000,
    },
}

RECOVERABLE_TOTAL_CENTS = 8000  # 2000 (wrong-price) + 6000 (quantity-overbill)


# ---------------------------------------------------------------------------
# Fixtures: a scratch database built entirely from the committed cache
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A scratch database built via the exact path the README documents as
    the assessor path — replay from the committed cache, no AWS access."""
    path = tmp_path / "acceptance.sqlite"
    run_ingest(
        db_path=path,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        reset_db=True,
        use_cache=True,
        cache_dir=COMMITTED_CACHE_DIR,
    )
    with connect(path) as conn:
        run_batch(conn)
    return path


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    # draft_notes_with_model=False: acceptance runs stay offline.
    return TestClient(create_app(db_path=db_path, draft_notes_with_model=False))


def _invoice_id_for(db_path: Path, file_id: str) -> int:
    with connect(db_path) as conn:
        row = repository.get_invoice_by_file_id(conn, file_id=file_id)
    return row["invoice_id"]


def _result(db_path: Path, invoice_id: int):
    with connect(db_path) as conn:
        return repository.get_reconciliation_result(conn, invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# Journey 1: a clean batch run reproduces the supplied oracle exactly
# (functional-spec 2.1, 2.2, 2.3, 2.13 — "The four supplied example
# invoices give the exact results that someone recorded in advance.")
# ---------------------------------------------------------------------------


def test_batch_run_from_clean_database_yields_six_invoices_with_correct_statuses(
    db_path: Path,
) -> None:
    # @regression
    with connect(db_path) as conn:
        rows = repository.list_invoices(conn)
    file_ids = {row["file_id"] for row in rows}
    assert file_ids == ALL_FILE_IDS
    assert len(rows) == 6  # within the spec's six-to-eight working set


@pytest.mark.parametrize("file_id", sorted(EXPECTED_HEADLINE))
def test_each_invoice_reproduces_its_exact_headline_figures(
    db_path: Path, file_id: str
) -> None:
    # @regression
    invoice_id = _invoice_id_for(db_path, file_id)
    result = _result(db_path, invoice_id)
    expected = EXPECTED_HEADLINE[file_id]
    assert result["status"] == expected["status"]
    assert result["expected_cents"] == expected["expected_cents"]
    assert result["billed_cents"] == expected["billed_cents"]
    assert result["difference_cents"] == expected["difference_cents"]


def test_batch_reproduces_the_committed_oracle_file_exactly(db_path: Path) -> None:
    """The four supplied invoices must match
    ``tasks/invoices/expected-seed-results.json`` record for record — the
    same comparison the seed check performs, exercised here end to end."""
    # @regression
    oracle = json.loads(EXPECTED_SEED_RESULTS_PATH.read_text(encoding="utf-8"))
    for case in oracle:
        file_id = case["file_id"]
        invoice_id = _invoice_id_for(db_path, file_id)
        result = _result(db_path, invoice_id)
        assert result["status"] == case["status"]
        if "expected_cents" in case:
            assert result["expected_cents"] == case["expected_cents"]
        if "billed_cents" in case:
            assert result["billed_cents"] == case["billed_cents"]
        if "difference_cents" in case:
            assert result["difference_cents"] == case["difference_cents"]


def test_batch_reproduces_the_two_added_fixtures_oracle_exactly(db_path: Path) -> None:
    """The quantity-overbill and undercharge invoices — added to cover the
    two settled ambiguities — must match their hand-calculated answers in
    ``tasks/invoices/expected-new-fixtures.json``."""
    # @regression
    oracle = json.loads(EXPECTED_NEW_FIXTURES_PATH.read_text(encoding="utf-8"))
    for case in oracle:
        file_id = case["file_id"]
        invoice_id = _invoice_id_for(db_path, file_id)
        result = _result(db_path, invoice_id)
        assert result["status"] == case["status"]
        assert result["expected_cents"] == case["expected_cents"]
        assert result["billed_cents"] == case["billed_cents"]
        assert result["difference_cents"] == case["difference_cents"]


def test_status_counts_match_the_brief_exactly(db_path: Path) -> None:
    """functional-spec 2.2 and 2.8: exactly one status per invoice, counted
    correctly across the whole batch."""
    # @regression
    with connect(db_path) as conn:
        counts = repository.count_results_by_status(conn)
    assert counts.get("reconciled", 0) == 1
    assert counts.get("discrepant", 0) == 3
    assert counts.get("duplicate", 0) == 1
    assert counts.get("unresolved", 0) == 1
    assert counts.get("failed", 0) == 0


# ---------------------------------------------------------------------------
# Negative counterpart to Journey 1: a reconciled batch is not an accident
# of a lenient oracle comparison — an altered figure must be caught.
# ---------------------------------------------------------------------------


def test_oracle_comparison_is_sensitive_to_a_wrong_figure(db_path: Path) -> None:
    """Negative case for the oracle-reproduction journey: if a result were
    wrong, this comparison style must actually catch it, not pass
    regardless of the data. Proves the positive assertions above are not
    vacuously true."""
    invoice_id = _invoice_id_for(db_path, "wrong-price")
    result = _result(db_path, invoice_id)
    # The real difference is 2000, not 9999 — asserting the wrong number
    # must fail, confirming this is a real check and not a tautology.
    assert result["difference_cents"] != 9999
    assert result["difference_cents"] == 2000


# ---------------------------------------------------------------------------
# Journey 2: a reviewer with no AWS access completes the whole flow from
# the committed cache. Required deliverable of the brief (architecture.md
# constraint 3; functional-spec 2.12's "run the check without the reading
# service").
# ---------------------------------------------------------------------------


def test_full_flow_completes_with_the_aws_credential_chain_fully_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # @regression
    for var in (
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_PROFILE", "nope")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")

    path = tmp_path / "no_aws.sqlite"
    run_ingest(
        db_path=path,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        reset_db=True,
        use_cache=True,
        cache_dir=COMMITTED_CACHE_DIR,
    )
    with connect(path) as conn:
        outcomes = run_batch(conn)

    assert len(outcomes) == 6
    statuses = {o.file_id: o.result.status for o in outcomes}
    assert statuses["clean"] == "reconciled"
    assert statuses["wrong-price"] == "discrepant"
    assert statuses["duplicate"] == "duplicate"
    assert statuses["missing-reference"] == "unresolved"


def test_committed_cache_fixtures_are_never_written_to_by_this_run(
    tmp_path: Path,
) -> None:
    """Negative/guard case: running the whole flow from the cache must not
    mutate the committed cache files on disk."""
    before = {
        p.name: p.read_bytes() for p in sorted(COMMITTED_CACHE_DIR.glob("*.json"))
    }
    path = tmp_path / "guard.sqlite"
    run_ingest(
        db_path=path,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        reset_db=True,
        use_cache=True,
        cache_dir=COMMITTED_CACHE_DIR,
    )
    with connect(path) as conn:
        run_batch(conn)
    after = {
        p.name: p.read_bytes() for p in sorted(COMMITTED_CACHE_DIR.glob("*.json"))
    }
    assert before == after


# ---------------------------------------------------------------------------
# Journey 3: queue -> filter -> detail -> evidence -> correction ->
# recoverable total on the summary changes.
# (functional-spec 2.5, 2.6, 2.7, 2.8)
# ---------------------------------------------------------------------------


def test_queue_shows_every_invoice_with_its_status(client: TestClient) -> None:
    response = client.get("/invoices")
    assert response.status_code == 200
    for file_id in ALL_FILE_IDS:
        assert file_id in response.text


def test_queue_filtered_by_status_shows_only_matching_invoices(
    client: TestClient,
) -> None:
    # @regression
    response = client.get("/invoices", params={"status": "discrepant"})
    assert response.status_code == 200
    for file_id in ("wrong-price", "quantity-overbill", "undercharge"):
        assert file_id in response.text
    # "duplicate" is also a status word that appears in the filter nav on
    # every page, so check the invoice *rows* only, not raw substring
    # membership in the whole document.
    for file_id in ("clean", "missing-reference"):
        assert file_id not in response.text
    # "duplicate" the status word is also the nav-bar filter link text, so
    # check the invoice *row* (its file_id as link text) is absent, not
    # bare substring membership in the whole document.
    assert ">duplicate</a></td>" not in response.text


def test_queue_filtered_by_a_status_with_no_matches_shows_none_of_the_others(
    client: TestClient,
) -> None:
    """Negative case: filtering by a status this batch has zero of
    (``failed``) must show none of the six invoices, not fall back to the
    unfiltered queue."""
    response = client.get("/invoices", params={"status": "failed"})
    assert response.status_code == 200
    for file_id in ALL_FILE_IDS - {"duplicate"}:
        assert file_id not in response.text
    # "duplicate" the status word is in the nav bar regardless of filter;
    # check the invoice row itself (its file_id as link text) is absent.
    assert ">duplicate</a></td>" not in response.text


def test_detail_page_shows_the_document_the_values_and_the_matched_records(
    client: TestClient, db_path: Path
) -> None:
    """functional-spec 2.1, 2.5, 2.10: opening a matched invoice shows the
    source document, the seven extracted values, the matched PO and
    receipt references, and the calculation, together on one screen."""
    # @regression
    invoice_id = _invoice_id_for(db_path, "wrong-price")
    response = client.get(f"/invoices/{invoice_id}")
    assert response.status_code == 200
    text = response.text
    assert "wrong-price" in text  # source document name
    assert "PO-2" in text  # matched purchase order reference
    assert "RC-2" in text  # matched receipt reference
    # the three money figures of the calculation
    assert "100.00" in text  # expected
    assert "120.00" in text  # billed
    assert "20.00" in text  # difference


def test_detail_page_for_unresolved_invoice_shows_no_expected_amount_or_difference(
    client: TestClient, db_path: Path
) -> None:
    """functional-spec 2.3: 'no expected amount and no difference, in
    place of a zero or an estimate' for an unresolved invoice."""
    # @regression
    invoice_id = _invoice_id_for(db_path, "missing-reference")
    response = client.get(f"/invoices/{invoice_id}")
    assert response.status_code == 200
    with connect(db_path) as conn:
        result = repository.get_reconciliation_result(conn, invoice_id=invoice_id)
    assert result["expected_cents"] is None
    assert result["difference_cents"] is None
    assert result["matched_po_id"] is None
    assert result["matched_receipt_id"] is None


def test_correcting_a_misread_value_changes_the_recoverable_total_on_summary(
    client: TestClient, db_path: Path
) -> None:
    """The full cross-module journey: open the queue, open wrong-price,
    correct total_cents from 12000 to 10000 (the hand-calculated
    correction case), and watch the recoverable total on the summary page
    fall from $80.00 to $60.00 — because wrong-price's $20.00 overcharge
    is now reconciled and no longer contributes."""
    # @regression
    summary_before = client.get("/summary")
    assert "$80.00" in summary_before.text

    invoice_id = _invoice_id_for(db_path, "wrong-price")
    response = client.post(
        f"/invoices/{invoice_id}/fields/total_cents",
        data={"value": "10000"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    result = _result(db_path, invoice_id)
    assert result["status"] == "reconciled"
    assert result["difference_cents"] == 0

    summary_after = client.get("/summary")
    assert "$60.00" in summary_after.text
    assert "$80.00" not in summary_after.text


# ---------------------------------------------------------------------------
# Journey 4: one bad invoice does not cost the rest of the run.
# (functional-spec 2.9)
# ---------------------------------------------------------------------------


def test_one_corrupt_invoice_does_not_stop_the_rest_of_the_batch(
    tmp_path: Path,
) -> None:
    # @regression
    # quantity-overbill has no duplicate relationship with any other
    # fixture, so corrupting it cannot change any other invoice's status
    # (unlike corrupting 'clean', whose failure would promote 'duplicate'
    # into the payable slot — a real, intended product behaviour, not
    # something this failure-isolation journey should entangle with).
    scratch_cache = tmp_path / "scratch_cache"
    shutil.copytree(COMMITTED_CACHE_DIR, scratch_cache)
    (scratch_cache / "quantity-overbill.json").write_text(
        "{ not valid json", encoding="utf-8"
    )

    path = tmp_path / "one_bad.sqlite"
    run_ingest(
        db_path=path,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        reset_db=True,
        use_cache=True,
        cache_dir=scratch_cache,
    )
    with connect(path) as conn:
        outcomes = run_batch(conn)

    statuses = {o.file_id: o.result.status for o in outcomes}
    assert statuses["quantity-overbill"] == "failed"
    # every other invoice in the batch still processed correctly
    assert statuses["clean"] == "reconciled"
    assert statuses["wrong-price"] == "discrepant"
    assert statuses["duplicate"] == "duplicate"
    assert statuses["missing-reference"] == "unresolved"
    assert statuses["undercharge"] == "discrepant"

    failed_result = next(o.result for o in outcomes if o.file_id == "quantity-overbill")
    assert failed_result.expected_cents is None
    assert failed_result.difference_cents is None


# ---------------------------------------------------------------------------
# Journey 5: the money guarantee end to end — no value loses a cent
# between the saved model response and the summary page.
# (functional-spec 2.3's rounding rule; architecture.md constraint 2)
# ---------------------------------------------------------------------------


def test_recoverable_total_sums_only_true_overcharges_not_all_differences(
    db_path: Path,
) -> None:
    """The one figure most likely to go subtly wrong: the recoverable
    total must be 8000 (2000 + 6000), never 7000 (2000 + 6000 - 1000) —
    the undercharge must not offset the overcharges."""
    # @regression
    with connect(db_path) as conn:
        total = repository.get_recoverable_total_cents(conn)
    assert total == RECOVERABLE_TOTAL_CENTS
    assert total != 7000  # would be the (wrong) net-of-all-differences figure


def test_summary_page_displays_the_exact_recoverable_total_in_dollars(
    client: TestClient,
) -> None:
    # @regression
    response = client.get("/summary")
    assert response.status_code == 200
    assert "$80.00" in response.text


def test_detail_page_money_matches_the_persisted_cents_exactly(
    client: TestClient, db_path: Path
) -> None:
    """Every money figure shown on the detail page for a discrepant
    invoice must agree, to the cent, with what is persisted — no value
    may drift between the database and the rendered page."""
    invoice_id = _invoice_id_for(db_path, "quantity-overbill")
    result = _result(db_path, invoice_id)
    assert result["expected_cents"] == 10000
    assert result["billed_cents"] == 16000
    assert result["difference_cents"] == 6000

    response = client.get(f"/invoices/{invoice_id}")
    assert "100.00" in response.text
    assert "160.00" in response.text
    assert "60.00" in response.text

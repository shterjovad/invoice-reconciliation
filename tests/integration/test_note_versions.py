# @layer: integration
# @spec: 001-invoice-reconciliation-review
"""Versioned discrepancy notes and the full correction chain.

Drives the real app against a scratch database built the way the headless
batch builds it. The model is mocked at the one seam
(``extraction.client.build_client``); no test reaches the network.
"""

from __future__ import annotations

import html as _html
import json
import re as _re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

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

# wrong-price: INV-2 on PO-2, $120.00 billed against an agreed $100.00.
MODEL_NOTE = (
    "Invoice INV-2 (purchase order PO-2) bills $120.00 against an "
    "agreed $100.00. The billed amount is $20.00 above the agreed "
    "amount."
)
MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def _build(path: Path, *, with_model: bool) -> Path:
    run_ingest(
        db_path=path,
        seed_path=SEED_PATH,
        images_dir=IMAGES_DIR,
        reset_db=True,
        use_cache=True,
        cache_dir=CACHE_DIR,
    )
    with connect(path) as conn:
        if with_model:
            body = MagicMock()
            body.read.return_value = json.dumps(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "record_discrepancy_note",
                            "input": {"note": MODEL_NOTE},
                        }
                    ]
                }
            ).encode("utf-8")
            fake = MagicMock()
            fake.invoke_model.return_value = {"body": body}
            with patch(
                "invoice_reconciliation.extraction.client.build_client", return_value=fake
            ):
                run_batch(conn, draft_notes_with_model=True)
        else:
            run_batch(conn)
    return path


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Batch with the mocked model: INV-2's note verifies, so v1 is 'model'."""
    return _build(tmp_path / "versions.sqlite", with_model=True)


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path=db_path, draft_notes_with_model=False))


def _invoice_id(db_path: Path, file_id: str = "wrong-price") -> int:
    with connect(db_path) as conn:
        return repository.get_invoice_by_file_id(conn, file_id=file_id)["invoice_id"]


def _versions(db_path: Path, invoice_id: int):
    with connect(db_path) as conn:
        return repository.list_note_versions(conn, invoice_id=invoice_id)


def _save(client: TestClient, invoice_id: int, text: str) -> None:
    response = client.post(
        f"/invoices/{invoice_id}/note", data={"text": text}, follow_redirects=False
    )
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Note versions
# ---------------------------------------------------------------------------


def test_batch_records_v1_as_model_with_the_model_id(db_path: Path) -> None:
    versions = _versions(db_path, _invoice_id(db_path))
    assert len(versions) == 1
    v1 = versions[0]
    assert v1["version_no"] == 1
    assert v1["source"] == "model"
    assert v1["created_by"] == "model"
    assert v1["model_id"] == MODEL_ID
    assert v1["text"] == MODEL_NOTE


def test_batch_records_a_calculated_v1_where_the_model_draft_did_not_verify(
    db_path: Path,
) -> None:
    """The mocked model returns INV-2's note for every invoice; the other
    discrepant invoices have different figures, so verification rejects it
    and they fall back to a calculated v1, created by 'system'."""
    other = _versions(db_path, _invoice_id(db_path, "quantity-overbill"))
    assert [(v["version_no"], v["source"], v["created_by"]) for v in other] == [
        (1, "calculated", "system")
    ]


def test_a_reviewer_save_adds_v2_and_leaves_v1_unchanged(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id(db_path)
    v1_before = dict(_versions(db_path, invoice_id)[0])

    _save(client, invoice_id, "Chasing the supplier by phone.")

    versions = _versions(db_path, invoice_id)
    assert [v["version_no"] for v in versions] == [1, 2]
    assert dict(versions[0]) == v1_before
    assert versions[1]["source"] == "reviewer"
    assert versions[1]["created_by"] == "reviewer"
    assert versions[1]["text"] == "Chasing the supplier by phone."
    assert versions[1]["model_id"] is None


def test_saving_identical_text_adds_no_version(client: TestClient, db_path: Path) -> None:
    invoice_id = _invoice_id(db_path)
    _save(client, invoice_id, "Chasing the supplier by phone.")
    _save(client, invoice_id, "Chasing the supplier by phone.")
    assert len(_versions(db_path, invoice_id)) == 2

    # Saving the untouched model draft back is also not an edit.
    other = _invoice_id(db_path, "quantity-overbill")
    with connect(db_path) as conn:
        draft = repository.get_discrepancy_note(conn, invoice_id=other)["current_text"]
    _save(client, other, draft)
    assert len(_versions(db_path, other)) == 1


def test_a_second_reviewer_save_adds_v3(client: TestClient, db_path: Path) -> None:
    invoice_id = _invoice_id(db_path)
    _save(client, invoice_id, "First edit.")
    _save(client, invoice_id, "Second edit.")
    versions = _versions(db_path, invoice_id)
    assert [v["version_no"] for v in versions] == [1, 2, 3]
    assert [v["source"] for v in versions] == ["model", "reviewer", "reviewer"]
    assert versions[2]["text"] == "Second edit."


def test_versions_survive_a_new_connection(client: TestClient, db_path: Path) -> None:
    invoice_id = _invoice_id(db_path)
    _save(client, invoice_id, "Kept on disk.")
    # A brand-new app and connection read the same history back.
    fresh = TestClient(create_app(db_path=db_path, draft_notes_with_model=False))
    assert [v["text"] for v in _versions(db_path, invoice_id)] == [
        MODEL_NOTE,
        "Kept on disk.",
    ]
    assert "Kept on disk." in fresh.get(f"/invoices/{invoice_id}").text


def test_a_correction_after_a_reviewer_edit_keeps_the_reviewer_version(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id(db_path)
    _save(client, invoice_id, "Reviewer words.")
    before = [dict(v) for v in _versions(db_path, invoice_id)]

    response = client.post(
        f"/invoices/{invoice_id}/fields/total_cents",
        data={"value": "13000"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    after = [dict(v) for v in _versions(db_path, invoice_id)]
    assert after == before
    assert after[-1]["source"] == "reviewer"
    with connect(db_path) as conn:
        note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
    assert note["current_text"] == "Reviewer words."
    assert bool(note["edit_superseded"]) is True


def test_a_correction_redraft_is_a_new_version_not_a_replacement(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id(db_path)
    response = client.post(
        f"/invoices/{invoice_id}/fields/total_cents",
        data={"value": "13000"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    versions = _versions(db_path, invoice_id)
    assert [v["version_no"] for v in versions] == [1, 2]
    assert versions[0]["text"] == MODEL_NOTE
    assert "130.00" in versions[1]["text"]
    with connect(db_path) as conn:
        note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
    assert note["current_text"] == versions[1]["text"]


class _Ancestors(HTMLParser):
    """Records, for each <ol class="note-versions">, the tags open above it."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.above_history: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "ol" and ("class", "note-versions") in attrs:
            self.above_history.append(list(self.stack))
        if tag not in {"br", "img", "input", "meta", "link"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            while self.stack.pop() != tag:
                pass


def test_history_renders_in_order_with_the_current_version_marked(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id(db_path)
    _save(client, invoice_id, "Reviewer words.")
    body = client.get(f"/invoices/{invoice_id}").text

    assert "Version history" in body
    # Search inside the history only: the label above the editor also names
    # the current version, so a page-wide search would match there first.
    history = body[body.index("Version history"):]
    first = history.index("v1 &middot; model-drafted")
    second = history.index("v2 &middot; edited by reviewer")
    assert first < second
    assert MODEL_NOTE in body and "Reviewer words." in body

    items = _re.findall(r'<li class="note-version[^"]*">.*?</li>', body, flags=_re.S)
    assert len(items) == 2
    assert "note-version-current" not in items[0]
    assert "note-version-current" in items[1]
    assert body.count('class="note-version-current"') == 1

    # Provenance: model id on v1; who and when on the reviewer version.
    assert MODEL_ID in _html.unescape(items[0])
    assert "Edited by reviewer on" in _html.unescape(items[1])

    # Valid nesting: the history list is not inside a paragraph.
    parser = _Ancestors()
    parser.feed(body)
    assert parser.above_history and all("p" not in above for above in parser.above_history)


def test_a_single_version_still_shows_its_history(client: TestClient, db_path: Path) -> None:
    body = client.get(f"/invoices/{_invoice_id(db_path)}").text
    assert "v1 &middot; model-drafted" in body
    assert body.count('class="note-version-current"') == 1


# ---------------------------------------------------------------------------
# Full correction chain
# ---------------------------------------------------------------------------


def _popups(body: str) -> list[list[str]]:
    popups = _re.findall(
        r'<span class="prov-popup"[^>]*>((?:\s*<span class="prov-line">.*?</span>)*)',
        body,
        flags=_re.S,
    )
    return [
        [_html.unescape(x.strip()) for x in _re.findall(r'<span class="prov-line">(.*?)</span>', p, flags=_re.S)]
        for p in popups
    ]


def _chain(body: str) -> list[str]:
    return next(p for p in _popups(body) if p[0].startswith("v0 original"))


def _correct(client: TestClient, invoice_id: int, field: str, value: str) -> None:
    response = client.post(
        f"/invoices/{invoice_id}/fields/{field}", data={"value": value}, follow_redirects=False
    )
    assert response.status_code == 303


def test_two_successive_corrections_show_the_whole_chain_in_order(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id(db_path)
    _correct(client, invoice_id, "total_cents", "11000")
    _correct(client, invoice_id, "total_cents", "10500")

    chain = _chain(client.get(f"/invoices/{invoice_id}").text)
    assert len(chain) == 3
    assert chain[0] == "v0 original (extracted): 12000"
    assert chain[1].startswith("v1 12000 → 11000, by reviewer at ")
    assert chain[2].startswith("v2 11000 → 10500, by reviewer at ")
    assert "(current)" not in chain[1]
    assert chain[2].endswith("(current)")


def test_one_correction_still_shows_a_two_line_chain(client: TestClient, db_path: Path) -> None:
    invoice_id = _invoice_id(db_path)
    _correct(client, invoice_id, "sku", "CABLE-X")
    chain = _chain(client.get(f"/invoices/{invoice_id}").text)
    assert chain[0] == "v0 original (extracted): CAB-1"
    assert chain[1].startswith("v1 CAB-1 → CABLE-X, by reviewer at ")
    assert chain[1].endswith("(current)")


def test_an_uncorrected_field_has_no_row_marker(client: TestClient, db_path: Path) -> None:
    body = client.get(f"/invoices/{_invoice_id(db_path)}").text
    assert not any(p[0].startswith("v0 original") for p in _popups(body))
    assert "field-corrected" not in body


def test_a_field_corrected_back_to_its_original_keeps_its_history(
    client: TestClient, db_path: Path
) -> None:
    invoice_id = _invoice_id(db_path)
    _correct(client, invoice_id, "total_cents", "11000")
    _correct(client, invoice_id, "total_cents", "12000")
    chain = _chain(client.get(f"/invoices/{invoice_id}").text)
    assert len(chain) == 3
    assert chain[2].startswith("v2 11000 → 12000")

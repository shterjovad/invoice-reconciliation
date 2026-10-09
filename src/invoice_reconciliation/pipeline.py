"""The pipeline: the single path that runs reconciliation for one invoice or
for the whole set.

This module wires ``reconciliation/matcher.py`` and
``reconciliation/rules.py`` together with ``db/repository.py``. It contains
no reconciliation logic of its own — no status decisions, no money
arithmetic, no duplicate-detection rule beyond calling the matcher. One
pipeline, two entry points: the headless batch command and the FastAPI web
view (including a reviewer correction, in a later slice) both call
``recalculate_one`` for a single invoice, or ``run_batch`` for all of them.
Neither grows its own copy of the reconciliation path.

``ensure_discrepancy_note`` lives here too, for the same reason: it is the
one note-drafting decision both ``run_batch`` (drafting during the batch,
so a reviewer's first click reads a stored row instead of waiting on a
model call) and the web correction/detail routes (the lazy fallback for an
invoice that became discrepant through a correction, never through the
batch) must share. Neither caller grows its own copy of that decision.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from invoice_reconciliation.db import repository
from invoice_reconciliation.money import MoneyFormatError
from invoice_reconciliation.reconciliation import matcher, rules
from invoice_reconciliation.reconciliation.notes import draft_discrepancy_note, draft_note

__all__ = [
    "InvoiceOutcome",
    "NoteOutcome",
    "recalculate_one",
    "run_batch",
    "ensure_discrepancy_note",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NoteOutcome:
    """What happened when ``ensure_discrepancy_note`` considered one invoice.

    ``drafted`` is ``False`` when there was nothing to do: the invoice is
    not discrepant, a note row already existed (drafting is once per
    invoice — never redrafted), or the existing row carries a reviewer's
    edit (never overwritten; see ``ensure_discrepancy_note``). ``attempts``
    and ``drafted_by`` are ``None`` whenever ``drafted`` is ``False``, since
    nothing was drafted to report on.
    """

    invoice_id: int
    file_id: str
    drafted: bool
    drafted_by: str | None
    attempts: int | None


@dataclass(frozen=True, slots=True)
class InvoiceOutcome:
    """What happened for one invoice during a batch or single recompute.

    ``note`` is ``None`` for ``recalculate_one`` (which never drafts a
    note itself) and for any invoice ``run_batch`` did not consider for
    drafting (not discrepant). For a discrepant invoice processed by
    ``run_batch``, it carries that invoice's ``NoteOutcome`` — the CLI
    reads this to report drafting attempts alongside the reconciliation
    line, without a second pass over the batch or a second return value.
    """

    invoice_id: int
    file_id: str
    result: rules.ReconciliationResult
    note: NoteOutcome | None = None


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601, for ``computed_at``."""
    return datetime.now(timezone.utc).isoformat()


def recalculate_one(conn: sqlite3.Connection, invoice_id: int) -> rules.ReconciliationResult:
    """Recompute and persist the reconciliation result for one invoice.

    This is the function a web correction calls (Slice 11) and the function
    the batch loop calls per invoice — the one path both entry points share.

    Steps:

    1. Read the invoice row (for ``received_at`` and ``extraction_failed``)
       and its current field values.
    2. Find the matching purchase order and receipt
       (``matcher.find_purchase_order_and_receipt``).
    3. Determine whether an earlier duplicate exists
       (``matcher.find_earlier_duplicate``), excluding this invoice itself.
    4. Call ``rules.reconcile`` — the pure decision function, passing
       ``extraction_failed`` straight from the invoice row. This is what
       tells a genuinely failed extraction (``failed``) apart from a
       successful extraction that found no purchase-order reference
       (``unresolved``, e.g. ``missing-reference``) — both leave
       ``current_fields`` looking the same (``po_id`` absent or null), so
       the distinction must come from this stored flag, not from the field
       values. If ``rules.reconcile`` still raises ``MoneyFormatError``
       (malformed money text already stored for this invoice), the result
       is recorded as ``failed`` with no amounts instead of propagating —
       one bad invoice must not abort the batch.
    5. Persist the result into ``reconciliation_results``, overwriting any
       existing row for this invoice.

    Returns the ``ReconciliationResult`` that was persisted.
    """
    current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)

    invoice_row = conn.execute(
        """
        SELECT invoice_id, received_at, extraction_failed
        FROM invoices
        WHERE invoice_id = ?
        """,
        (invoice_id,),
    ).fetchone()
    received_at = invoice_row["received_at"]
    extraction_failed = bool(invoice_row["extraction_failed"])

    match = matcher.find_purchase_order_and_receipt(conn, current_fields=current_fields)

    is_duplicate = False
    supplier_id = current_fields.get("supplier_id")
    invoice_number = current_fields.get("invoice_number")
    if supplier_id is not None and invoice_number is not None:
        earlier = matcher.find_earlier_duplicate(
            conn,
            supplier_id=supplier_id,
            invoice_number=invoice_number,
            received_at=received_at,
            exclude_invoice_id=invoice_id,
        )
        is_duplicate = earlier is not None

    try:
        result = rules.reconcile(
            current_fields=current_fields,
            match=match,
            is_duplicate=is_duplicate,
            extraction_failed=extraction_failed,
        )
    except MoneyFormatError:
        # Malformed money text already stored in the database (not a
        # programming bug) must not abort the batch. Isolate this one
        # invoice as 'failed', with no amounts, and let every other
        # invoice keep processing. ``rules.reconcile`` itself stays pure
        # and keeps raising on bad input — this is the one place that
        # catches it.
        result = rules.ReconciliationResult(status="failed")

    repository.upsert_reconciliation_result(
        conn,
        invoice_id=invoice_id,
        status=result.status,
        expected_cents=result.expected_cents,
        billed_cents=result.billed_cents,
        difference_cents=result.difference_cents,
        count_as_payable=result.count_as_payable,
        matched_po_id=result.matched_po_id,
        matched_receipt_id=result.matched_receipt_id,
        computed_at=_now_iso(),
    )

    return result


def ensure_discrepancy_note(
    conn: sqlite3.Connection, invoice_id: int, *, use_model: bool = False, force: bool = False
) -> NoteOutcome:
    """Draft the discrepancy note for ``invoice_id``, if it needs one.

    This is not a second reconciliation path: it reads the figures
    ``recalculate_one`` already computed and persisted, and passes them to
    ``notes.draft_discrepancy_note`` — no status decision and no money
    arithmetic happens here. It is the one note-drafting decision shared
    by ``run_batch`` (drafting during the batch, so a reviewer's first
    click reads a stored row instead of waiting on a model call) and the
    web layer (``web.routes``'s detail and correction routes) — neither
    caller grows its own copy of it.

    **Drafting is once per invoice, by default.** With ``force=False``
    (``run_batch``'s use, and the detail route's lazy fallback), if a note
    row already exists this function does nothing and returns
    ``drafted=False`` — it never redrafts, whether that row was written by
    an earlier batch run, a correction, or an earlier call to this
    function. This is what makes it safe to call unconditionally for
    every discrepant invoice on every batch run: the first call drafts,
    every later call is a no-op read-check.

    **A correction forces a redraft of its own invoice's note.** With
    ``force=True`` (the correction route's use, immediately after
    ``recalculate_one`` changes this one invoice's figures), an existing,
    non-reviewer-edited note row *is* replaced — a correction changes the
    very figures the note describes, so leaving the old text in place
    would describe amounts that no longer match the detail page around it.
    This is the one path where "once per invoice" does not apply: it is
    scoped to the one invoice the correction just touched, never to the
    five it did not.

    **A reviewer's edit is never overwritten, force or not.** If the
    existing note row has ``is_reviewer_edited = 1``, this function does
    not redraft at all, regardless of ``force`` — the figures may have
    moved, but the reviewer's own words stay exactly as written. Instead
    it marks the row ``edit_superseded`` so the reviewer can see the edit
    may now describe stale figures and decide whether to redraft (the UI
    surfaces this flag; see ``detail.html``). This is the one branch where
    this function writes to an *existing* row rather than leaving it
    untouched or replacing it outright.

    **Drafting failure never fails the caller.** ``notes.
    draft_discrepancy_note`` already falls back to the calculated note on
    any model error and never raises — but a defensive ``except Exception``
    around the call still marks the note ``calculated`` and returns
    normally on any unexpected failure, exactly like a failed extraction
    isolates to one invoice in ``recalculate_one``. One slow or broken note
    must not cost the batch the other five.

    ``use_model`` controls whether the one drafting attempt may call the
    model at all; it defaults to ``False`` (the calculated path) so that
    calling this function never requires AWS credentials or makes a
    network call unless a caller opts in explicitly.

    A non-``discrepant`` invoice (reconciled, duplicate, unresolved,
    failed) is left with no note row at all — nothing to draft, since
    there is no difference to describe. An invoice that *was* discrepant
    and a correction resolved it (no longer discrepant) is not cleaned up
    here: that is a deliberate no-op, left as a recorded gap rather than
    silently deleting history, since no route reads a stale note for a
    non-discrepant invoice.
    """
    invoice_row = conn.execute(
        "SELECT file_id FROM invoices WHERE invoice_id = ?", (invoice_id,)
    ).fetchone()
    file_id = invoice_row["file_id"] if invoice_row is not None else ""

    result_row = repository.get_reconciliation_result(conn, invoice_id=invoice_id)
    if result_row is None or result_row["status"] != "discrepant":
        return NoteOutcome(
            invoice_id=invoice_id, file_id=file_id, drafted=False, drafted_by=None, attempts=None
        )

    existing_note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
    if existing_note is not None and bool(existing_note["is_reviewer_edited"]):
        repository.mark_discrepancy_note_edit_superseded(conn, invoice_id=invoice_id)
        return NoteOutcome(
            invoice_id=invoice_id, file_id=file_id, drafted=False, drafted_by=None, attempts=None
        )

    if existing_note is not None and not force:
        # Drafting is once per invoice: a note row already exists (from an
        # earlier batch run, an earlier correction, or an earlier call to
        # this function) and is not a reviewer edit, and this call was not
        # asked to force a redraft — so there is nothing stale about it to
        # redraft here, leave it exactly as it is.
        return NoteOutcome(
            invoice_id=invoice_id, file_id=file_id, drafted=False, drafted_by=None, attempts=None
        )

    current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)
    invoice_number = current_fields.get("invoice_number") or "–"
    po_id = result_row["matched_po_id"] or "–"

    purchase_order = None
    supplier_id = current_fields.get("supplier_id")
    if supplier_id is not None and result_row["matched_po_id"] is not None:
        purchase_order = repository.get_purchase_order(
            conn, supplier_id=supplier_id, po_id=result_row["matched_po_id"]
        )

    # The matched purchase order is looked up above rather than parsed
    # from current_fields' own quantity/unit_cents text, for the same
    # reason rules.reconcile computes expected_cents from the purchase
    # order: the agreed quantity and unit price are the PO's, never the
    # invoice's own (possibly different, possibly corrected-but-still-
    # discrepant) claimed values.
    quantity = int(purchase_order["quantity"]) if purchase_order is not None else 0
    unit_cents = int(purchase_order["unit_cents"]) if purchase_order is not None else 0

    try:
        drafted = draft_discrepancy_note(
            invoice_number=invoice_number,
            po_id=po_id,
            quantity=quantity,
            unit_cents=unit_cents,
            expected_cents=result_row["expected_cents"],
            billed_cents=result_row["billed_cents"],
            difference_cents=result_row["difference_cents"],
            use_model=use_model,
        )
    except Exception as exc:  # noqa: BLE001 - one bad note must not cost the batch
        # notes.draft_discrepancy_note already catches every model/network
        # failure itself and falls back to the calculated note without
        # raising — this branch only guards against a future change to
        # that contract (or a genuinely unexpected bug), exactly as
        # recalculate_one's MoneyFormatError guard isolates one invoice's
        # failure from the rest of the batch.
        logger.warning(
            "drafting the discrepancy note for invoice %r (%s) raised %s: %s; "
            "falling back to a calculated note",
            invoice_id,
            file_id,
            type(exc).__name__,
            exc,
        )
        calculated_text = draft_note(
            invoice_number=invoice_number,
            po_id=po_id,
            quantity=quantity,
            unit_cents=unit_cents,
            expected_cents=result_row["expected_cents"],
            billed_cents=result_row["billed_cents"],
            difference_cents=result_row["difference_cents"],
        )
        repository.upsert_discrepancy_note(
            conn,
            invoice_id=invoice_id,
            drafted_text=calculated_text,
            drafted_by="calculated",
            model_id=None,
            drafted_at=None,
            attempts=0,
            rejection_reasons=None,
            created_at=_now_iso(),
        )
        return NoteOutcome(
            invoice_id=invoice_id,
            file_id=file_id,
            drafted=True,
            drafted_by="calculated",
            attempts=0,
        )

    repository.upsert_discrepancy_note(
        conn,
        invoice_id=invoice_id,
        drafted_text=drafted.text,
        drafted_by=drafted.drafted_by,
        model_id=drafted.model_id,
        drafted_at=_now_iso() if drafted.drafted_by == "model" else None,
        attempts=drafted.attempts,
        rejection_reasons=(
            json.dumps(drafted.rejection_reasons) if drafted.rejection_reasons else None
        ),
        created_at=_now_iso(),
    )
    return NoteOutcome(
        invoice_id=invoice_id,
        file_id=file_id,
        drafted=True,
        drafted_by=drafted.drafted_by,
        attempts=drafted.attempts,
    )


def run_batch(
    conn: sqlite3.Connection, *, draft_notes_with_model: bool = False
) -> list[InvoiceOutcome]:
    """Recompute reconciliation for every invoice, in a stable order.

    Invoices are processed ordered by ``received_at`` (ties broken by
    ``invoice_id``) so duplicate detection behaves identically on every run:
    the first copy by ``received_at`` is evaluated before any later copy
    sharing its ``(supplier_id, invoice_number)``, and so stays payable.

    After each invoice's reconciliation result is persisted, this also
    calls ``ensure_discrepancy_note`` for every invoice that just became
    ``discrepant`` — so its note is already in the database by the time
    anyone opens the detail page (a stored-row read, a few milliseconds)
    rather than being drafted on that reviewer's first click (a live model
    call, 3+ seconds). This reuses exactly the same drafting decision the
    web layer's lazy fallback uses (``ensure_discrepancy_note``, this
    module); it is not a second implementation. That function already
    isolates a drafting failure to the one invoice it happened on (falling
    back to a calculated note, like a failed extraction isolates in
    ``recalculate_one``), so one slow or broken note never aborts the
    batch.

    ``draft_notes_with_model`` defaults to ``False``, matching
    ``ensure_discrepancy_note``'s own credential-free default: a bare call
    still drafts a note for every newly-discrepant invoice (removing the
    first-click stall), but with the pure, calculated text and no network
    call — so a bare ``run_batch(conn)`` never requires AWS credentials,
    exactly as before this feature existed, and existing callers that
    never asked for model drafting see no new behaviour requiring it. Pass
    ``True`` (the CLI's production default, matching
    ``web.app.create_app``'s own default) to let each draft attempt a
    verified model call first, falling back to calculated per invoice —
    never raising — exactly as ``notes.draft_discrepancy_note`` already
    does on any credential or network failure.

    Returns one ``InvoiceOutcome`` per invoice, in the order processed —
    each carrying that invoice's ``NoteOutcome`` on ``.note`` when drafting
    was attempted for it, so the CLI can report drafting attempts per
    invoice without a second pass over the batch.
    """
    rows = conn.execute(
        """
        SELECT invoice_id, file_id
        FROM invoices
        ORDER BY received_at ASC, invoice_id ASC
        """
    ).fetchall()

    outcomes: list[InvoiceOutcome] = []
    for row in rows:
        result = recalculate_one(conn, row["invoice_id"])
        note_outcome: NoteOutcome | None = None
        if result.status == "discrepant":
            note_outcome = ensure_discrepancy_note(
                conn, row["invoice_id"], use_model=draft_notes_with_model
            )
        outcomes.append(
            InvoiceOutcome(
                invoice_id=row["invoice_id"],
                file_id=row["file_id"],
                result=result,
                note=note_outcome,
            )
        )
    return outcomes

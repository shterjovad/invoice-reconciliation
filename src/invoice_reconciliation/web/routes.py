"""The reviewer-UI routes (``technical-considerations.md`` section 2.6).

This slice implements the queue, the detail page, the image route, and the
field-correction route. The remaining two routes (the note editor and
``/summary``) are added by later slices as siblings on this same
``router`` — none of them re-implement reconciliation: the correction
route calls ``pipeline.recalculate_one``, the same function the headless
batch command calls per invoice.

Every route opens its own short-lived connection via ``get_db_path`` +
``invoice_reconciliation.db.connection.connect``, reads ``request.app.state.db_path``, and
closes it before returning — the web view and the batch command read and
write the same SQLite file, so no route holds a connection open across
requests.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from invoice_reconciliation import pipeline
from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.db.ingest import DEFAULT_SEED_PATH
from invoice_reconciliation.extraction.cache import CacheMissError, ResponseCache
from invoice_reconciliation.money import MoneyFormatError, cents_to_display
from invoice_reconciliation.pipeline import recalculate_one
from invoice_reconciliation.reconciliation import improvement
from invoice_reconciliation.web import provenance

__all__ = ["router"]

router = APIRouter()

# There is no authentication in this product (task brief, Slice 11). Every
# correction is attributed to this fixed placeholder rather than inventing a
# login system to satisfy the NOT NULL constraint on
# ``corrections.changed_by``.
_REVIEWER = "reviewer"

# The two fields whose current_value text holds integer USD cents (e.g.
# "12000" == $120.00), per reconciliation/rules.py's _parse_cents contract —
# NOT a dollar-amount string, so validation here must match that contract
# rather than money.dollars_to_cents (which would reinterpret "12000" as
# $12,000.00 and return 1200000).
_CENTS_FIELDS = frozenset({"unit_cents", "total_cents"})

# Fixed base directory that every invoice image must resolve inside of.
# ``image_path`` in the database is a relative path from the repository
# root (e.g. "tasks/invoices/images/clean.png"), not from
# "tasks/invoices/" — resolving it against the current working directory
# (the repository root the app is run from) and then checking the result
# is still under this base is what stops a stored value like
# "../../../../etc/passwd" from ever being read, no matter what a future
# row in the database might contain.
_IMAGES_BASE_DIR = Path("tasks/invoices/images").resolve()

# The seven extracted field names, in the order the task lists them —
# the same order the detail page shows them in.
FIELD_NAMES: tuple[str, ...] = (
    "invoice_number",
    "supplier_id",
    "po_id",
    "sku",
    "quantity",
    "unit_cents",
    "total_cents",
)

# The five statuses a ``reconciliation_results`` row may carry (db/schema.py
# CHECK constraint). The queue filter does not validate ``?status=``
# against this set — an unmatched value simply yields an empty list from
# ``repository.list_invoices_with_results`` — but the template uses this
# list to render the filter links in a fixed, known order.
STATUSES: tuple[str, ...] = (
    "reconciled",
    "discrepant",
    "duplicate",
    "unresolved",
    "failed",
)


def get_db_path(request: Request) -> str | Path:
    """Read the configured database path from application state.

    A small accessor rather than a literal ``request.app.state.db_path``
    at every call site, so later routes (detail, correction, note,
    summary) read it the same way.
    """
    return request.app.state.db_path


def get_draft_notes_with_model(request: Request) -> bool:
    """Read whether discrepancy-note drafting should attempt a live model
    call, from application state.

    Defaults to ``False`` when the app was built without setting
    ``app.state.draft_notes_with_model`` explicitly (via
    ``getattr`` with a default) — the same safe-by-default shape
    ``create_app``'s own ``draft_notes_with_model`` parameter uses.
    Mirrors ``get_db_path``'s one-accessor-per-call-site shape, and keeps
    this read in one place rather than repeating the attribute name (and
    its default) at every route that drafts a note.
    """
    return bool(getattr(request.app.state, "draft_notes_with_model", False))


def _display_amount(cents: int | None) -> str:
    """Render a nullable cents value for the queue table.

    ``None`` (duplicate, unresolved, failed rows) becomes a dash, never
    the literal text "None". A present value always goes through
    ``money.cents_to_display`` — this function never divides by 100
    itself.
    """
    if cents is None:
        return "–"  # en dash
    return cents_to_display(cents)


def _row_to_view(row) -> dict[str, object]:
    """Shape one joined invoices/reconciliation_results row for the template.

    All money fields pass through ``_display_amount`` (and so through
    ``money.cents_to_display``) here, once, so the template itself never
    touches a cents integer directly.
    """
    return {
        "invoice_id": row["invoice_id"],
        "file_id": row["file_id"],
        "status": row["status"],
        "expected_display": _display_amount(row["expected_cents"]),
        "billed_display": _display_amount(row["billed_cents"]),
        "difference_display": _display_amount(row["difference_cents"]),
        "difference_cents": row["difference_cents"],
    }


@router.get("/invoices", response_class=HTMLResponse)
def list_invoices(request: Request, status: str | None = None) -> HTMLResponse:
    """The invoice queue: every invoice with its status and main figures.

    ``?status=`` narrows the list to an exact status match. An unknown
    status is not an error: ``repository.list_invoices_with_results``
    simply returns no rows, and the template renders an empty table body
    rather than the route raising or returning a 500.
    """
    db_path = get_db_path(request)
    with connect(db_path) as conn:
        rows = repository.list_invoices_with_results(conn, status=status)
        # The summary strip shows the whole batch whatever filter is active,
        # so these two reads take no ``status``. They are the same two
        # repository calls ``/summary`` makes, so the strip and that page
        # cannot disagree.
        status_counts_raw = repository.count_results_by_status(conn)
        recoverable_total_cents = repository.get_recoverable_total_cents(conn)

    invoices = [_row_to_view(row) for row in rows]
    status_counts = {name: status_counts_raw.get(name, 0) for name in STATUSES}

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "invoices": invoices,
            "statuses": STATUSES,
            "selected_status": status,
            "status_counts": status_counts,
            "total_count": sum(status_counts.values()),
            "recoverable_total_display": cents_to_display(recoverable_total_cents),
        },
    )


def _read_cache_entry(*, file_id: str, image_path: Path):
    """Read the saved model-response cache entry for ``file_id``, or ``None``.

    A ``live`` run writes no cache entry unless ``--refresh-cache`` was
    passed, and the image file itself might be missing (a ``failed``
    invoice). Both are ordinary, expected absences here, not errors: the
    provenance citation simply omits the model id, capture time, and image
    hash when there is nothing to cite. This function never raises for
    either case — it is read-only and purely for display.
    """
    if not image_path.is_file():
        return None
    try:
        image_bytes = image_path.read_bytes()
    except OSError:
        return None

    cache = ResponseCache()
    try:
        return cache.get(file_id, image_bytes)
    except CacheMissError:
        return None


def _field_rows(
    original: dict[str, str | None],
    current: dict[str, str | None],
    *,
    corrections_by_field: dict[str, list[sqlite3.Row]],
) -> list[dict[str, object]]:
    """Pair each of the seven field names with its original and current value.

    Both dicts come from ``repository.get_original_fields`` /
    ``get_current_fields``. A field absent from either (the
    ``missing-reference`` fixture's null ``po_id``) renders as an en dash,
    not the string "None" — the same placeholder the queue uses for a
    null amount, kept consistent here for a null text field.

    The Original and Current columns each carry one citation on their
    table header (built once, in ``invoice_detail``, from
    ``original_citation`` / ``provenance.current_column_citation``) — not
    per cell. The one exception kept per row: a field that has actually
    been corrected still carries its own ``row_citation``, naming who
    corrected it, when, and the previous value, since that is a fact
    about this specific row, not the column. An uncorrected row's
    ``row_citation`` is ``None`` and the template renders no icon for it.
    ``corrections_by_field`` maps a field name to all of its ``corrections``
    rows, oldest first; the per-row citation lists the whole chain
    (v0 original, v1, v2, ...), not just the latest change.
    """
    rows = []
    for field_name in FIELD_NAMES:
        original_value = original.get(field_name)
        current_value = current.get(field_name)
        is_corrected = original_value != current_value
        field_corrections = corrections_by_field.get(field_name, [])
        row_citation = None
        if field_corrections:
            row_citation = provenance.correction_chain_citation(
                original_value=original_value, corrections=field_corrections
            )
        rows.append(
            {
                "field_name": field_name,
                "original_display": original_value if original_value is not None else "–",
                "current_display": current_value if current_value is not None else "–",
                "is_corrected": is_corrected,
                "row_citation": row_citation,
            }
        )
    return rows


_NOTE_SOURCE_LABELS = {
    "model": "model-drafted",
    "calculated": "calculated",
    "reviewer": "edited by reviewer",
}


def _note_version_views(versions: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Shape the note's version rows (oldest first) for the history list.

    The last row is the current version. Each carries its provenance
    citation, built by ``provenance.note_version_citation``.
    """
    views = []
    for index, row in enumerate(versions):
        reasons = (
            json.loads(row["rejection_reasons"]) if row["rejection_reasons"] is not None else None
        )
        views.append(
            {
                "version_no": row["version_no"],
                "label": _NOTE_SOURCE_LABELS.get(row["source"], row["source"]),
                "created_at": row["created_at"],
                "text": row["text"],
                "is_current": index == len(versions) - 1,
                "citation": provenance.note_version_citation(
                    version_no=row["version_no"],
                    source=row["source"],
                    created_by=row["created_by"],
                    created_at=row["created_at"],
                    model_id=row["model_id"],
                    attempts=row["attempts"],
                    rejection_reasons=reasons,
                ),
            }
        )
    return views


def _match_view(purchase_order, receipt, *, po_id_present: bool) -> dict[str, object] | None:
    """Shape the matched purchase order and receipt for the template.

    Returns ``None`` when there is no matched purchase order at all (the
    ``unresolved`` case) — the template uses this to render "no matched
    purchase order" rather than a table of blanks. ``po_id_present``
    distinguishes, for the no-match citation, "no reference was printed on
    the invoice" from "a reference was printed but did not match".
    """
    if purchase_order is None:
        return {
            "no_match_citation": provenance.no_match_citation(po_id_present=po_id_present),
        }

    unit_price_display = cents_to_display(purchase_order["unit_cents"])
    po_citation = provenance.matched_po_citation(
        supplier_id=purchase_order["supplier_id"],
        po_id=purchase_order["po_id"],
        seed_path=str(DEFAULT_SEED_PATH),
        quantity=purchase_order["quantity"],
        unit_price_display=unit_price_display,
    )
    receipt_citation = None
    if receipt is not None:
        receipt_citation = provenance.matched_receipt_citation(
            receipt_id=receipt["receipt_id"],
            po_id=purchase_order["po_id"],
            seed_path=str(DEFAULT_SEED_PATH),
            received_quantity=receipt["quantity"],
        )

    return {
        "po_id": purchase_order["po_id"],
        "supplier_id": purchase_order["supplier_id"],
        "sku": purchase_order["sku"],
        "quantity": purchase_order["quantity"],
        "unit_price_display": unit_price_display,
        "receipt_id": receipt["receipt_id"] if receipt is not None else None,
        "receipt_quantity": receipt["quantity"] if receipt is not None else None,
        "po_citation": po_citation,
        "receipt_citation": receipt_citation,
    }


@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int) -> HTMLResponse:
    """The invoice detail page: the evidence behind one reconciliation result.

    Shows the invoice image beside all seven extracted values (each with
    its original and current value), the matched purchase order and
    receipt (if any), and the expected-vs-billed calculation.

    A ``failed`` invoice shows its reason
    (``invoices.extraction_failure_reason``) and no amounts. An
    ``unresolved`` invoice shows all seven fields but no expected amount,
    no difference, and no matched purchase order — never a zero and never
    an estimate standing in for "not computed".

    A nonexistent ``invoice_id`` is a 404, not a 500: this route looks the
    invoice up first and raises before touching any other table.
    """
    db_path = get_db_path(request)
    with connect(db_path) as conn:
        invoice_row = repository.get_invoice(conn, invoice_id=invoice_id)
        if invoice_row is None:
            raise HTTPException(status_code=404, detail="invoice not found")

        original_fields = repository.get_original_fields(conn, invoice_id=invoice_id)
        current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)
        result_row = repository.get_reconciliation_result(conn, invoice_id=invoice_id)

        purchase_order = None
        receipt = None
        if result_row is not None and result_row["matched_po_id"] is not None:
            supplier_id = current_fields.get("supplier_id")
            purchase_order = repository.get_purchase_order(
                conn, supplier_id=supplier_id, po_id=result_row["matched_po_id"]
            )
            receipt = repository.get_receipt_for_po(conn, po_id=result_row["matched_po_id"])

        # Every correction per field, oldest first (list_corrections order),
        # for the full version chain in a corrected field's citation.
        corrections_by_field: dict[str, list[sqlite3.Row]] = {}
        for correction_row in repository.list_corrections(conn, invoice_id=invoice_id):
            corrections_by_field.setdefault(correction_row["field_name"], []).append(
                correction_row
            )

        # Draft the note on first view if this invoice has never been seen
        # as discrepant through the web layer before (e.g. it was only ever
        # reconciled by the headless batch command). This is the one lazy
        # draft point: once a note row exists, every later view of this
        # invoice reads it back here rather than calling
        # _ensure_discrepancy_note again, so a page refresh never bills a
        # second model call. ``use_model`` is read from application state
        # (on by default, since the brief makes a model-drafted note the
        # normal path — see get_draft_notes_with_model; tests pass False).
        if result_row is not None and result_row["status"] == "discrepant":
            existing_note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
            if existing_note is None:
                _ensure_discrepancy_note(
                    conn, invoice_id, use_model=get_draft_notes_with_model(request)
                )
        note_row = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
        note_versions = repository.list_note_versions(conn, invoice_id=invoice_id)

    status = result_row["status"] if result_row is not None else None
    expected_cents = result_row["expected_cents"] if result_row is not None else None
    billed_cents = result_row["billed_cents"] if result_row is not None else None
    difference_cents = result_row["difference_cents"] if result_row is not None else None

    cache_entry = _read_cache_entry(
        file_id=invoice_row["file_id"], image_path=Path(invoice_row["image_path"])
    )
    original_citation = provenance.original_value_citation(
        image_path=invoice_row["image_path"],
        layout=invoice_row["layout"],
        extraction_source=invoice_row["extraction_source"],
        cache_entry=cache_entry,
        image_url=f"/invoices/{invoice_id}/image",
    )
    current_column_citation = provenance.current_column_citation()

    invoice_view = {
        "invoice_id": invoice_row["invoice_id"],
        "file_id": invoice_row["file_id"],
        "layout": invoice_row["layout"],
        "received_at": invoice_row["received_at"],
        "extraction_source": invoice_row["extraction_source"],
        "extraction_failed": bool(invoice_row["extraction_failed"]),
        "failure_reason": invoice_row["extraction_failure_reason"],
    }

    calculation = {
        "status": status,
        "expected_display": _display_amount(expected_cents),
        "billed_display": _display_amount(billed_cents),
        "difference_display": _display_amount(difference_cents),
        "difference_cents": difference_cents,
        # Explicit flags so the template can tell "no amount computed"
        # (unresolved, duplicate, failed) apart from "computed and zero"
        # (reconciled) — the dash placeholder from _display_amount already
        # tells them apart visually, but the template needs this to decide
        # whether to render the calculation section's arithmetic line at
        # all.
        "has_amounts": expected_cents is not None and billed_cents is not None,
    }

    note_view = None
    if note_row is not None:
        stored_rejection_reasons = (
            json.loads(note_row["rejection_reasons"])
            if note_row["rejection_reasons"] is not None
            else None
        )
        note_view = {
            "drafted_text": note_row["drafted_text"],
            "current_text": note_row["current_text"],
            "is_reviewer_edited": bool(note_row["is_reviewer_edited"]),
            "edit_superseded": bool(note_row["edit_superseded"]),
            "drafted_by": note_row["drafted_by"],
            "note_citation": provenance.discrepancy_note_citation(
                drafted_by=note_row["drafted_by"],
                model_id=note_row["model_id"],
                drafted_at=note_row["drafted_at"],
                attempts=note_row["attempts"],
                rejection_reasons=stored_rejection_reasons,
            ),
            "versions": _note_version_views(note_versions),
            # A discrepancy note states figures "from verified findings"
            # (task brief). Once a correction makes the invoice reconcile
            # (or otherwise stops it being discrepant), the stored note
            # describes figures that are no longer true, so it is retired:
            # its history stays readable, but it is not offered as current
            # and cannot be edited. A later correction that makes the
            # invoice discrepant again redrafts it as a new version.
            "is_active": status == "discrepant",
        }

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "invoice": invoice_view,
            "status": status,
            "fields": _field_rows(
                original_fields,
                current_fields,
                corrections_by_field=corrections_by_field,
            ),
            "original_column_citation": original_citation,
            "current_column_citation": current_column_citation,
            "match": _match_view(
                purchase_order, receipt, po_id_present=current_fields.get("po_id") is not None
            ),
            "calculation": calculation,
            "note": note_view,
        },
    )


@router.get("/invoices/{invoice_id}/image")
def invoice_image(request: Request, invoice_id: int) -> FileResponse:
    """Serve the raw invoice image for ``invoice_id``.

    Security: ``image_path`` is user-influenced data (it was read from a
    database row selected by a user-supplied id), never trusted as-is.
    The stored relative path is resolved against the fixed
    ``_IMAGES_BASE_DIR`` and the resolved path is checked to still be
    inside that base directory (``Path.resolve()`` then
    ``is_relative_to``) before anything is read from disk. A path that
    escapes the base — by a stored ``../../etc/passwd``-style value, say —
    gets a 404, the same response as a path that simply does not exist.
    This route never returns file bytes for a path outside the base.

    A nonexistent invoice id, or an invoice whose image file is missing
    from disk, is also a 404 rather than a 500.
    """
    db_path = get_db_path(request)
    with connect(db_path) as conn:
        invoice_row = repository.get_invoice(conn, invoice_id=invoice_id)

    if invoice_row is None:
        raise HTTPException(status_code=404, detail="invoice not found")

    # The stored path is relative to the repository root ("tasks/invoices/
    # images/clean.png"), not to _IMAGES_BASE_DIR itself — so it is
    # resolved from the current working directory (``Path.resolve()`` on
    # a relative path resolves against cwd), then checked against the
    # fixed base. Anchoring the resolve to cwd explicitly (rather than
    # relying on the process's cwd happening to be the repo root) keeps
    # this correct regardless of where the server was started from.
    repo_root = Path.cwd()
    stored_path = Path(invoice_row["image_path"])
    resolved_path = (repo_root / stored_path).resolve()

    if not resolved_path.is_relative_to(_IMAGES_BASE_DIR):
        raise HTTPException(status_code=404, detail="image not found")

    if not resolved_path.is_file():
        raise HTTPException(status_code=404, detail="image not found")

    media_type = mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
    return FileResponse(resolved_path, media_type=media_type)


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601, matching ``pipeline._now_iso``'s format."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_discrepancy_note(
    conn, invoice_id: int, *, use_model: bool = False, force: bool = False
) -> None:
    """Draft the discrepancy note for ``invoice_id``, if it needs one.

    A thin wrapper over ``pipeline.ensure_discrepancy_note`` — the one
    note-drafting decision shared with ``pipeline.run_batch`` (which now
    drafts a note for every invoice that becomes discrepant during the
    batch; see that module). This function stays as the lazy fallback for
    the two cases the batch cannot have already covered:

    - the detail route, for an invoice that became discrepant through a
      web correction after the last batch run (the batch never saw it as
      discrepant, so never drafted a note for it) — called with
      ``force=False``, the default, so an invoice the batch (or an
      earlier view) already drafted a note for is read back, not
      redrafted;
    - the correction route itself, immediately after a correction changes
      the figures — called with ``force=True``, so the one invoice just
      corrected gets a fresh note describing its new figures rather than
      the detail page showing a note that still describes the pre-
      correction amounts. Called synchronously, inside the same POST (see
      that route's docstring for why a correction accepts this short wait
      rather than leaving a stale note or deferring to the next batch).

    A reviewer's edit is still never overwritten, force or not — enforced
    by ``pipeline.ensure_discrepancy_note`` itself, not re-implemented
    here. ``use_model`` and ``force`` are passed straight through
    unchanged.
    """
    pipeline.ensure_discrepancy_note(conn, invoice_id, use_model=use_model, force=force)


def _validate_field_value(field_name: str, raw_value: str) -> str:
    """Check a reviewer-submitted value for one field, and return the text
    to store in ``current_value``.

    Returns the normalised text on success. Raises ``HTTPException(422)``
    with a readable message on an invalid value — nothing is written to the
    database in that case.

    - ``unit_cents`` and ``total_cents`` hold integer USD cents as text
      (``"12000"`` == $120.00), the same contract
      ``reconciliation/rules.py:_parse_cents`` reads back. They are
      validated with that same plain base-10 integer parse, never with
      ``money.dollars_to_cents`` — that function treats its input as a
      dollar amount and would reinterpret ``"12000"`` as $12,000.00.
    - ``quantity`` must be a positive integer.
    - Every other field (``invoice_number``, ``supplier_id``, ``po_id``,
      ``sku``) is free text; only a blank value is rejected, since a
      reviewer correction supplies a specific replacement, not an
      intentional null (unlike extraction, which may legitimately produce
      ``None`` for ``po_id``).
    """
    text = raw_value.strip()
    if not text:
        raise HTTPException(status_code=422, detail=f"{field_name} must not be blank")

    if field_name in _CENTS_FIELDS:
        negative = text.startswith("-")
        unsigned = text[1:] if negative else text
        if not unsigned.isdigit():
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must be an integer number of cents, got {raw_value!r}",
            )
        return text

    if field_name == "quantity":
        if not text.isdigit() or int(text) <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"quantity must be a positive integer, got {raw_value!r}",
            )
        return text

    return text


@router.post("/invoices/{invoice_id}/fields/{field_name}")
def correct_field(
    request: Request,
    invoice_id: int,
    field_name: str,
    value: str = Form(...),
) -> RedirectResponse:
    """Correct one extracted field, then recalculate the invoice.

    This route holds no reconciliation logic: it validates the submitted
    value, persists it, and delegates recomputation entirely to
    ``pipeline.recalculate_one`` — the same function the headless batch
    command calls per invoice.

    Takes ``value`` as an ordinary form field (``application/
    x-www-form-urlencoded``), matching the rest of this server-rendered
    product: ``detail.html``'s per-field correction form posts here
    directly, with no client-side script. On success, redirects (303) back
    to the detail page so the reviewer sees the corrected value and the new
    reconciliation result in one round trip.

    Steps:

    1. Look up the invoice (404 if it does not exist) and the field's
       current value (404 if ``field_name`` is not one of the seven
       extracted fields for this invoice).
    2. Validate the new value (422 if invalid; nothing is written).
    3. In one transaction: write a ``corrections`` audit row, then update
       ``extracted_fields.current_value``. ``original_value`` is never
       touched — it stays whatever extraction first produced.
    4. Still inside that same connection, call
       ``pipeline.recalculate_one`` so the reconciliation result reflects
       the correction before the response is returned.

    A failure between the correction row and the field update cannot
    leave one without the other: both happen inside the single
    ``connect()`` context, which commits once on clean exit and rolls
    back entirely on any exception.
    """
    if field_name not in FIELD_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown field: {field_name}")

    db_path = get_db_path(request)
    with connect(db_path) as conn:
        invoice_row = repository.get_invoice(conn, invoice_id=invoice_id)
        if invoice_row is None:
            raise HTTPException(status_code=404, detail="invoice not found")

        current_fields = repository.get_current_fields(conn, invoice_id=invoice_id)
        if field_name not in current_fields:
            raise HTTPException(status_code=404, detail=f"unknown field: {field_name}")

        value_before = current_fields[field_name]
        value_after = _validate_field_value(field_name, value)

        # Saving the value that is already current is not a correction:
        # writing it would add an audit row for a change that never
        # happened, and would trigger a needless redraft below.
        if value_after == value_before:
            return RedirectResponse(
                url=f"/invoices/{invoice_id}#fields", status_code=303
            )

        note_inputs_before = _note_inputs(conn, invoice_id)

        changed_at = _now_iso()
        repository.insert_correction(
            conn,
            invoice_id=invoice_id,
            field_name=field_name,
            value_before=value_before,
            value_after=value_after,
            changed_by=_REVIEWER,
            changed_at=changed_at,
        )
        repository.update_current_field_value(
            conn,
            invoice_id=invoice_id,
            field_name=field_name,
            current_value=value_after,
        )

        try:
            recalculate_one(conn, invoice_id)
        except MoneyFormatError as exc:
            # recalculate_one already catches this internally and records
            # 'failed' instead of raising — this branch only guards against
            # a future change to that contract so a correction never 500s.
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Redraft the note from the new figures — unless a reviewer already
        # edited it, in which case _ensure_discrepancy_note leaves the edit
        # untouched and marks it edit_superseded instead (see its
        # docstring). force=True: this invoice's own note, if any, was
        # drafted against the pre-correction figures (by an earlier batch
        # run or an earlier view), so it must be redrafted here rather than
        # left describing stale amounts — the one case "drafting is once
        # per invoice" does not apply to. A short wait here (one verified
        # model call, at most) is accepted deliberately: a correction is
        # an explicit, synchronous user action, unlike the batch's own
        # drafting, so the reviewer is already waiting on this request
        # regardless.
        # Redraft only when the correction changed something the note
        # states. A correction to a field the note does not use (the SKU,
        # say) leaves the old note accurate, so it is kept: no new version,
        # and no model call for the reviewer to wait on.
        if _note_inputs(conn, invoice_id) != note_inputs_before:
            _ensure_discrepancy_note(
                conn,
                invoice_id,
                use_model=get_draft_notes_with_model(request),
                force=True,
            )

    # The anchor returns the reviewer to the fields table rather than the
    # top of the page; detail.html restores the exact scroll position too.
    return RedirectResponse(url=f"/invoices/{invoice_id}#fields", status_code=303)


def _note_inputs(conn, invoice_id: int) -> tuple:
    """Everything a discrepancy note states, for one invoice.

    These are the inputs ``prompts.build_note_prompt`` and
    ``notes.draft_note`` draw on: the status, the invoice number, the
    matched purchase order, the expected, billed and difference amounts,
    and the billed unit price and quantity the note's source sentence
    states. If a correction leaves all of them unchanged, the stored note
    still describes the invoice correctly and must not be redrafted.
    """
    result = repository.get_reconciliation_result(conn, invoice_id=invoice_id)
    fields = repository.get_current_fields(conn, invoice_id=invoice_id)
    if result is None:
        return (None, fields.get("invoice_number"))
    return (
        result["status"],
        fields.get("invoice_number"),
        result["matched_po_id"],
        result["expected_cents"],
        result["billed_cents"],
        result["difference_cents"],
        # The note also states where the difference comes from: the billed
        # unit price and quantity (see reconciliation/difference_source.py).
        fields.get("unit_cents"),
        fields.get("quantity"),
    )


@router.post("/invoices/{invoice_id}/note")
def save_note(
    request: Request,
    invoice_id: int,
    text: str = Form(...),
) -> RedirectResponse:
    """Save a reviewer's edit to a discrepant invoice's note.

    This route only ever writes ``discrepancy_notes.current_text`` (by way
    of ``repository.update_discrepancy_note_text``). It never calls
    ``pipeline.recalculate_one`` and never touches ``reconciliation_results``
    or ``extracted_fields`` — a note is commentary on an already-computed
    result, not an input to one, so editing it must not change the
    reconciliation outcome (``technical-considerations.md`` section 5:
    "a reviewer's edit to a draft note does not trigger a recalculation").

    There is deliberately no send action anywhere in this product: this
    route (and every template) only ever drafts and saves text for a human
    to use elsewhere. No "send", "email", or "submit to supplier" route
    exists in this router.

    A blank edit is rejected (422) the same way a blank field correction
    is — an edit supplies replacement text, not an intentional empty note.

    A nonexistent invoice id, or an invoice with no note row yet (never
    discrepant, or discrepant but not yet viewed so no draft exists), is a
    404: there is nothing to attach this edit to.
    """
    db_path = get_db_path(request)
    with connect(db_path) as conn:
        invoice_row = repository.get_invoice(conn, invoice_id=invoice_id)
        if invoice_row is None:
            raise HTTPException(status_code=404, detail="invoice not found")

        existing_note = repository.get_discrepancy_note(conn, invoice_id=invoice_id)
        if existing_note is None:
            raise HTTPException(status_code=404, detail="no note drafted for this invoice")

        # A retired note (the invoice is no longer discrepant) is history
        # only. Editing it would add a version describing figures that are
        # no longer true.
        result_row = repository.get_reconciliation_result(conn, invoice_id=invoice_id)
        if result_row is None or result_row["status"] != "discrepant":
            raise HTTPException(
                status_code=409,
                detail="this invoice is no longer discrepant; its note is retired",
            )

        new_text = text.strip()
        if not new_text:
            raise HTTPException(status_code=422, detail="note text must not be blank")

        repository.update_discrepancy_note_text(
            conn,
            invoice_id=invoice_id,
            current_text=new_text,
            edited_at=_now_iso(),
            changed_by=_REVIEWER,
        )

    # Return to the note, not the top of the page (see the correction route).
    return RedirectResponse(url=f"/invoices/{invoice_id}#note", status_code=303)


# ---------------------------------------------------------------------------
# GET /summary
# ---------------------------------------------------------------------------

def _suggested_improvements(
    request: Request, file_ids_by_issue: dict[str, list[str]], all_file_ids: tuple[str, ...]
) -> dict:
    """The improvements for the summary page, built from issue counts.

    Code ranks the issue types and keeps the top two
    (``improvement.rank_issues``); the model phrases all of them in one
    call (``improvement.draft_improvements``). The drafts are kept on
    ``app.state`` per set of issues and their invoices, so a page load
    calls the model only when the facts change, for example after a
    correction.
    """
    issues = improvement.rank_issues(file_ids_by_issue)
    if not issues:
        return {"issues": [], "empty_text": improvement.NO_ISSUES, "any_recurring": False}

    cache = getattr(request.app.state, "improvement_drafts", None)
    if cache is None:
        cache = request.app.state.improvement_drafts = {}
    use_model = get_draft_notes_with_model(request)
    key = (tuple(issues), use_model)
    if key not in cache:
        cache[key] = improvement.draft_improvements(
            issues, other_file_ids=all_file_ids, use_model=use_model
        )
    items = [
        {
            "label": improvement.ISSUE_LABELS[issue.kind],
            "count": issue.count,
            "recurring": issue.recurring,
            "text": drafted.text,
            "drafted_by": drafted.drafted_by,
            "model_id": drafted.model_id,
        }
        for issue, drafted in zip(issues, cache[key])
    ]
    return {
        "issues": items,
        "empty_text": None,
        "any_recurring": any(issue.recurring for issue in issues),
    }


# Display label for each ``DifferenceSource.kind``, in the order the
# "By source" table lists them. ``unknown`` is for a discrepant invoice
# whose source figures are missing; it is shown, never guessed.
_SOURCE_LABELS = {
    "price": "Unit price",
    "quantity": "Quantity",
    "price_and_quantity": "Unit price and quantity",
    "total_only": "Total only",
    "unknown": "Not known",
}


@router.get("/summary", response_class=HTMLResponse)
def summary(request: Request) -> HTMLResponse:
    """The batch summary: counts per status, the recoverable total, and
    each discrepant invoice's difference.

    Reads only what ``pipeline.run_batch`` / ``recalculate_one`` already
    persisted — no reconciliation logic lives here. Three read-only
    repository calls:

    - ``count_results_by_status`` for the per-status counts (filled in
      with 0 for any of the five known statuses with no rows at all,
      rather than the template needing to handle a missing key).
    - ``get_recoverable_total_cents`` for the sum of positive differences
      on discrepant invoices only (``SUM(difference_cents) WHERE status =
      'discrepant' AND difference_cents > 0``) — this is computed by SQL
      in the repository layer, not by summing Python-side here, so there
      is exactly one place that condition is expressed.
    - ``list_discrepant_results`` for the per-invoice difference listing,
      each amount rendered through ``_display_amount`` /
      ``money.cents_to_display`` like every other money value in this
      layer.

    Duplicates and unresolved are reported as counts only, on this same
    page, never folded into the recoverable total or given a dollar
    figure of any kind — their value is unknown or not owed, not zero.
    """
    db_path = get_db_path(request)
    with connect(db_path) as conn:
        status_counts_raw = repository.count_results_by_status(conn)
        recoverable_total_cents = repository.get_recoverable_total_cents(conn)
        discrepant_rows = repository.list_discrepant_results(conn)
        all_rows = repository.list_invoices_with_results(conn)
        source_kinds = {}
        for row in discrepant_rows:
            source = pipeline.source_of_difference(
                conn,
                current_fields=repository.get_current_fields(conn, invoice_id=row["invoice_id"]),
                matched_po_id=row["matched_po_id"],
            )
            source_kinds[row["invoice_id"]] = source.kind if source is not None else "unknown"

    status_counts = {status: status_counts_raw.get(status, 0) for status in STATUSES}

    differences = [
        {
            "invoice_id": row["invoice_id"],
            "file_id": row["file_id"],
            "invoice_number": row["invoice_number"] or "–",
            "po_id": row["matched_po_id"] or "–",
            "source_label": _SOURCE_LABELS[source_kinds[row["invoice_id"]]],
            "difference_display": _display_amount(row["difference_cents"]),
            "difference_cents": row["difference_cents"],
        }
        for row in discrepant_rows
    ]

    # Group by source: a count, the overcharges and the undercharges, kept
    # apart so an undercharge never offsets an overcharge. Integer cents
    # throughout; each invoice's whole amount stays under its one source.
    by_source = []
    for kind, label in _SOURCE_LABELS.items():
        rows = [r for r in discrepant_rows if source_kinds[r["invoice_id"]] == kind]
        if not rows:
            continue
        diffs = [r["difference_cents"] or 0 for r in rows]
        by_source.append(
            {
                "label": label,
                "count": len(rows),
                "over_display": cents_to_display(sum(d for d in diffs if d > 0)),
                "under_display": cents_to_display(sum(d for d in diffs if d < 0)),
                "has_under": any(d < 0 for d in diffs),
            }
        )

    # Every issue type in the saved results, for the suggested improvements:
    # the source of each discrepant invoice, then duplicate, unresolved
    # (no purchase-order reference) and failed invoices.
    file_ids_by_issue = {
        kind: [r["file_id"] for r in discrepant_rows if source_kinds[r["invoice_id"]] == kind]
        for kind in improvement.ISSUE_ORDER[:4]
    }
    for issue_kind, status in (
        ("duplicate", "duplicate"),
        ("missing_reference", "unresolved"),
        ("failed", "failed"),
    ):
        file_ids_by_issue[issue_kind] = [r["file_id"] for r in all_rows if r["status"] == status]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "summary.html",
        {
            "status_counts": status_counts,
            "statuses": STATUSES,
            "recoverable_total_display": cents_to_display(recoverable_total_cents),
            "differences": differences,
            "by_source": by_source,
            "improvements": _suggested_improvements(
                request, file_ids_by_issue, tuple(r["file_id"] for r in all_rows)
            ),
        },
    )

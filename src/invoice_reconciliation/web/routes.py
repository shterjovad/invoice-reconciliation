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

import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.money import MoneyFormatError, cents_to_display
from invoice_reconciliation.pipeline import recalculate_one

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

    invoices = [_row_to_view(row) for row in rows]

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "invoices": invoices,
            "statuses": STATUSES,
            "selected_status": status,
        },
    )


def _field_rows(original: dict[str, str | None], current: dict[str, str | None]) -> list[dict[str, object]]:
    """Pair each of the seven field names with its original and current value.

    Both dicts come from ``repository.get_original_fields`` /
    ``get_current_fields``. A field absent from either (the
    ``missing-reference`` fixture's null ``po_id``) renders as an en dash,
    not the string "None" — the same placeholder the queue uses for a
    null amount, kept consistent here for a null text field.
    """
    rows = []
    for field_name in FIELD_NAMES:
        original_value = original.get(field_name)
        current_value = current.get(field_name)
        rows.append(
            {
                "field_name": field_name,
                "original_display": original_value if original_value is not None else "–",
                "current_display": current_value if current_value is not None else "–",
                "is_corrected": original_value != current_value,
            }
        )
    return rows


def _match_view(purchase_order, receipt) -> dict[str, object] | None:
    """Shape the matched purchase order and receipt for the template.

    Returns ``None`` when there is no matched purchase order at all (the
    ``unresolved`` case) — the template uses this to render "no matched
    purchase order" rather than a table of blanks.
    """
    if purchase_order is None:
        return None
    return {
        "po_id": purchase_order["po_id"],
        "supplier_id": purchase_order["supplier_id"],
        "sku": purchase_order["sku"],
        "quantity": purchase_order["quantity"],
        "unit_price_display": cents_to_display(purchase_order["unit_cents"]),
        "receipt_id": receipt["receipt_id"] if receipt is not None else None,
        "receipt_quantity": receipt["quantity"] if receipt is not None else None,
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

    status = result_row["status"] if result_row is not None else None
    expected_cents = result_row["expected_cents"] if result_row is not None else None
    billed_cents = result_row["billed_cents"] if result_row is not None else None
    difference_cents = result_row["difference_cents"] if result_row is not None else None

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

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "invoice": invoice_view,
            "status": status,
            "fields": _field_rows(original_fields, current_fields),
            "match": _match_view(purchase_order, receipt),
            "calculation": calculation,
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

    return RedirectResponse(
        url=f"/invoices/{invoice_id}", status_code=303
    )

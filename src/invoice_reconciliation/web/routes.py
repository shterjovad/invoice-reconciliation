"""The six reviewer-UI routes (``technical-considerations.md`` section 2.6).

This slice implements the queue only (``GET /invoices``). The other five
routes are added by later slices as siblings on this same ``router`` —
none of them re-implement reconciliation: a correction route calls
``pipeline.recalculate_one``, the same function the headless batch command
calls per invoice.

Every route opens its own short-lived connection via ``get_db_path`` +
``invoice_reconciliation.db.connection.connect``, reads ``request.app.state.db_path``, and
closes it before returning — the web view and the batch command read and
write the same SQLite file, so no route holds a connection open across
requests.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.money import cents_to_display

__all__ = ["router"]

router = APIRouter()

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

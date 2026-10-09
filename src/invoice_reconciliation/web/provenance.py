"""Provenance citations for the invoice detail page.

Builds the text (and, where one genuinely exists, the link) behind each
value shown on ``detail.html``: an extracted field's original or current
value, the matched purchase order, and the matched receipt. This module
computes; the template only displays.

A citation is a plain ``dict`` with two keys:

- ``lines``: a list of short text lines, joined with newlines for the
  ``title`` attribute and rendered as separate lines in the CSS-only
  tooltip.
- ``link``: ``None``, or a ``{"href": ..., "text": ...}`` dict for the one
  real, resolvable link the citation can offer (today: the invoice image,
  via the existing ``GET /invoices/{id}/image`` route). No other route is
  invented here.

Every citation always has at least one line — there is no code path that
reaches the template with an empty or ``None`` citation, so the template
never renders the literal string "None" or a blank tooltip.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "Citation",
    "original_value_citation",
    "current_column_citation",
    "matched_po_citation",
    "matched_receipt_citation",
    "no_match_citation",
    "discrepancy_note_citation",
    "note_version_citation",
    "correction_chain_citation",
]

Citation = dict[str, object]


def _abbreviate_sha256(digest: str | None) -> str | None:
    """Shorten a SHA-256 hex digest for display, e.g. ``"d86cb325…e2739e99"``.

    ``None`` stays ``None`` — the caller decides what to show (or omit)
    when no hash is available, this function never invents one.
    """
    if digest is None:
        return None
    if len(digest) <= 16:
        return digest
    return f"{digest[:8]}…{digest[-8:]}"


def original_value_citation(
    *,
    image_path: str,
    layout: str,
    extraction_source: str,
    cache_entry: sqlite3.Row | object | None,
    image_url: str,
) -> Citation:
    """Citation for an extracted field's *original* value.

    ``cache_entry`` is the ``extraction.cache.CacheEntry`` read for this
    invoice's ``file_id``, or ``None`` when no cache file exists for it (a
    live run that was never captured, or a run outside this slice's
    fixtures). When it is ``None``, the citation still names the image,
    layout, and extraction source — it simply omits the model id,
    capture time, and image hash rather than showing them as blank or
    "None".
    """
    lines = [f"Extracted from {image_path} (layout {layout})."]

    if extraction_source == "cache":
        lines.append("Replayed from a saved model response (not a live call).")
    elif extraction_source == "live":
        lines.append("Produced by a live model call.")
    else:
        lines.append(f"Extraction source: {extraction_source}.")

    if cache_entry is not None:
        lines.append(f"Model: {cache_entry.model_id}.")
        lines.append(f"Captured: {cache_entry.captured_at}.")
        abbreviated = _abbreviate_sha256(cache_entry.image_sha256)
        if abbreviated is not None:
            lines.append(f"Image SHA-256: {abbreviated} (binds this response to this exact image).")

    return {
        "lines": lines,
        "link": {"href": image_url, "text": "view invoice image"},
    }


def _show(value: object) -> str:
    return "–" if value is None else str(value)


def correction_chain_citation(
    *,
    original_value: str | None,
    corrections: list[sqlite3.Row],
) -> Citation:
    """Every change to a field, oldest first, one line each.

    ``corrections`` is the field's rows from the append-only ``corrections``
    table, oldest first. Version 0 is the extracted original (from
    ``extracted_fields.original_value``); each correction is the next
    version, and the last one is marked ``(current)``. Reads like the
    discrepancy note's version history (``v1 · ...``).
    """
    lines = [f"v0 original (extracted): {_show(original_value)}"]
    last = len(corrections)
    for number, correction in enumerate(corrections, start=1):
        line = (
            f"v{number} {_show(correction['value_before'])} \u2192 "
            f"{_show(correction['value_after'])}, by {correction['changed_by']} "
            f"at {correction['changed_at']}"
        )
        if number == last:
            line += " (current)"
        lines.append(line)
    return {"lines": lines, "link": None}


def current_column_citation() -> Citation:
    """Citation for the *Current* column header.

    States the general rule for every value in the column: identical to
    the Original column unless a reviewer corrected it, and that a
    correction is recorded in the ``corrections`` table with who made it
    and when. This is the one citation shown for the whole column; a row
    that has actually been corrected also carries its own citation (see
    ``correction_chain_citation``), since the previous value and the time of
    that specific change are a fact about that row, not the column.
    """
    lines = [
        "The value reconciliation uses now.",
        "Identical to Original unless a reviewer corrected it.",
        "A correction is recorded in the corrections table, with who "
        "made it and when.",
    ]
    return {"lines": lines, "link": None}


def matched_po_citation(
    *,
    supplier_id: str,
    po_id: str,
    seed_path: str,
    quantity: int,
    unit_price_display: str,
) -> Citation:
    """Citation for the matched purchase order.

    Names the real matching rule (``supplier_id`` AND ``po_id`` together,
    read from ``reconciliation/matcher.py`` — never amount, since two
    seeded purchase orders are identical apart from their ID), the source
    file, and the agreed terms that produced ``expected_cents``.
    """
    lines = [
        f"Matched purchase orders table, row {po_id}.",
        f"Matching rule: supplier_id={supplier_id!r} AND po_id={po_id!r} "
        "(never on amount alone).",
        f"Source: {seed_path}.",
        f"Agreed terms: {quantity} ordered at {unit_price_display} per unit "
        "(this is what produced the expected amount).",
    ]
    return {"lines": lines, "link": None}


def matched_receipt_citation(
    *,
    receipt_id: str,
    po_id: str,
    seed_path: str,
    received_quantity: int,
) -> Citation:
    """Citation for the matched receipt.

    Names the table and row, that it was found via the matched purchase
    order's ``po_id``, the source file, and the received quantity.
    """
    lines = [
        f"Matched receipts table, row {receipt_id}.",
        f"Found via its po_id ({po_id!r}), the same purchase order this "
        "invoice matched.",
        f"Source: {seed_path}.",
        f"Received quantity: {received_quantity}.",
    ]
    return {"lines": lines, "link": None}


def discrepancy_note_citation(
    *,
    drafted_by: str,
    model_id: str | None,
    drafted_at: str | None,
    attempts: int | None,
    rejection_reasons: list[str] | None,
) -> Citation:
    """Citation for the discrepancy note's drafted text: which path
    produced it, and (on the model path) the model and attempt count.

    ``drafted_by`` is ``"model"`` or ``"calculated"`` (see
    ``reconciliation.notes.DraftedNote``). On the model path, names the
    model id and when it drafted; a calculated note states plainly that
    no model was used — never silent about which path a reviewer is
    reading. ``rejection_reasons`` (present only when at least one attempt
    was rejected before success, or when every attempt failed and the
    note fell back to calculated) is listed so a reviewer can see what the
    model got wrong, not just that it was retried.
    """
    if drafted_by == "model":
        lines = [f"Drafted by the model ({model_id})."]
        if drafted_at is not None:
            lines.append(f"Drafted: {drafted_at}.")
        if attempts is not None and attempts > 1:
            lines.append(f"Accepted on attempt {attempts} of up to 3.")
    else:
        lines = ["Drafted by the calculated (non-model) fallback — no model was used."]

    if rejection_reasons:
        lines.append("Rejected attempt(s): " + "; ".join(rejection_reasons))

    return {"lines": lines, "link": None}


def note_version_citation(
    *,
    version_no: int,
    source: str,
    created_by: str,
    created_at: str,
    model_id: str | None,
    attempts: int | None,
    rejection_reasons: list[str] | None,
) -> Citation:
    """Citation for one row of the discrepancy note's version history.

    A ``model`` or ``calculated`` version reuses ``discrepancy_note_citation``
    (model id, attempts, rejection reasons). A ``reviewer`` version states
    who wrote it and when.
    """
    if source == "reviewer":
        return {
            "lines": [f"Edited by {created_by} on {created_at}."],
            "link": None,
        }
    return discrepancy_note_citation(
        drafted_by=source,
        model_id=model_id,
        drafted_at=created_at,
        attempts=attempts,
        rejection_reasons=rejection_reasons,
    )


def no_match_citation(*, po_id_present: bool) -> Citation:
    """Citation for the ``unresolved`` case: explain why there is no match.

    The most important citation on the page — it shows the system
    declining to invent a match rather than guessing a nearest record.
    ``po_id_present`` distinguishes "no reference was printed on the
    invoice at all" from "a reference was printed but it did not match
    any purchase order" (conflicting supplier, or an unknown po_id) —
    both are classified ``unresolved``, but the explanation differs.
    """
    if po_id_present:
        lines = [
            "No purchase order matched this invoice's printed reference.",
            "The matching rule requires supplier_id AND po_id to both "
            "agree with a purchase orders row; no row satisfied both, so "
            "nothing was matched.",
            "No nearest or similar record was guessed.",
        ]
    else:
        lines = [
            "No purchase-order reference was printed on this invoice.",
            "With no po_id to match on, nothing was matched.",
            "No nearest or similar record was guessed.",
        ]
    return {"lines": lines, "link": None}

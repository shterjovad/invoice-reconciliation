"""Suggested process improvements, built from issue counts in the database.

The brief asks to "Explain one recurring issue or useful process
improvement supported by the results; do not treat a discrepancy as proof
of wrongdoing."

One discrepancy is a case for a reviewer, not a process problem. So the
summary looks at every issue type in the saved results and counts them:

- from discrepant invoices, where the difference comes from
  (``DifferenceSource.kind``): unit price, quantity, both, or total only;
- duplicate invoices;
- invoices with no purchase-order reference (``unresolved``);
- invoices extraction could not read (``failed``).

Two steps, in the same shape as ``notes.py``:

1. ``rank_issues`` — code, not the model, ranks the issue types by count
   and keeps the top two (``TOP_ISSUES``). An issue is *recurring* only
   when 2 or more invoices have it; a count of 1 is a single case, and the
   page labels it so. On a tie, the order of ``ISSUE_ORDER`` decides.
2. ``draft_improvements`` — in one model call for all the issues, the
   model writes one plain sentence per issue on which invoices have it. Code chooses the second sentence,
   the conclusion (``CONCLUSIONS``), and the model must copy it word for
   word. ``verify_improvement`` checks each text on its own; a retry call
   asks again only for the rejected issues, with the reason, up to three
   calls in all. An issue still rejected gets the prepared sentence
   (``prepared_improvement``). The blame-word check keeps
   the brief's rule: "do not treat a discrepancy as proof of wrongdoing."

The issue types, the rule of 2 or more, the top two and the tie order are
presentation choices, not domain rules: they change no status and no
amount. The README records them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from invoice_reconciliation.prompts import (
    IMPROVEMENT_SCHEMA,
    IMPROVEMENT_TOOL_NAME,
    build_improvement_prompt,
)
from invoice_reconciliation.reconciliation.notes import _ACCUSATORY_WORDS

__all__ = [
    "CONCLUSIONS",
    "ISSUE_LABELS",
    "ISSUE_ORDER",
    "NO_ISSUES",
    "TOP_ISSUES",
    "DraftedImprovement",
    "Issue",
    "draft_improvements",
    "prepared_improvement",
    "rank_issues",
    "verify_improvement",
]

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
_ANTHROPIC_VERSION = "bedrock-2023-05-31"

TOP_ISSUES = 2

# Every issue type, in tie order. The first four are the ``DifferenceSource``
# kinds of discrepant invoices; the last three come from result statuses.
ISSUE_ORDER = (
    "price",
    "quantity",
    "price_and_quantity",
    "total_only",
    "duplicate",
    "missing_reference",
    "failed",
)

ISSUE_LABELS = {
    "price": "Unit price differs",
    "quantity": "Quantity differs",
    "price_and_quantity": "Unit price and quantity differ",
    "total_only": "Total differs",
    "duplicate": "Duplicate invoice",
    "missing_reference": "Missing purchase-order reference",
    "failed": "Invoice could not be read",
}

# The conclusion for each issue type. Code chooses it; the model copies it.
CONCLUSIONS = {
    "price": "Recheck unit prices against the purchase order before shipping.",
    "quantity": "Recheck quantities against the purchase order before shipping.",
    "price_and_quantity": (
        "Recheck unit prices and quantities against the purchase order before shipping."
    ),
    "total_only": "Recheck invoice totals against the purchase order before shipping.",
    "duplicate": "Check each invoice number against invoices already received before approval.",
    "missing_reference": "Ask for a purchase-order reference on every invoice.",
    "failed": "Check the scan quality of each invoice when it arrives.",
}

# What the invoices with each issue did, in plain words (plural form). The
# prepared sentence and the prompt both use it.
_WHAT_HAPPENED = {
    "price": "bill a unit price that differs from the purchase order",
    "quantity": "bill a quantity that differs from the quantity ordered or received",
    "price_and_quantity": "bill a unit price and a quantity that differ from the purchase order",
    "total_only": "bill a total that does not equal the billed quantity times the billed unit price",
    "duplicate": "repeat the supplier and invoice number of an invoice already received",
    "missing_reference": "carry no purchase-order reference, so they could not be matched",
    "failed": "could not be read by extraction",
}

# The same, for one invoice.
_WHAT_HAPPENED_ONE = {
    "price": "bills a unit price that differs from the purchase order",
    "quantity": "bills a quantity that differs from the quantity ordered or received",
    "price_and_quantity": "bills a unit price and a quantity that differ from the purchase order",
    "total_only": "bills a total that does not equal the billed quantity times the billed unit price",
    "duplicate": "repeats the supplier and invoice number of an invoice already received",
    "missing_reference": "carries no purchase-order reference, so it could not be matched",
    "failed": "could not be read by extraction",
}

# A word the draft must contain, so it describes the right issue.
_ISSUE_WORD = {
    "price": "price",
    "quantity": "quantit",
    "price_and_quantity": "price",
    "total_only": "total",
    "duplicate": "invoice number",
    "missing_reference": "reference",
    "failed": "read",
}

# Words a single case must not use.
_RECURRING_WORDS = ("recur", "pattern")

NO_ISSUES = "No issues in this batch, so no improvement is suggested."


@dataclass(frozen=True, slots=True)
class Issue:
    """One issue type and the invoices that have it."""

    kind: str
    file_ids: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.file_ids)

    @property
    def recurring(self) -> bool:
        return self.count >= 2


@dataclass(frozen=True, slots=True)
class DraftedImprovement:
    """The improvement text and how it was produced."""

    text: str
    drafted_by: str  # "model" or "calculated"
    model_id: str | None
    attempts: int


def rank_issues(file_ids_by_issue: dict[str, list[str]]) -> list[Issue]:
    """The top issue types by count; ties follow ``ISSUE_ORDER``."""
    issues = [
        Issue(kind=kind, file_ids=tuple(file_ids_by_issue[kind]))
        for kind in ISSUE_ORDER
        if file_ids_by_issue.get(kind)
    ]
    issues.sort(key=lambda issue: -issue.count)  # stable: keeps ISSUE_ORDER on ties
    return issues[:TOP_ISSUES]


def prepared_improvement(issue: Issue) -> str:
    """The calculated fallback: the same two sentences the model is asked for."""
    if issue.recurring:
        first = f"{issue.count} invoices ({', '.join(issue.file_ids)}) {_WHAT_HAPPENED[issue.kind]}."
    else:
        first = f"{issue.file_ids[0]} {_WHAT_HAPPENED_ONE[issue.kind]}."
    return f"{first} {CONCLUSIONS[issue.kind]}"


def verify_improvement(
    text: str, issue: Issue, *, other_file_ids: tuple[str, ...] = ()
) -> str | None:
    """Return why a model draft is rejected, or ``None`` if it passes.

    Facts only, as ``notes.verify_note`` does: no cause or blame wording,
    no dollar figure (the model was given none), every invoice with the
    issue named and no other invoice named, the issue described, the
    conclusion copied word for word, and no claim of recurrence for a
    single case.
    """
    lowered = text.lower()
    for word in _ACCUSATORY_WORDS:
        if word in lowered:
            return f"used accusatory wording {word!r}, which is never permitted"
    if "$" in text:
        return "contains a dollar figure, but none was supplied"
    for file_id in issue.file_ids:
        if file_id not in text:
            return f"did not name the invoice {file_id!r}"
    for file_id in other_file_ids:
        if file_id not in issue.file_ids and file_id in text:
            return f"named the invoice {file_id!r}, which does not have this issue"
    # Invoice names such as "wrong-price" must not satisfy the next checks.
    prose = lowered
    for file_id in (*issue.file_ids, *other_file_ids):
        prose = prose.replace(file_id.lower(), "")
    if _ISSUE_WORD[issue.kind] not in prose:
        return f"did not describe the issue ({ISSUE_LABELS[issue.kind].lower()})"
    if CONCLUSIONS[issue.kind] not in text:
        return f"did not end with the conclusion {CONCLUSIONS[issue.kind]!r}, exactly as given"
    if not issue.recurring:
        for word in _RECURRING_WORDS:
            if word in prose:
                return f"called the issue {word!r}, but it occurs only once"
    return None


def _extract_texts(response_body: dict) -> dict[str, str]:
    """Map each returned issue key to its text."""
    for block in response_body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            entries = (block.get("input") or {}).get("improvements")
            if not isinstance(entries, list):
                raise ValueError("tool_use block has no 'improvements' list")
            return {
                entry["issue"]: entry["text"].strip()
                for entry in entries
                if isinstance(entry, dict)
                and isinstance(entry.get("issue"), str)
                and isinstance(entry.get("text"), str)
            }
    raise ValueError("response has no tool_use content block")


def _prompt_entry(issue: Issue) -> dict:
    return {
        "key": issue.kind,
        "file_ids": list(issue.file_ids),
        "what_happened": (_WHAT_HAPPENED if issue.recurring else _WHAT_HAPPENED_ONE)[issue.kind],
        "recurring": issue.recurring,
        "conclusion": CONCLUSIONS[issue.kind],
    }


def draft_improvements(
    issues: list[Issue],
    *,
    other_file_ids: tuple[str, ...] = (),
    use_model: bool = True,
) -> list[DraftedImprovement]:
    """Draft every issue's improvement in one model call.

    Each returned text is checked on its own with ``verify_improvement``. A
    text that passes is kept. A retry call (up to three calls in all) asks
    again only for the issues still rejected, each with its reason, so a
    good text is never drafted twice. An issue still rejected after the
    last call gets its prepared sentence, marked ``calculated``.

    Never raises: no credentials, a network error or a malformed response
    all fall back to the prepared sentences.
    """
    by_kind = {issue.kind: issue for issue in issues}
    accepted: dict[str, DraftedImprovement] = {}

    def result() -> list[DraftedImprovement]:
        return [
            accepted.get(issue.kind)
            or DraftedImprovement(
                text=prepared_improvement(issue),
                drafted_by="calculated",
                model_id=None,
                attempts=0 if not use_model else _MAX_ATTEMPTS,
            )
            for issue in issues
        ]

    if not use_model or not issues:
        return result()

    try:
        from invoice_reconciliation.config import ModelConfig
        from invoice_reconciliation.extraction.client import build_client

        client = build_client(ModelConfig(model_id=_MODEL_ID))
    except Exception as exc:  # noqa: BLE001 - any client failure falls back
        logger.warning("could not build the Bedrock client for the improvements (%s)", exc)
        return result()

    rejection_reasons: dict[str, str] = {}
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        pending = [issue for issue in issues if issue.kind not in accepted]
        if not pending:
            break
        body = {
            "anthropic_version": _ANTHROPIC_VERSION,
            "max_tokens": 300 * len(pending),
            "temperature": 0.0,
            "tools": [
                {
                    "name": IMPROVEMENT_TOOL_NAME,
                    "description": "Record one process improvement per issue.",
                    "input_schema": IMPROVEMENT_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": IMPROVEMENT_TOOL_NAME},
            "messages": [
                {
                    "role": "user",
                    "content": build_improvement_prompt(
                        issues=[_prompt_entry(issue) for issue in pending],
                        rejection_reasons=rejection_reasons,
                    ),
                }
            ],
        }
        try:
            raw = client.invoke_model(modelId=_MODEL_ID, body=json.dumps(body))
            texts = _extract_texts(json.loads(raw["body"].read()))
        except Exception as exc:  # noqa: BLE001 - one failed call
            logger.warning("improvements call %d/%d failed: %s", attempt, _MAX_ATTEMPTS, exc)
            continue

        rejection_reasons = {}
        for issue in pending:
            text = texts.get(issue.kind)
            reason = (
                "returned no text for this issue"
                if text is None
                else verify_improvement(text, by_kind[issue.kind], other_file_ids=other_file_ids)
            )
            if reason is None:
                accepted[issue.kind] = DraftedImprovement(
                    text=text, drafted_by="model", model_id=_MODEL_ID, attempts=attempt
                )
            else:
                rejection_reasons[issue.kind] = reason
                logger.warning(
                    "improvement for %r rejected on call %d/%d: %s",
                    issue.kind,
                    attempt,
                    _MAX_ATTEMPTS,
                    reason,
                )

    return result()

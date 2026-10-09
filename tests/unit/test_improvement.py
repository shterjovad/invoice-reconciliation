"""Suggested process improvements: code counts every issue type in the
saved results and ranks them, the model phrases each of the top two, and a
checked fallback covers every model failure.

No test here reaches AWS: the model client is a fake patched in at
``invoice_reconciliation.extraction.client.build_client``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from invoice_reconciliation.reconciliation.improvement import (
    CONCLUSIONS,
    Issue,
    draft_improvements,
    prepared_improvement,
    rank_issues,
    verify_improvement,
)

_CLIENT_PATH = "invoice_reconciliation.extraction.client.build_client"
_PRICE = Issue(kind="price", file_ids=("wrong-price", "undercharge"))
_DUPLICATE = Issue(kind="duplicate", file_ids=("duplicate",))
_ALL = ("clean", "wrong-price", "duplicate", "missing-reference", "quantity-overbill", "undercharge")

# The issue counts of the fresh seed batch.
_SEED_ISSUES = {
    "price": ["wrong-price", "undercharge"],
    "quantity": ["quantity-overbill"],
    "duplicate": ["duplicate"],
    "missing_reference": ["missing-reference"],
}


def _fake_client(responses: list[dict[str, str]]) -> MagicMock:
    """A fake Bedrock client: the Nth call returns the Nth ``{issue: text}`` map."""

    def body(texts: dict[str, str]) -> MagicMock:
        b = MagicMock()
        b.read.return_value = json.dumps(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "input": {
                            "improvements": [
                                {"issue": key, "text": text} for key, text in texts.items()
                            ]
                        },
                    }
                ]
            }
        ).encode("utf-8")
        return b

    client = MagicMock()
    client.invoke_model.side_effect = [{"body": body(r)} for r in responses]
    return client


def _prompt_of(client: MagicMock, call: int) -> str:
    return json.loads(client.invoke_model.call_args_list[call].kwargs["body"])["messages"][0]["content"]


_QUANTITY = Issue(kind="quantity", file_ids=("quantity-overbill",))
_PRICE_TEXT = (
    "Invoices wrong-price and undercharge bill a unit price that differs from the "
    "purchase order. " + CONCLUSIONS["price"]
)
_QUANTITY_TEXT = (
    "Invoice quantity-overbill bills a quantity that differs from the quantity ordered "
    "or received. " + CONCLUSIONS["quantity"]
)


# --- ranking issue types ---------------------------------------------------


def test_the_seed_batch_ranks_unit_price_first_as_the_only_recurring_issue() -> None:
    """Unit price has 2 invoices; quantity, duplicate and missing reference
    have 1 each, so the tie order puts quantity second, as a single case."""
    top = rank_issues(_SEED_ISSUES)
    assert [issue.kind for issue in top] == ["price", "quantity"]
    assert [issue.recurring for issue in top] == [True, False]


def test_duplicates_and_missing_references_count_as_issues() -> None:
    top = rank_issues(
        {"duplicate": ["d1", "d2", "d3"], "missing_reference": ["m1", "m2"], "price": ["p1"]}
    )
    assert [(issue.kind, issue.count) for issue in top] == [
        ("duplicate", 3),
        ("missing_reference", 2),
    ]


def test_only_the_top_two_are_kept() -> None:
    assert len(rank_issues(_SEED_ISSUES)) == 2


def test_no_issue_gives_an_empty_ranking() -> None:
    assert rank_issues({}) == []
    assert rank_issues({"price": [], "duplicate": []}) == []


# --- the prepared sentence and the verifier --------------------------------


def test_the_prepared_sentences_name_the_invoices_and_end_with_the_conclusion() -> None:
    assert prepared_improvement(_PRICE) == (
        "2 invoices (wrong-price, undercharge) bill a unit price that differs from the "
        "purchase order. Recheck unit prices against the purchase order before shipping."
    )
    assert prepared_improvement(_DUPLICATE) == (
        "duplicate repeats the supplier and invoice number of an invoice already received. "
        "Check each invoice number against invoices already received before approval."
    )


def test_every_prepared_sentence_passes_its_own_check() -> None:
    for kind in CONCLUSIONS:
        for file_ids in (("inv-a",), ("inv-a", "inv-b")):
            issue = Issue(kind=kind, file_ids=file_ids)
            assert verify_improvement(prepared_improvement(issue), issue) is None, kind


def test_the_verifier_rejects_blame_figures_missing_and_extra_invoices() -> None:
    good = prepared_improvement(_PRICE)
    assert verify_improvement(good, _PRICE, other_file_ids=_ALL) is None
    assert "accusatory" in verify_improvement(good + " An overcharge.", _PRICE)
    assert "dollar" in verify_improvement(good + " $20.00.", _PRICE)
    assert "undercharge" in verify_improvement(good.replace(", undercharge", ""), _PRICE)
    assert "quantity-overbill" in verify_improvement(
        good + " See quantity-overbill.", _PRICE, other_file_ids=_ALL
    )


def test_an_invoice_name_does_not_count_as_describing_the_issue() -> None:
    """ "wrong-price" contains "price", but the prose must say it too."""
    text = "wrong-price and undercharge differ. " + CONCLUSIONS["price"].replace("unit prices", "them")
    assert "did not describe" in verify_improvement(text, _PRICE)


def test_the_verifier_requires_the_conclusion_word_for_word() -> None:
    text = (
        "wrong-price and undercharge bill a unit price that differs. "
        "The business can compare each line item against purchase order terms."
    )
    assert "conclusion" in verify_improvement(text, _PRICE)


def test_a_single_case_must_not_be_called_recurring() -> None:
    text = (
        "duplicate is a recurring invoice number problem. "
        + CONCLUSIONS["duplicate"]
    )
    assert "occurs only once" in verify_improvement(text, _DUPLICATE)


# --- drafting with the model: one call for every issue -------------------


def test_one_call_drafts_every_issue() -> None:
    client = _fake_client([{"price": _PRICE_TEXT, "quantity": _QUANTITY_TEXT}])
    with patch(_CLIENT_PATH, return_value=client):
        drafted = draft_improvements([_PRICE, _QUANTITY], other_file_ids=_ALL)

    assert client.invoke_model.call_count == 1
    assert [d.text for d in drafted] == [_PRICE_TEXT, _QUANTITY_TEXT]
    assert {d.drafted_by for d in drafted} == {"model"}
    prompt = _prompt_of(client, 0)
    assert "Issue key: price" in prompt and "Issue key: quantity" in prompt


def test_a_retry_asks_again_only_for_the_rejected_issue_and_keeps_the_good_one() -> None:
    blamed = "Invoice quantity-overbill shows a quantity error. " + CONCLUSIONS["quantity"]
    client = _fake_client(
        [{"price": _PRICE_TEXT, "quantity": blamed}, {"quantity": _QUANTITY_TEXT}]
    )
    with patch(_CLIENT_PATH, return_value=client):
        drafted = draft_improvements([_PRICE, _QUANTITY], other_file_ids=_ALL)

    assert client.invoke_model.call_count == 2
    retry = _prompt_of(client, 1)
    assert "Issue key: quantity" in retry and "Issue key: price" not in retry
    assert "rejected" in retry
    assert [d.text for d in drafted] == [_PRICE_TEXT, _QUANTITY_TEXT]
    assert [d.attempts for d in drafted] == [1, 2]


def test_an_issue_still_rejected_after_three_calls_gets_its_prepared_sentence() -> None:
    blamed = "Invoice quantity-overbill shows a quantity error."
    client = _fake_client(
        [{"price": _PRICE_TEXT, "quantity": blamed}, {"quantity": blamed}, {"quantity": blamed}]
    )
    with patch(_CLIENT_PATH, return_value=client):
        drafted = draft_improvements([_PRICE, _QUANTITY], other_file_ids=_ALL)

    assert client.invoke_model.call_count == 3
    assert drafted[0].drafted_by == "model"
    assert drafted[1].drafted_by == "calculated"
    assert drafted[1].text == prepared_improvement(_QUANTITY)


def test_a_missing_entry_is_retried() -> None:
    client = _fake_client([{"price": _PRICE_TEXT}, {"quantity": _QUANTITY_TEXT}])
    with patch(_CLIENT_PATH, return_value=client):
        drafted = draft_improvements([_PRICE, _QUANTITY], other_file_ids=_ALL)
    assert "returned no text" in _prompt_of(client, 1)
    assert {d.drafted_by for d in drafted} == {"model"}


def test_no_credentials_gives_the_prepared_sentences_and_never_raises() -> None:
    with patch(_CLIENT_PATH, side_effect=RuntimeError("no credentials")):
        drafted = draft_improvements([_PRICE, _QUANTITY])
    assert [d.text for d in drafted] == [prepared_improvement(_PRICE), prepared_improvement(_QUANTITY)]
    assert {d.drafted_by for d in drafted} == {"calculated"}


def test_use_model_false_never_builds_a_client() -> None:
    with patch(_CLIENT_PATH) as build_client:
        drafted = draft_improvements([_PRICE], use_model=False)
    build_client.assert_not_called()
    assert drafted[0].drafted_by == "calculated"

# LLM usage note

This note states which tools and models built this project, which parts of
the code they generated, one instruction given to an agent, and the
corrections that followed a check.

## Tools and models

**Development.** Claude Code CLI as the orchestrating agent. It ran Claude
Opus 5 for most of the build and Claude Opus 5.5 from 9 October 2026. Four
custom subagents handled specialist work:

- `bedrock-extraction`: the Bedrock calls, the prompts, field parsing, and
  the response cache.
- `python-backend`: FastAPI routes and templates, the reconciliation rules
  engine, and the money conversion module.
- `sqlite-database`: the schema and the data-access layer.
- `testing-expert`: verification runs and the feature-wide acceptance
  tests.

Their definitions are in `.claude/agents/`.

**Application (runtime).** The app calls
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` on AWS Bedrock, through the
`bedrock-runtime` service in `us-east-1`, at temperature 0.0. It has two
jobs:

1. **Extraction.** It reads each invoice image and returns the seven
   fields. The parser validates the structure before anything is stored.
2. **Discrepancy notes.** It drafts the note for each discrepant invoice.
   The brief asks for this: "Use the model to draft a short discrepancy
   note from verified findings and source references."

The model never supplies a figure. Every amount comes from the rules
engine, in integer cents. Matching, arithmetic and classification run in
plain Python with no model involved.

Every prompt and response schema is in one file,
`src/invoice_reconciliation/prompts.py`, with comments recording what each
prompt must never do and what was learned from failures.

## Live calls and replayed calls

The brief asks to "Label cached responses and simulated failures so the
reviewer can distinguish them from new model calls."

- **Extraction** can run live (`--extract`) or from saved responses
  (`--from-cache`). The six saved responses in
  `tests/fixtures/bedrock_responses/` came from real calls. Each records
  the model ID, the capture time and the SHA-256 of the image it belongs
  to. Each invoice row records `extraction_source` (`live` or `cache`),
  and the detail page states it.
- **Notes** are always live calls. Each stored version records
  `drafted_by` (`model` or `calculated`), the model ID, the number of
  attempts, and the reason each rejected attempt failed. The detail page
  shows this for every version.

Measured on the same batch: with AWS unreachable it took 1 s and gave
three `calculated` notes. With AWS reachable it took 12 s and gave three
`model` notes. The difference is the three live calls.

## Generated components

Agents generated most of the source tree: the FastAPI routes and
templates, the SQLite schema and repository layer, the reconciliation
rules engine, the money conversion module, the Bedrock client and cache,
the note drafting with its verification, the note version history, the
provenance citations, and the test suite. The suite has 225 tests: 122
unit, 80 integration and 23 acceptance.

Domain rules came from `tasks/invoices/domain.md`, not from agent
invention. Unclear points are recorded in the README's "Settled
ambiguities" section. One of them is still open: whether a discrepancy is
only about the billed total.

## One representative instruction

Instruction given to the `python-backend` agent, Slice 11 ("Correct a value
and watch the result change"):

> Validate a reviewer's corrected money value by running it through
> `money.dollars_to_cents` before writing it to the database.

This instruction was wrong, and the agent said so instead of following it.

## Corrections that followed a check

### 1. A wrong instruction, refused by the agent

The agent read how `total_cents` and `unit_cents` are actually stored: as
integer USD cents held in text form, where `"12000"` means $120.00. That is
the same contract `reconciliation/rules.py` reads back (`_parse_cents`).
`money.dollars_to_cents`, by contrast, treats its input as a dollar amount.
Passed `"12000"`, it returns `1200000`, which is wrong by a factor of 100.

The agent declined the instruction and validated the correction with a
plain base-10 integer parse instead. It recorded the reason in
`src/invoice_reconciliation/web/routes.py`:

```
- ``unit_cents`` and ``total_cents`` hold integer USD cents as text
  (``"12000"`` == $120.00), the same contract
  ``reconciliation/rules.py:_parse_cents`` reads back. They are
  validated with that same plain base-10 integer parse, never with
  ``money.dollars_to_cents`` — that function treats its input as a
  dollar amount and would reinterpret ``"12000"`` as $12,000.00.
```

I checked this myself before accepting it. I read `_parse_cents` in
`rules.py` and confirmed it treats `total_cents` as a plain integer count
of cents. I also ran `dollars_to_cents("12000")` and confirmed it returns
`1200000`, not `12000`.

### 2. A recorded fact, overturned by testing

A stored note of mine recorded `AnthropicBedrockMantle` as the verified
Bedrock client class for this environment. An agent tried it, got a `404`
on every model ID it tried on this account, and stopped instead of
silently switching clients. I reproduced the `404`, tried five model IDs
(all `404`), and confirmed that classic `bedrock-runtime` returns a real
completion with the same credentials, region and model ID.
`AnthropicBedrockMantle` targets a separate AWS service
(`bedrock-mantle.us-east-1.api.aws`) with its own entitlement. The note
and the technical specification were both corrected, with the reason
stated in each.

### 3. Notes said to be model-drafted were templates

The brief says: "Use the model to draft a short discrepancy note from
verified findings and source references." The compliance audit marked
this row as a pass, because a note existed and it stated no cause.

The owner asked whether the model really drafted the notes. A check of
`reconciliation/notes.py` showed it did not. The only import was the
money formatter. The note was a fixed sentence built with f-strings, with
the computed values inserted:

```
f"Invoice {invoice_number} (matched to purchase order {po_id}) "
f"bills ${billed_display} against an agreed ${expected_display} "
f"for {quantity} units at ${unit_display} each. "
```

The audit had checked that a note existed, not how it was produced. The
model now drafts the note, a verifier checks every figure before it is
stored, and the fixed sentence stays only as the fallback when the model
is unavailable or every attempt is rejected. Each stored note records
which path produced it.

### 4. A self-contradicting prompt, found by measuring retries

Every model-drafted note is checked before it is stored. `verify_note`
rejects the whole draft if it contains any dollar figure the rules engine
did not compute, names the wrong purchase order, gets the direction of
the difference wrong, or uses cause or blame words. After a rejection the
model retries, up to three times, and is told why.

On the first live run, the check rejected a real draft. The note for
INV-4 named "$20.00", the unit price, which was not one of the three
computed totals. The retry passed. The check worked, but the retry rate
pointed at the prompt. The prompt listed the unit price and then said to
use only the listed figures except the unit price. The model's use of it
was reasonable; the instruction contradicted itself.

The fix removed the unit price from the prompt and added a worked
example. Measured afterwards: 12 live drafts, all accepted on the first
attempt. Later, when the note was extended to state where a difference
comes from, the unit price had to return. That time the prompt and the
verifier take their permitted figures from one function,
`reconciliation/difference_source.py`, so they cannot contradict each
other again. All three notes again passed on the first attempt.

### 5. My own wrong diagnosis, corrected by a browser check

A tooltip on the detail page showed its content late and outside its
popup. I first explained this as selected text, then as a slow native
`title` tooltip. The second was a real problem and I fixed it, but it was
not the main one. Opening the page in a real browser and measuring the
built page showed the cause in a few minutes: the popup used a list
(`<ul>`) inside a paragraph (`<p>`). HTML does not allow that, so the
browser moved the list out of the popup. The template looked correct,
which is why reading the source never found it. A test now fails if list
markup returns to the popup.

### 6. An ambiguity found by testing a correction

The owner tested a correction on INV-5, which bills 5 units at $18.00
with a total of $90.00. They changed only the total, to $100.00, and left
the unit price at $18.00. The invoice then showed as `reconciled`, and its
note was retired.

The code did what the brief's rule says. The brief defines the
discrepancy by the total ("Billed amount minus expected amount is the
discrepancy") and names only one separate comparison, for quantity
("Compare billed quantity with both ordered and received quantity").
The rules engine never reads the invoice's own unit price. But the
invoice now contradicts itself: 5 × $18.00 is $90.00, not $100.00.
Editing only the total can therefore make any invoice reconcile.

The brief does not settle whether the unit price, or the agreement
between the invoice's own fields, should count. It also says "Do not
silently add domain rules." So the code was not changed. The question is
recorded as open in the README's "Settled ambiguities" section (entry 7),
with the options considered.

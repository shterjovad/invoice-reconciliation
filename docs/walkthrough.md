# Walkthrough

A written presentation of the approach and the findings. Read in three to
five minutes. For the live demo steps, see `docs/demo-script.md`. For the
system diagrams, see `docs/architecture.md`.

## What the system does

The system reads invoice images, matches each one against a purchase order
and a receipt, and classifies it as reconciled, discrepant, duplicate, or
unresolved. A reviewer opens a web page, checks the evidence behind a
result, corrects a misread value if needed, and watches the result
recalculate.

One command proves the whole flow needs no AWS access:

```
uv run python -m invoice_reconciliation.cli --reset-db --from-cache --seed-check
```

This replays six invoices from saved Bedrock responses, reconciles each
one, and checks the result against hand-calculated answers. All six match.
Exit code 0.

## Design decisions that mattered

**Integer cents everywhere, one converter.** Every money value in the
system is a whole integer number of USD cents, end to end. One function,
`money.dollars_to_cents`, converts a dollar amount to cents; no other
module does its own arithmetic. This matters because the model that reads
each invoice image returns a money field as a JSON number instead of a
string in about one call of three, measured by running the same image
three times. The schema types money fields as strings, and the converter
accepts a string, a float, or an int anyway. Either defence alone leaves a
gap for roughly a third of runs.

**Hand-calculated oracles, stored separately from product output.** The
expected result for each invoice was worked out by hand first, before the
product ran, and stored in `tasks/invoices/expected-seed-results.json` and
two companion files. The seed check compares the product's output against
these figures, not against a prior run of the product itself. A test that
checks a program against its own output proves nothing.

**Committed responses, so replay needs no AWS access.** Real Bedrock
responses for all six invoices are committed under
`tests/fixtures/bedrock_responses/`. The `--from-cache` flag replays the
real parsing and reconciliation logic against these saved responses; only
the network call is skipped. An assessor whose AWS session has expired can
still run the full flow and see it work.

**A linear pipeline, not an agent framework.** The product itself —
extraction, matching, reconciliation, persistence — is one straight
pipeline, not an agent or a chain of LLM calls making decisions about
control flow. The CLI and the web view both call the same pipeline
function and read and write the same database. The web view adds no
reconciliation logic of its own.

**Patterns, not single cases, drive the improvement.** The summary page
counts every issue type in the saved results: unit-price, quantity and
total differences, duplicates, missing purchase-order references and
failed reads. It ranks them and shows the top two. An issue is called
recurring only when two or more invoices have it. One model call phrases
the improvements, but code chooses each conclusion, such as "Recheck unit
prices against the purchase order before shipping.", and a verifier
rejects blame words, dollar figures and wrong invoice names. On the seeded
batch, the unit price differs on two invoices, one above and one below
the agreed price. That points to a price-data check, not to wrongdoing.

## Findings

**Six defects, each caught by a check.** Listed in full in README.md. Two
worth naming here: a malformed stored money value used to crash the whole
batch instead of failing one invoice — fixed so one bad invoice is marked
`failed` while the rest of the batch keeps processing. And a failed
extraction was originally classified the same as an invoice with no
purchase-order reference — the batch summary then reported zero failures
on a run that had printed two. These are different facts calling for
different reviewer action, so they are now reported separately.

**The measured money-type variance.** Documented above, under integer
cents. This was found by running the same image through extraction three
times and comparing the raw responses, not by reading documentation about
what the model is supposed to return.

**The recoverable-total trap.** The obvious computation — sum every
discrepant invoice's difference, positive and negative — gives $70.00 for
the seeded batch. The correct figure is $80.00: the sum of positive
differences on discrepant invoices only. The seeded batch has one
undercharge alongside two overcharges, and summing everything lets the
undercharge quietly cancel part of a real overcharge, understating what
the business can actually recover. A guard test exists for exactly this:
removing the positive-difference filter makes three acceptance tests fail.

**A model failure used to stop the whole batch.** An audit reproduced it:
one throttled call, or a missing AWS credential, stopped ingest at the
first invoice with a traceback. Every failure of the call itself now
fails only its own invoice, with the Bedrock error code in the stored
reason. With no credentials at all, all six invoices show as `failed`
with the reason, the batch completes, and the CLI exits with code 2.

**Failed versus unresolved.** `unresolved` means the invoice was read
successfully but carries no purchase-order reference — a fact about the
document. `failed` means the pipeline could not process the invoice at
all — a fact about the pipeline. These were briefly conflated during the
build: a failed extraction wrote no fields, and the matcher read the
absence of a purchase-order id as `unresolved`. Fixed by persisting a
failure reason and classifying a failed extraction as `failed`.

## Honest limitations

Stated plainly, not glossed:

- PDF invoice support was not attempted. The seeded invoices are PNG only.
- No export route exists for the review queue or the verified
  calculations.
- The `corrections` table is an append-only audit trail, and each
  field's citation on the detail page shows its correction chain. There
  is no separate page that lists correction history. Discrepancy notes do
  keep a version history, shown on the detail page.
- Four questions the brief leaves open are recorded, not decided, in
  the README's "Settled ambiguities" (entries 7 to 10): whether a
  discrepancy is only about the billed total, a purchase order with more
  than one receipt, a purchase order with no receipt, and the SKU. Entry
  11 explains why no price trend is claimed: the dates are synthetic and
  the batch is small.
- With no AWS access, notes and improvements use their calculated text.
  Only extraction has saved real responses to replay.
- The two required submission items this note and its companion document
  close — the LLM usage note and this walkthrough — did not exist before
  this pass. Do not treat their presence as implying the rest of the
  submission was already complete; `docs/compliance-audit.md` is the
  source of truth for what is PASS, PARTIAL, or FAIL at any given moment,
  and it is worth a fresh read rather than a memory of its last state.

## What I would do next with more time

- Add PDF support, since the brief lists it as optional but it is a
  realistic input format for this domain.
- Build an export route for the review queue, so a reviewer's verified
  figures can leave the application as a file.
- Add a page that lists the correction history across all invoices.
- Save one real response for the note and improvement calls, so the
  model-drafted text also replays without credentials.

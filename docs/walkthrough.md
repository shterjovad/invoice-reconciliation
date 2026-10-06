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

**Failed versus unresolved.** `unresolved` means the invoice was read
successfully but carries no purchase-order reference — a fact about the
document. `failed` means the pipeline could not process the invoice at
all — a fact about the pipeline. These were briefly conflated during the
build: a failed extraction wrote no fields, and the matcher read the
absence of a purchase-order id as `unresolved`. Fixed by persisting a
failure reason and classifying a failed extraction as `failed`.

## Honest limitations

Six of thirty-seven rows in `docs/compliance-audit.md` are not a clean
pass. Stated plainly, not glossed:

- PDF invoice support was not attempted. The seeded invoices are PNG only.
- No export route exists for the review queue or the verified
  calculations.
- The `corrections` table is an append-only audit trail, but this was a
  by-product of the required correction flow, not a deliberately pursued
  history feature — there is no revision history for discrepancy notes
  themselves, and no UI surface presents correction history as its own
  feature.
- The summary page states one process-improvement observation in the
  running application, but at the time of the audit its exact wording was
  not also restated in README.md, so a reader of the README alone would
  not see the claim's text without starting the server.
- The README states time spent against the project's hour budget only
  after the compliance audit flagged its absence; confirm the current
  README still carries that figure before submission.
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
- Surface correction history and note-revision history as a visible
  feature on the detail page, rather than leaving it recoverable only by
  querying the database directly.
- State the process-improvement suggestion's exact text in README.md, so
  an assessor reading only that file sees the claim without starting the
  server.

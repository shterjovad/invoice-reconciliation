# Invoice Reconciliation and Review

This tool reads invoice images, matches each one against a purchase order and
a receipt, and classifies it as reconciled, discrepant, duplicate, or
unresolved. A reviewer can correct a misread value through a web page, and
the result recalculates on the spot.

One pipeline runs both entry points. The headless batch command (`cli.py`)
and the web view (`web/`) read and write the same database through the same
reconciliation function. Neither is a copy of the other.

Full design detail is in `context/spec/001-invoice-reconciliation-review/`.
This file is the entry point for an assessor: what to run, what to expect,
and what was decided along the way.

---

## Run instructions

### The assessor path — no AWS access needed

```
uv run python -m invoice_reconciliation.cli --reset-db --from-cache --seed-check
```

This rebuilds the database, replays the six invoices from saved Bedrock
responses committed in `tests/fixtures/bedrock_responses/`, reconciles each
one, and checks the result against the hand-calculated answers in
`tasks/invoices/expected-seed-results.json`. No network call happens. Run
with the AWS credential chain fully disabled, the command still works:

```
$ AWS_PROFILE=nope AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
  uv run python -m invoice_reconciliation.cli --reset-db --from-cache --seed-check

Processed 6 invoice(s):
  [clean] status=reconciled expected=$100.00 billed=$100.00 difference=$0.00
  [wrong-price] status=discrepant expected=$100.00 billed=$120.00 difference=$20.00
  [duplicate] status=duplicate expected=- billed=- difference=- count_as_payable=False
  [missing-reference] status=unresolved expected=- billed=- difference=-
  [quantity-overbill] status=discrepant expected=$100.00 billed=$160.00 difference=$60.00
  [undercharge] status=discrepant expected=$100.00 billed=$90.00 difference=-$10.00
Processing: all invoices processed (no 'failed' status).

Seed check results:
  [PASS] clean: OK (reconciled)
  [PASS] wrong-price: OK (discrepant)
  [PASS] duplicate: OK (duplicate)
  [PASS] missing-reference: OK (unresolved)
Seed comparison: PASSED — all cases match the oracle exactly.
```

Exit code `0`.

### Live extraction (needs AWS credentials)

```
uv run python -m invoice_reconciliation.cli --extract --reset-db --seed-check
```

This reads each invoice image through a live Bedrock call instead of the
saved responses. It needs a valid AWS session with Bedrock access in
`us-east-1`. Two commands produce that session:

- `aws login` — the in-house wrapper used on this machine.
- `aws configure sso` once, then `aws sso login --profile <name>` — the
  standard AWS CLI path. An external assessor will not have the in-house
  wrapper, so this is the one to use.

If the session lands under a named profile rather than `default`, set
`export AWS_PROFILE=<name>` first. Without it, the credential chain resolves
`default` and the run fails in a way that reads like a code defect when it
is a configuration one.

To force fresh live calls and overwrite the saved responses, add
`--refresh-cache` in place of `--from-cache`. `--from-cache` and
`--refresh-cache` are mutually exclusive.

### All CLI flags

```
uv run python -m invoice_reconciliation.cli --help
```

| Flag | Purpose |
| --- | --- |
| `--extract` | Read each invoice through a live Bedrock call. Needs AWS credentials. Off by default. |
| `--from-cache` | Run extraction from saved responses. No credentials needed. |
| `--refresh-cache` | Force live calls and overwrite the saved responses. |
| `--seed-check` | After persisting, compare results with `expected-seed-results.json`. |
| `--reset-db` | Drop and rebuild the database before ingest. |
| `--ingest-dir PATH` | Source directory. Default `tasks/invoices/`. |
| `--db-path PATH` | Database location. Default `invoice_reconciliation.sqlite`. |
| `--log-level` | Logging verbosity. Default `WARNING`. |

Exit codes separate two different problems — "the model failed" and "the
rules are wrong" — rather than merging them into one number:

| Code | Meaning |
| --- | --- |
| 0 | Success. No invoice failed, and the seed check (if requested) passed. |
| 1 | Seed check failed, but no invoice failed to process. A rules/data problem. |
| 2 | One or more invoices failed to process, or the batch crashed before finishing. A processing problem. |
| 3 | Both: at least one invoice failed to process, and the seed check (if requested) also failed. |

### The web view

Run the batch command once first, with no `--db-path` override, so it
writes to the default `invoice_reconciliation.sqlite` in the repository
root:

```
uv run python -m invoice_reconciliation.cli --reset-db --from-cache
```

Then start the server:

```
uv run uvicorn invoice_reconciliation.web.app:create_app --factory
```

Open `http://127.0.0.1:8000/invoices`. Three screens:

- **`/invoices`** — the queue. Every invoice with its status, expected
  amount, billed amount, and difference. Links to filter by status.
- **`/invoices/{id}`** — the detail page for one invoice: the image, the
  seven extracted fields (original value beside current value, with a
  correction form for each), the matched purchase order and receipt, the
  expected-versus-billed calculation, and a draft discrepancy note.
  Saving a correction recalculates the result in place and redirects back
  to this page.
- **`/summary`** — counts per status, the recoverable total, each
  discrepant invoice's difference, and one suggested process improvement.

Screenshots from a real run, against the six seeded invoices:

| Queue | Detail | Summary |
| --- | --- | --- |
| ![queue](docs/screenshots/queue.png) | ![detail](docs/screenshots/detail.png) | ![summary](docs/screenshots/summary.png) |

### Tests

```
uv run pytest -q
```

136 tests pass.

---

## The five required checks

Run against the six seeded invoices, with the AWS credential chain fully
disabled (`AWS_PROFILE=nope AWS_EC2_METADATA_DISABLED=true
AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null`), so the
result cannot depend on network access.

| # | Check | Expected | Observed | Result |
| --- | --- | --- | --- | --- |
| 1 | Clean invoice reconciles | status `reconciled`, difference $0.00 | `status=reconciled expected=$100.00 billed=$100.00 difference=$0.00` | PASS |
| 2 | Wrong price flags as discrepant | status `discrepant`, difference $20.00 | `status=discrepant expected=$100.00 billed=$120.00 difference=$20.00` | PASS |
| 3 | Duplicate invoice excluded from payable total | status `duplicate`, `count_as_payable=false` | `status=duplicate expected=- billed=- difference=- count_as_payable=False` | PASS |
| 4 | Missing reference stays unresolved, no invented match | status `unresolved`, no matched purchase order or receipt | `status=unresolved expected=- billed=- difference=-`; `matched_po_id=(none)` `matched_receipt_id=(none)` | PASS |
| 5 | A correction survives a restart | the corrected result is still there after a new process opens the database | see below | PASS |

**All five checks pass.**

Check 5, run across two separate processes — a correction made in one
server process, then read back from a brand-new one with no shared memory:

```
before: ('discrepant', 2000)
POST /invoices/2/fields/total_cents  -> 303 See Other

[server process stopped, a new one started against the same database file]

after (new process): ('reconciled', 0)
original_value / current_value for total_cents: ('12000', '10000')
corrections rows for this invoice: 1
```

`original_value` stays `12000` — the value extraction first produced is
never overwritten. `current_value` moved to `10000`, the corrected amount.
One row was appended to `corrections` recording the change. The new
process read the corrected, reconciled result straight from disk, with no
server-side state carried over.

---

## The rounding rule

The platform stores every money value as a whole integer number of USD
cents. `money.dollars_to_cents` is the one function that converts a dollar
amount to cents, and every other module calls it rather than doing its own
arithmetic. All later arithmetic — the expected amount, the billed amount,
the difference, the recoverable total — works on integers only. No step
divides cents by 100 and no step multiplies a float by 100. The platform
converts each amount to cents exactly once, at the boundary where it enters
the system, and never rounds again after that.

The model sometimes returns a money field as a JSON number instead of a
string — measured at about one call in three. `dollars_to_cents` accepts
`str | float | int` for this reason. A numeric input is first formatted as
text with `f"{v:.2f}"`, then split on the decimal point, with each half
parsed as an integer. `float(x) * 100` is never applied to a string — that
is the single most likely silent failure in a project like this, because it
can move a result by a cent without raising anything. The two cases are
tested side by side: `dollars_to_cents("24.00")` and `dollars_to_cents(24.0)`
both return `2400`; `dollars_to_cents("120.00")` and `dollars_to_cents(120.0)`
both return `12000`; `dollars_to_cents(5)` returns `500`.

---

## Settled ambiguities

Six gaps in the brief or the supplied data came up during the build. Each
one is recorded here with the gap, the behaviour chosen, and the reason —
not resolved silently.

### 1. Undercharge (a negative difference)

**Gap.** The rules name a positive difference an overcharge, and say
nothing about a negative one.

**Chosen.** Status `discrepant`, difference negative. Excluded from the
recoverable total.

**Reason.** The difference is real and worth showing to a reviewer. It is
not money the business can pursue, so it stays out of the recoverable
figure.

### 2. Quantity mismatch

**Gap.** The rules call for comparing billed quantity against ordered and
received quantity, but every supplied invoice carries quantity 5
throughout. The rule had nothing to test it against.

**Chosen.** A quantity mismatch forces `discrepant`, valued at the agreed
unit price. A new fixture (`quantity-overbill`) was added so the rule has
a case to run against.

**Reason.** The functional specification settles the reading. Leaving the
rule untested would have meant shipping untested logic.

### 3. The recoverable total

**Chosen.** The sum of positive differences on `discrepant` invoices only —
$80.00 for this batch. Duplicates and unresolved invoices are reported as
counts, not folded into any dollar figure.

**Reason.** Summing all discrepant differences, positive and negative,
gives $70.00 for this batch, because the undercharge partly cancels a real
overcharge. That number understates what the business can actually
recover. Duplicates and unresolved invoices have a value that is either not
owed or not known, which is a different kind of "not counted" than zero.

### 4. Failed versus unresolved

**Gap.** `unresolved` means the invoice was read successfully but carries
no purchase-order reference. `failed` means the invoice could not be
processed. These were initially conflated: a failed extraction wrote no
fields, and the matcher read the absence of a `po_id` as "unresolved", not
"failed".

**Chosen.** A failed extraction now records `extraction_failed` and a
reason, and is classified `failed`, not `unresolved`.

**Reason.** The two cases call for different reviewer action. "No
purchase-order reference was printed on this invoice" is a fact about the
document. "The model could not read this invoice at all" is a fact about
the pipeline. Folding one into the other hid the second from a reviewer.

### 5. Which field a correction must change

**Gap.** Correcting an invoice's `unit_cents` has no visible effect,
which could look like a bug.

**Chosen.** Left as documented behaviour, not changed.

**Reason.** `rules.py` reads `billed_cents` from the invoice's own
`total_cents` field, and `expected_cents` from the matched purchase
order's unit price — never from the invoice's own `unit_cents`. Correcting
`unit_cents` therefore changes neither side of the comparison, and the
status does not move. `total_cents` is the field that drives the billed
side. `tasks/invoices/expected-correction-case.json` records the
hand-calculated before and after for the one field that does move the
result.

### 6. Duplicate detection

**Chosen.** Duplicates are matched on supplier ID and invoice number,
ordered by `received_at`. The first copy by that order stays payable; any
later copy is `duplicate` with `count_as_payable` false.

**Reason.** The brief calls for identifying duplicates by supplier ID and
invoice number. `received_at` gives a defined order for which copy counts
as the original when more than one invoice shares that pair.

---

## Six defects found during the build

Each of these was caught by a test or a check, not by reading the code
afterward. They are listed because they show what the test suite actually
exercised.

1. **`receipts.receipt_id` was typed `INTEGER` against text fixture values**
   such as `"RC-1"`. Ingest crashed with `datatype mismatch`. Two further
   columns in the same table carried the same class of error:
   `matched_receipt_id` had the same wrong type, and `count_as_payable` was
   `NOT NULL` when it must be null for every status except `duplicate`.
2. **A second CLI run without `--reset-db` failed on a foreign-key
   constraint.** Ingest needed to either refuse a second run cleanly or
   require `--reset-db` — the second run without it is not a supported
   path.
3. **Malformed stored money crashed the whole batch instead of failing one
   invoice.** Fixed so `MoneyFormatError` is caught per invoice, in
   `pipeline.recalculate_one`, and that one invoice is marked `failed`
   while the rest of the batch keeps processing.
4. **`AnthropicBedrockMantle` returned `404` for every model tried, on this
   account.** It targets a separate AWS service
   (`bedrock-mantle.us-east-1.api.aws`) from classic Bedrock
   (`bedrock-runtime.us-east-1.amazonaws.com`), with its own entitlement.
   Classic `bedrock-runtime` returns a real completion with the same
   credentials, region, and model ID. The technical specification was
   corrected after this was found.
5. **A failed extraction was classified `unresolved`.** The batch summary
   then reported no failures on a run that had printed two. This is the
   same gap recorded above as ambiguity 4, fixed the same way.
6. **No layer persisted a failure reason.** `extraction_failed` held only a
   boolean, so the detail page could show that extraction failed but not
   why. A reason field was added so a reviewer has something to act on.

**Also worth stating plainly:** the model returns a money field as a JSON
number, not a string, in roughly one call of three (measured, not assumed).
The project defends against this in two places at once — the JSON Schema
types the money fields as strings, and `dollars_to_cents` accepts a number
anyway. Either defence alone would have left a gap.

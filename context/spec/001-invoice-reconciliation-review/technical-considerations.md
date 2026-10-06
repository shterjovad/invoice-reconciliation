# Technical Specification: Invoice Reconciliation and Review

- **Functional Specification:** `context/spec/001-invoice-reconciliation-review/functional-spec.md`
- **Status:** Draft
- **Author(s):** shterjovad

---

## 1. High-Level Technical Approach

The repository holds no application code. This is new work.

The product is one Python package with **one pipeline and two entry points**:

```
                 ┌──────────────────────────────────────────┐
  invoice PNGs ──▶│ ingest → extract → reconcile → persist │──▶ SQLite
  seed.json    ──▶└──────────────────────────────────────────┘      │
                            ▲                    ▲                   │
                            │                    │                   ▼
                    CLI (batch, replay,    web correction      FastAPI + Jinja2
                     seed check)           re-runs this            (queue,
                                            same step              detail,
                                            per invoice)           summary)
```

The rules engine is a **pure function** with no database access and no network access. The batch
command calls it. The web view calls the same function after a correction. Neither re-implements
the rules.

Three constraints shape everything below:

1. **Money never touches a float.** One module converts dollar strings to integer cents. Every other
   module imports it.
2. **The parse path is the same for a live call and a replay.** Only the network call changes.
3. **The seed check compares the exact record shape** of `expected-seed-results.json`. It does not
   normalise.

---

## 2. Proposed Solution and Implementation Plan (The "How")

### 2.1 Package layout

**Decision:** the package is `src/invoice_reconciliation/`. The three design passes proposed three
different names. This one is now fixed, and all paths below use it.

```
src/invoice_reconciliation/
├── config.py                  ModelConfig, paths, env var reading
├── money.py                   THE converter: dollars_to_cents()
├── models.py                  dataclasses shared across layers
├── pipeline.py                orchestration; the ONLY caller of rules + repository together
├── cli.py                     batch command entry point
├── seed_check.py              compares results with expected-seed-results.json
├── extraction/
│   ├── client.py              Bedrock client construction (lazy)
│   ├── prompt.py              the one prompt constant
│   ├── schema.py              the seven-field JSON Schema
│   ├── parser.py              raw response → ExtractedFields; pure, replay-exercised
│   └── cache.py               ResponseCache, keyed by image content hash
├── reconciliation/
│   ├── matcher.py             find PO + receipt; find earlier duplicate
│   ├── rules.py               THE rules engine; pure function
│   └── notes.py               drafts the discrepancy note
├── db/
│   ├── connection.py          get_connection(), PRAGMA foreign_keys=ON
│   ├── schema.py              DDL, init_db()
│   └── repository.py          all SQL for all tables
└── web/
    ├── app.py                 FastAPI factory, Jinja2 setup
    ├── routes.py              the six routes
    └── templates/             queue.html, detail.html, summary.html
tests/
├── unit/                      test_money.py, test_rules.py, test_parser.py, test_cache.py
├── integration/               test_pipeline.py, test_web.py
└── fixtures/bedrock_responses/  committed raw responses, one per invoice image
```

### 2.2 Data model

All money columns are `INTEGER` holding cents. No `REAL` column may hold money.

| Table | Key columns | Purpose |
| --- | --- | --- |
| `purchase_orders` | `po_id` PK, `supplier_id`, `sku`, `quantity`, `unit_cents` | Supplied reference data |
| `receipts` | `receipt_id` PK, `po_id` FK, `sku`, `quantity` | Supplied reference data |
| `invoices` | `invoice_id` PK, `file_id` UNIQUE, `image_path`, `layout`, `received_at`, `extraction_source` | One row per source document. `received_at` orders the duplicate rule. `extraction_source` is `live` or `cache`. |
| `extracted_fields` | PK (`invoice_id`, `field_name`), `original_value`, `current_value` | The seven values. `original_value` is never overwritten. `current_value` changes on correction. Both are TEXT, because this table holds what the model returned before interpretation. |
| `corrections` | `correction_id` PK, `invoice_id` FK, `field_name`, `value_before`, `value_after`, `changed_by`, `changed_at` | Append-only audit trail |
| `reconciliation_results` | `invoice_id` PK, `status`, `expected_cents`, `billed_cents`, `difference_cents`, `count_as_payable`, `matched_po_id`, `matched_receipt_id`, `computed_at` | Overwritten on each recompute. All cent columns are nullable. |
| `discrepancy_notes` | `invoice_id` PK, `drafted_text`, `current_text`, `is_reviewer_edited`, `edited_at` | One row per discrepant invoice |

`status` carries a `CHECK` constraint over the five values: `reconciled`, `discrepant`, `duplicate`,
`unresolved`, `failed`.

**Two columns, not a revisions table.** `extracted_fields` keeps `original_value` and
`current_value` side by side. Reconciliation then reads the current value with a flat `SELECT`, with
no "find the latest row" query that could read a stale value. The `corrections` table holds the full
history. This splits current state from change history rather than making one table serve both.

**Indexes:** `purchase_orders(supplier_id, po_id)` for the match join; `receipts(po_id)`;
`invoices(file_id)` UNIQUE; `corrections(invoice_id)`.

**Note on the match join.** `supplier_id`, `po_id` and `invoice_number` live in `extracted_fields`,
because a reviewer can correct them. Matching and duplicate detection therefore join through
`extracted_fields` rather than reading a column on `invoices`. At six to eight invoices this costs
nothing, and it keeps one source of truth for a correctable value.

### 2.3 The money converter — the highest-risk module

**The model does not always return the same type.** Three identical calls against
`tasks/invoices/images/wrong-price.png` gave two different shapes. One call returned JSON numbers
(`quantity: 5`, `unit_price_usd: 24.0`, `total_usd: 120.0`). Two returned strings. A float that
reaches `s.split(".")` raises `AttributeError`. That is a crash, not a wrong figure, and it would
appear in roughly one run of three.

The project answers this in two places:

1. **At the source.** The JSON Schema in 2.4 constrains the money fields to `"type": "string"`.
   This removes the variance where it starts.
2. **At the converter.** The converter still accepts a number, because a schema is a request and not
   a guarantee. Defence at both ends costs one line and removes a whole class of run-to-run failure.

`money.py` exposes two functions:

| Function | Contract |
| --- | --- |
| `dollars_to_cents(v: str \| float \| int) -> int` | Accepts a string or a number. **Normalises a numeric input with `f"{v:.2f}"` first**, which makes it a string with exactly two decimal places. Then splits on `"."`, parses each half as `int`, and returns `whole * 100 + frac` with the sign applied once. **It never calls `float()` on a string.** The format call is the only place a number is handled, and it converts to text rather than multiplying. |
| `cents_to_display(cents: int) -> str` | The inverse, for display only. |

The distinction matters. `float(x) * 100` on a string is the bug this project must avoid, because it
rounds silently. `f"{v:.2f}"` on a value the JSON parser already produced as a float is a different
operation: it fixes the text form before any arithmetic, and the arithmetic that follows is integer.

**The rounding rule, stated for the documents:** the platform converts each amount to whole cents
once, at the boundary. All later arithmetic uses integers. The platform therefore never rounds
during a calculation, and no result can move by a cent.

Unit tests must cover:

| Input | Expected | Reason |
| --- | --- | --- |
| `"24.00"` | `2400` | trailing zeros |
| `"120.00"` | `12000` | the worked example |
| `"5.5"` | `550` | one decimal place |
| `"5"` | `500` | no decimal point |
| `"0.09"` | `9` | leading zero in the cents |
| `"-12.50"` | `-1250` | negative, for the underbill case |
| `24.0` | `2400` | **a float from the model.** Measured: the model returns this shape in about one call of three. |
| `120.0` | `12000` | **a float**, the worked example in its numeric form |
| `5` | `500` | **an int**, the shape the model returns for a whole-dollar amount |
| `"24.999"` | raises `MoneyFormatError` | over-precision. The platform raises rather than truncates, because silent truncation is itself a money bug. |
| `""`, `"abc"` | raises `MoneyFormatError` | malformed input never coerces silently |
| `None` | raises `MoneyFormatError` | an absent value never becomes zero |

A `MoneyFormatError` during extraction marks that invoice `failed`. It does not stop the batch.

### 2.4 Extraction and the replay cache

**Contract.** `ExtractedFields` is a frozen dataclass with seven fields. `po_id` is `str | None`.
`unit_price` and `total` leave this layer **unconverted**, in the form the model returned. The schema
asks for strings, and the dataclass types them `str | float | int`, because measured calls show the
model sometimes returns a number. The converter in 2.3 handles both. `quantity` is an `int`, because
it is a count and carries no rounding risk.

**Client class.** The Bedrock client is **`AnthropicBedrockMantle(aws_region="us-east-1")`** from the
`anthropic` package. It is **not** `AnthropicBedrock`, which is the legacy `InvokeModel` path.
`extraction/client.py` is the only module that names this class.

**Structured output.** A JSON Schema forces the shape. Two constraints carry weight:

- `po_id` is typed `["string", "null"]` and is required. The model must emit the key, and `null` is a
  valid answer. The prompt states: if no purchase-order reference is printed, set `po_id` to `null`,
  and do not guess. This is what keeps the `missing-reference` fixture `unresolved`.
- **The money fields are typed `"string"`**, with a description giving the form `"24.00"`. Measured
  behaviour made this necessary: without the constraint, the same call returns `24.0` as a JSON
  number about one time in three. The converter in 2.3 also accepts a number, so the two defences
  overlap on purpose.

`quantity` stays typed `"integer"`. It is a count, it carries no rounding risk, and the measured
calls returned it consistently.

**No layout branching.** The prompt tells the model to read by field label, not by position. Layout
`a` and layout `b` run the same code path. The committed cache entries span both layouts, so the
parser tests exercise both.

**Validation**, in order, each raising a subclass of `ExtractionError`:

1. Shape — did the response contain the structured block at all?
2. Schema — does it validate against the seven-field schema?
3. Domain — is `quantity > 0`, and is each money field a value that `money.dollars_to_cents`
   accepts? **The parser delegates this check to the converter rather than applying its own regular
   expression.** An earlier draft matched `^-?\d+\.\d{2}$` here, which would reject the numeric
   `24.0` that the model returns about one call in three, and mark a good invoice `failed`. One rule
   for what counts as a valid amount, in one module, prevents the two from drifting apart.

**Cache key: a SHA-256 hash of the image bytes**, not the `file_id`. A content hash makes the cache
self-verifying. If anyone regenerates a fixture image, the hash changes and the entry misses. The
pipeline then calls the model, or in replay mode fails loudly. Keying on `file_id` would serve a
stale response for changed content, which is the exact failure a replay must not hide. The
`file_id` still appears inside the entry for readability.

**Cache entry** holds a metadata envelope plus `raw_response`, which is the unmodified response from
the SDK. The parser reads only `raw_response`, so a file on disk is indistinguishable from a live
response. These files are **committed**. They are fixture data, not local state.

**The replay switch** wraps the network call only:

```
cached = cache.get(image_bytes)
if use_cache:          # --from-cache
    raw = cached.raw_response        # CacheMissError if absent
else:
    raw = client.messages.create(...)
    cache.put(...)
fields = parser.parse(raw)           # the SAME call on both paths
```

The Bedrock client is built lazily, inside the live branch. A replay run therefore never touches the
credential chain, and a reviewer with no AWS access can run the whole flow.

**Extraction runs serially.** Six to eight invoices do not justify concurrency. Serial execution
keeps per-invoice failure isolation simple and the cache writes free of races.

### 2.5 The rules engine

`reconciliation/rules.py` holds one pure function. It takes the current field values, the matched
purchase order, the matched receipt and a duplicate flag. It returns a result. It performs no
input and output.

Order of evaluation per invoice:

1. **Failed.** If extraction raised, the status is `failed` with no amounts. Stop.
2. **Unresolved.** If `po_id` is null, or no purchase order matches `(supplier_id, po_id)`, or the
   purchase order exists under a different supplier, the status is `unresolved` with no amounts.
   Stop. **The platform never falls back to matching on amount.**
3. **Duplicate.** If an earlier invoice shares `(supplier_id, invoice_number)`, the status is
   `duplicate` with `count_as_payable = false` and no cent fields. Stop. The first copy by
   `received_at` continues through steps 4 and 5.
4. **Compute.** `expected_cents = ordered_quantity * unit_cents` from the purchase order.
   `billed_cents` comes from the invoice total through `money.dollars_to_cents`.
   `difference_cents = billed_cents - expected_cents`.
5. **Classify.** If the billed quantity differs from the ordered quantity or the received quantity,
   the status is `discrepant`, and the difference stays valued at the agreed unit price. Otherwise,
   a zero difference gives `reconciled` and a non-zero difference gives `discrepant`. A negative
   difference is `discrepant` too. The sign alone does not change the status.

**Recoverable total**, computed in the summary query, not stored per row:

```
SUM(difference_cents) WHERE status = 'discrepant' AND difference_cents > 0
```

This excludes `duplicate`, `unresolved`, `failed` and every negative difference.

**Conflict resolved.** One design pass proposed that a quantity mismatch forces `discrepant` even
when the amounts agree. Step 5 above adopts that reading, because the functional specification
settles it: a quantity difference is `discrepant`, valued at the agreed unit price. This is recorded
as a settled ambiguity, and a new fixture tests it.

### 2.6 Route table

| Method | Path | Purpose | Mutates |
| --- | --- | --- | --- |
| `GET` | `/invoices` | The queue. Optional `?status=` filter. | No |
| `GET` | `/invoices/{id}` | Detail: image, seven values with original and current, matched records, calculation, draft note | No |
| `GET` | `/invoices/{id}/image` | Serves the invoice image | No |
| `POST` | `/invoices/{id}/fields/{field_name}` | Correct one value, then recalculate | **Yes** |
| `POST` | `/invoices/{id}/note` | Save a reviewer edit to the note | **Yes** |
| `GET` | `/summary` | Counts per status, recoverable total, differences, one improvement | No |

**No route sends anything.** The requirement that notes stay drafts is met by the absence of a send
route.

**How a correction recalculates without duplicating the rules.** The `POST` handler does three
things: it validates the new value; it writes a `corrections` row and updates
`extracted_fields.current_value` in one transaction; it calls `pipeline.recalculate_one(invoice_id)`.
That third call is the same function the batch loop uses per invoice. It re-reads the current
values, re-runs `matcher` and `rules.reconcile`, and overwrites the result row. The route holds no
reconciliation logic.

### 2.7 The batch command

| Flag | Purpose |
| --- | --- |
| `--from-cache` | Run extraction from saved responses. No credentials needed. |
| `--refresh-cache` | Force live calls and overwrite the saved responses. |
| `--seed-check` | After persisting, compare results with `expected-seed-results.json`. |
| `--reset-db` | Drop and rebuild the database before ingest. |
| `--ingest-dir PATH` | Source directory. Default `tasks/invoices/`. |
| `--db-path PATH` | Database location. |
| `--log-level` | Logging verbosity. |

The assessor path is `python -m invoice_reconciliation.cli --from-cache --seed-check`.

### 2.8 The seed check

`seed_check.py` compares **the exact record shape** of each entry. A `duplicate` record carries
`count_as_payable` and omits the cent keys. An `unresolved` record carries explicit `null` for
`expected_cents` and `difference_cents`, and omits `billed_cents`. **The comparison does not
normalise these shapes.**

It reports both a printed difference per failing case and an exit code: `0` when all cases agree,
`1` otherwise. **Two signals stay separate:** whether any invoice failed to process, and whether the
seed comparison passed. One exit code must not conflate them.

### 2.9 Configuration and secrets

`config.py` holds one frozen `ModelConfig`: the model ID
`us.anthropic.claude-sonnet-4-5-20250929-v1:0`, the region `us-east-1`, `max_tokens`, and
`temperature = 0.0` for repeatable extraction. Nothing else hardcodes the model ID.

`.env.example` carries two names only: `AWS_REGION` and `BEDROCK_MODEL_ID`. **Remove
`MODEL_API_KEY`.** Bedrock authenticates with SigV4 from the credential chain, so no key exists.

Per-call records come from the actual response, not from `ModelConfig`. A replayed entry then
reports the model that truly served it.

---

## 3. Impact and Risk Analysis

### System Dependencies

- **AWS Bedrock, us-east-1**, through the `anthropic` SDK. The session comes from `aws login`. The
  run instructions must say `aws login`, not `aws sso login`.
- **The supplied fixtures** in `tasks/invoices/` are the source of truth. The database rebuilds from
  them.
- **The host interpreter** is Python 3.11.6. Nothing the project needs is installed there yet.

### Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| **Money loses a cent through a float.** The highest-likelihood silent failure in the build. | One converter module, integer cents everywhere, `float()` never called on a money string, and the unit table in 2.3. A code review should grep for `float(` near money. |
| **The model returns a number where the code expects a string.** **Measured, not hypothetical:** three identical calls on `wrong-price.png` returned JSON floats once and strings twice. A float reaching `s.split(".")` raises `AttributeError` and crashes that invoice. | Two overlapping defences: the schema types the money fields as `"string"`, and the converter accepts `str \| float \| int`. Three numeric rows in the unit table hold this. Without both, roughly one run in three fails. |
| **The seed check passes on a normalised shape** and hides a real difference. | Compare the exact per-status key sets. Test the comparison itself against a deliberately wrong record. |
| **The model invents a purchase-order reference** for `missing-reference`, which destroys the `unresolved` case. | The schema types `po_id` as nullable and required. The prompt forbids guessing. A unit test asserts `po_id is None` for that fixture from its committed response. |
| **An expired session blocks the assessor.** | Replay mode runs the full flow from committed responses with no credentials. This is a required deliverable, not a convenience. |
| **A stale cache serves a wrong response** after someone regenerates a fixture. | The cache key is a content hash. A changed image misses the cache and fails loudly in replay mode. |
| **One bad invoice stops the batch.** | Per-invoice `try`/`except` scoped to extraction errors and SDK errors, never a bare `except Exception`. A failed invoice gets `status = failed` and no amounts. |
| **The 8-hour budget.** The brief caps the whole exercise, including data preparation and documents. | Build in roadmap order: rules proven against prepared data, then extraction, then the web view. The rules engine is testable without any model call. |
| **The web view and the batch drift apart.** | One `pipeline.recalculate_one` used by both. The rules function performs no input and output, so it cannot grow a second path. |

---

## 4. Testing Strategy

`pytest`. No test framework exists in the repository today.

**Unit tests, no network:**

- `test_money.py` — the table in 2.3. This is the most important test file in the project.
- `test_rules.py` — each status, the match rule against the two near-identical purchase orders, the
  duplicate order rule, the quantity mismatch, the negative difference, and the recoverable total
  excluding duplicates, unresolved and negatives.
- `test_parser.py` — valid responses for both layouts from committed fixtures; `po_id is None` for
  `missing-reference`; malformed, incomplete and wrong-typed responses each raising the right error.
- `test_cache.py` — hit, miss, and a changed image producing a miss.

**Integration tests:**

- `test_pipeline.py` — the full batch from committed responses, asserting the four supplied invoices
  reproduce `expected-seed-results.json`; a correction changing a status and the result surviving a
  reopened connection; one failed invoice not stopping the others.
- `test_web.py` — the queue, the status filter, the detail view, and a correction through the real
  route.

**The seed check** is the headline test. It runs from the CLI and as a test. The five reference
cases include the correction case required by the brief, with answers calculated by hand and stored
apart from any output of the platform.

**Evidence for completion.** A claim that the work is done cites the output of
`python -m invoice_reconciliation.cli --from-cache --seed-check` and the `pytest` run.

---

## 5. Assumptions Recorded

The user confirmed the package layout decisions below in the architecture step. These remain open
for challenge:

- **Assumption:** the package is `src/invoice_reconciliation/`. The three specialist designs
  proposed different names; this value reconciles them.
- **Assumption:** `money.py` raises on over-precision such as `"24.999"` rather than truncating.
- **Assumption:** `money.py` accepts negative amounts, because the underbill fixture needs them.
- **Measured, not assumed:** the model returns money fields as JSON numbers in about one call of
  three. The schema constrains them to strings and the converter accepts both. Three numeric rows in
  the unit table hold this.
- **Measured, not assumed:** the client class is `AnthropicBedrockMantle`, not `AnthropicBedrock`.
- **Assumption:** a quantity mismatch forces `discrepant` even when the amounts agree. The
  functional specification settles this, and a new fixture tests it.
- **Assumption:** the reviewer's edit to a draft note does not trigger a recalculation. Only a
  correction to an extracted value does.

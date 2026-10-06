# Tasks: Invoice Reconciliation and Review

Spec directory: `context/spec/001-invoice-reconciliation-review/`

**Slice order.** The rules engine comes first and is proven against the supplied answers with no
model calls. Extraction arrives at Slice 6 and changes only where the values come from. Fixture
records arrive early, so the rules get tested against them at once. The images render later, when
extraction exists to read them.

**Evidence rule.** Every Verify task reports real output from that run. It stops any process it
started, by the PID it recorded. It deletes its own artifacts. The Feature Testing slice keeps its
tests — those are the regression suite.

---

- [x] **Slice 1: Convert money without losing a cent**

  > The smallest piece of real value, and the highest-risk module in the build.

  - [x] Create the project with `uv`: `pyproject.toml`, Python 3.11, and the `src/invoice_reconciliation/` package. Declare `pytest` and `Pillow >= 10.1`. Do not add a Dockerfile or any CI file. **[Agent: python-backend]**
  - [x] Write `src/invoice_reconciliation/money.py` with `dollars_to_cents(v: str | float | int) -> int` and `cents_to_display(cents: int) -> str`. For a numeric input, format with `f"{v:.2f}"` before splitting on the decimal point. Never call `float()` on a string. Raise `MoneyFormatError` on over-precision such as `"24.999"`, on malformed input, and on `None`. **[Agent: python-backend]**
  - [x] Write `tests/unit/test_money.py` covering the full table from technical-considerations.md section 2.3: `"24.00"`, `"120.00"`, `"5.5"`, `"5"`, `"0.09"`, `"-12.50"`, the numeric forms `24.0`, `120.0` and `5`, plus the raising cases `"24.999"`, `""`, `"abc"` and `None`. **[Agent: testing-expert]**
  - [x] Verify: run `pytest tests/unit/test_money.py -v` and report the real output. Confirm every row passes, including the three numeric rows. Grep the source for `float(` and confirm no money path calls it. Delete any temporary files the check produced. **[Agent: testing-expert]**

- [x] **Slice 2: Hold the reference data and rebuild it from the fixtures**

  > After this slice the database exists and reloads from `seed.json` on demand.

  - [x] Write `src/invoice_reconciliation/db/connection.py` and `db/schema.py`. Create the seven tables from technical-considerations.md section 2.2. Every money column is `INTEGER`. `status` carries a `CHECK` over the five values. Add the four indexes. Set `PRAGMA foreign_keys=ON`. **[Agent: sqlite-database]**
  - [x] Write `src/invoice_reconciliation/db/repository.py` with the read and write functions for purchase orders, receipts and invoices. Keep the SQL explicit and parameterised. **[Agent: sqlite-database]**
  - [x] Write the ingest step that loads `tasks/invoices/seed.json` into the database, and a `--reset-db` path that drops and rebuilds it. The database is a rebuildable artifact; the fixtures are the source of truth. **[Agent: sqlite-database]**
  - [x] Verify: run the ingest, then query the database with `sqlite3` and report the real rows. Confirm two purchase orders, two receipts, and that `unit_cents` holds integers and not text or floats. Run it twice and confirm the second run gives the same result. Delete any scratch database file the check created. **[Agent: sqlite-database]**

- [x] **Slice 3: Match an invoice to its purchase order and receipt**

  > The first reconciliation behaviour. It proves the trap case: two purchase orders identical apart from their ID.

  - [x] Write `src/invoice_reconciliation/reconciliation/matcher.py`. Match on `supplier_id` **and** `po_id` together, reading them from `extracted_fields.current_value`. Never match on amount. Add `find_earlier_duplicate(supplier_id, invoice_number)` ordered by `received_at`. **[Agent: python-backend]**
  - [x] Extend the ingest to load the four supplied invoice records from `seed.json` into `invoices` and `extracted_fields`, writing the same value to `original_value` and `current_value`. This gives the rules engine real input before extraction exists. **[Agent: sqlite-database]**
  - [x] Write `tests/unit/test_matcher.py`: an invoice naming `PO-2` matches `PO-2` and not `PO-1`, although the two orders agree on supplier, SKU, quantity and price. A null `po_id` matches nothing. A purchase order under a different supplier does not match. **[Agent: testing-expert]**
  - [x] Verify: run `pytest tests/unit/test_matcher.py -v` and report the real output. Confirm the near-identical purchase orders stay separate. Delete any temporary files. **[Agent: testing-expert]**

- [x] **Slice 4: Give every invoice a status and the exact difference**

  > After this slice the four supplied invoices reproduce the supplied answers. This is the core of the exercise.

  - [x] Write `src/invoice_reconciliation/reconciliation/rules.py` as one pure function with no input or output. Apply the order in technical-considerations.md section 2.5: failed, then unresolved, then duplicate, then compute, then classify. `expected_cents = ordered_quantity * unit_cents`. `difference_cents = billed_cents - expected_cents`. A quantity mismatch gives `discrepant`, valued at the agreed unit price. A negative difference gives `discrepant`. **[Agent: python-backend]**
  - [x] Write `src/invoice_reconciliation/pipeline.py` with `run_batch()` and `recalculate_one(invoice_id)`. These are the only functions that call the rules engine together with the repository. Both the batch command and the web view will call them. **[Agent: python-backend]**
  - [x] Write `src/invoice_reconciliation/seed_check.py`. Compare against the **exact per-status record shape** of `tasks/invoices/expected-seed-results.json`: a `duplicate` record carries `count_as_payable` and omits the cent keys; an `unresolved` record carries explicit `null` for `expected_cents` and `difference_cents` and omits `billed_cents`. Do not normalise. Print a per-case difference and return exit code 0 or 1. **[Agent: python-backend]**
  - [x] Write `src/invoice_reconciliation/cli.py` with the flags from technical-considerations.md section 2.7. Keep the seed-check signal separate from the processing-failure signal; one exit code must not merge them. **[Agent: python-backend]**
  - [x] Write `tests/unit/test_rules.py` covering each of the five statuses, the recoverable total excluding duplicates, unresolved, failed and negative differences, and a `duplicate` first copy staying payable. **[Agent: testing-expert]**
  - [x] Verify: run `python -m invoice_reconciliation.cli --reset-db --seed-check` and report the real output. Confirm all four supplied invoices reproduce `expected-seed-results.json` exactly and the exit code is 0. Then edit one expected value in a scratch copy, re-run, and confirm the check reports that failure rather than passing. Delete the scratch copy. **[Agent: testing-expert]**

- [x] **Slice 5: Add the fixture records that test the undecided rules**

  > The supplied data cannot falsify two settled decisions. This slice adds records that can. Images come later, in Slice 7.

  - [x] Extend `tasks/invoices/seed.json` with an invoice that bills more units than were ordered and received, and an invoice that bills less than the agreed amount. Keep the four supplied invoices unchanged. Use the supplied rules; invent no new ones. **[Agent: python-backend]**
  - [x] Write the expected answers for both new invoices by hand into a separate file from any output of the product. Show the arithmetic in a comment or a short note so a reviewer can check it. **[Agent: python-backend]**
  - [x] Write `tests/unit/test_settled_rules.py`: the over-quantity invoice gives `discrepant` with the difference valued at the agreed unit price; the underbill gives `discrepant` with a negative difference; the recoverable total excludes that negative. **[Agent: testing-expert]**
  - [x] Verify: run the batch and the new tests, and report the real output. Confirm the two new invoices give the hand-calculated answers and that the four supplied invoices still reproduce their original results. Delete any temporary files. **[Agent: testing-expert]**

- [x] **Slice 6: Read the values from a real invoice image**

  > The first live model call. After this slice the pipeline runs from documents rather than prepared records.

  - [x] Write `src/invoice_reconciliation/config.py` with the frozen `ModelConfig`: model id `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, region `us-east-1`, `max_tokens`, and `temperature = 0.0`. Read `AWS_REGION` and `BEDROCK_MODEL_ID` from the environment with these as defaults. **[Agent: bedrock-extraction]**
  - [x] Write `extraction/client.py` using the classic **`boto3` `bedrock-runtime`** client with `invoke_model` and the `anthropic_version: "bedrock-2023-05-31"` body. **Corrected 2026-10-06:** this task previously required `AnthropicBedrockMantle`, which was tested and returns `404` for every model on this account — it is a separate AWS service with a separate entitlement. Build the client lazily, inside the live path only, so a replay run never touches the credential chain. **[Agent: bedrock-extraction]**
  - [x] Write `extraction/prompt.py` and `extraction/schema.py`. Type the money fields as `"string"` to remove the measured type variance. Type `po_id` as `["string", "null"]` and required. The prompt must tell the model to read by field label and not by position, and to set `po_id` to `null` when no reference is printed rather than guessing. **[Agent: bedrock-extraction]**
  - [x] Write `extraction/parser.py` as a pure function: shape check, schema check, then a domain check that **delegates money validation to `money.dollars_to_cents`** rather than applying its own regular expression. Raise a specific `ExtractionError` subclass per failure. **[Agent: bedrock-extraction]**
  - [x] Replace the prepared-record ingest with the extraction path in `pipeline.py`. The values now come from the model and flow into `extracted_fields.original_value` and `current_value`. **[Agent: python-backend]**
  - [x] Verify: run one live extraction against `tasks/invoices/images/wrong-price.png` and report the seven real values returned. Confirm the money fields arrive as strings and that `po_id` is `null` for `missing-reference.png`. Record the token counts. Delete any temporary output. **[Agent: bedrock-extraction]**

- [x] **Slice 7: Replay saved responses without credentials**

  > **Carried over issue from Slice 6 — RESOLVED in this slice.** A failed extraction was
  > classified `unresolved` (the status for "read it, no purchase-order reference") instead of
  > `failed`. Fixed by recording `invoices.extraction_failed` and passing it to the existing
  > `extraction_failed` parameter of `rules.reconcile`; the classification logic itself did not
  > change. Measured after the fix: a missing cache entry gives `failed` with 0 fields and exit
  > code 2, while `missing-reference` stays `unresolved` with 7 fields and still matches the
  > oracle.

  > A required deliverable. After this slice a reviewer with no AWS access runs the whole flow.

  - [x] Write `extraction/cache.py`. Name each file by `file_id`, and record `image_sha256`, `model_id` and `captured_at` inside alongside the unmodified `raw_response`. `cache.get(file_id, image_bytes)` warns and fails that invoice when the stored hash does not describe the image in hand. **[Agent: bedrock-extraction]**
  - [x] Add `--from-cache` and `--refresh-cache` to the batch command. The replay branch wraps the network call only; `parser.parse` runs identically on both paths. **[Agent: python-backend]**
  - [x] Run the live extraction once over every invoice image and commit the saved responses under `tests/fixtures/bedrock_responses/`. These are fixture data, not local state. **[Agent: bedrock-extraction]**
  - [x] Render the two new invoice images from the Slice 5 records with `tasks/invoices/render_invoices.py`, then capture their responses into the cache as well. Use layout `b` for at least one, so both layouts carry a new case. **[Agent: python-backend]**
  - [x] Write `tests/unit/test_cache.py`: a hit; a miss when no file exists; a changed image against a stored entry producing a warning and, in replay mode, a failure for that invoice rather than a stale response. **[Agent: testing-expert]**
  - [x] Verify: unset or remove AWS access for the duration of the check, run `python -m invoice_reconciliation.cli --reset-db --from-cache --seed-check`, and report the real output. Confirm the full batch completes with no credentials and the seed check passes. Restore the environment afterwards. Delete any scratch database. **[Agent: testing-expert]**

- [x] **Slice 8: Continue the batch when one invoice fails**

  > A required behaviour. One bad invoice must not cost the reviewer the rest of the run.

  - [x] *(Partly done in Slice 4 — `MoneyFormatError` isolation landed early when a defect surfaced. Extraction and cache error types still to add in this slice.)* Wrap each invoice in `pipeline.run_batch` with a `try`/`except` scoped to `ExtractionError`, `CacheMissError` and the SDK error types. Never use a bare `except Exception`; a genuine bug must still surface. A failed invoice gets `status = failed`, no amounts, and a readable reason. **[Agent: python-backend]**
  - [x] *(Satisfied by Slices 4 and 7; audited 2026-10-06 rather than rebuilt.)* Make the batch report the failed invoices separately from the seed-check result, and return a non-zero processing signal that does not merge with the seed-check exit code. Measured with one cache entry removed: the batch prints `Extraction: 1 invoice(s) FAILED to extract: ['undercharge']` and `Processing: 1 invoice(s) FAILED to process.` above an independent `Seed comparison: PASSED`, and exits 2. The four codes in `cli.py` stay distinct — 0 success, 1 seed-check failed, 2 processing failed, 3 both. **[Agent: python-backend]**
  - [x] Write `tests/integration/test_failure_isolation.py`: a corrupt or absent cache entry for one invoice leaves the others processed and listed; the failed invoice carries no expected amount and no difference. **[Agent: testing-expert]**
  - [x] Verify: corrupt one saved response in a scratch copy of the cache, run the batch from it, and report the real output. Confirm the other invoices still process and the failed one shows a reason. Restore the cache and delete the scratch copy. **[Agent: testing-expert]**

- [x] **Slice 9: Show the invoice queue in a browser**

  > The first user-visible screen. It reads the results the earlier slices already produce.

  - [x] Write `src/invoice_reconciliation/web/app.py` as a FastAPI factory with Jinja2 templates and a static mount. **[Agent: python-backend]**
  - [x] Add `GET /invoices` with an optional `?status=` filter, and `queue.html` listing every invoice with its status and main figures. **[Agent: python-backend]**
  - [x] Verify: start Uvicorn in the background and record its PID. Request the queue and the filtered queue, and report the real responses. Confirm every invoice appears with one status and that the filter narrows the list. Stop the server by the recorded PID, without piping the stop command into anything. Delete any captured output. **[Agent: python-backend]**

- [x] **Slice 10: Open one invoice and see the evidence behind its status**

  > The screen that makes the result trustworthy.

  - [x] Add `GET /invoices/{id}` and `GET /invoices/{id}/image`, with `detail.html` showing the invoice image beside the seven values, each with its original and current value, the matched purchase order and receipt, and the expected-against-billed calculation. **[Agent: python-backend]**
  - [x] Show an unresolved invoice with no expected amount and no difference, rather than a zero or an estimate. Show a failed invoice with its reason. **[Agent: python-backend]**
  - [x] Verify: start the server in the background and record its PID. Open the discrepant invoice and report the real page content: billed $120.00, expected $100.00, difference $20.00, and the matched `PO-2` and its receipt. Open the unresolved invoice and confirm no amounts appear. Stop the server by PID. Delete any screenshots or captured output. **[Agent: python-backend]**

- [x] **Slice 11: Correct a value and watch the result change**

  > The required correction case. It must survive a restart.

  - [x] Add `POST /invoices/{id}/fields/{field_name}`. Validate the new value, write a `corrections` row and update `extracted_fields.current_value` in one transaction, then call `pipeline.recalculate_one`. The route holds no reconciliation logic. **[Agent: python-backend]**
  - [x] Keep `original_value` visible beside the corrected value in `detail.html`, with the correction recorded. **[Agent: python-backend]**
  - [x] Add the correction case to the reference set: the field to correct, and the result the correction must produce, calculated by hand and stored apart from any output. **[Agent: python-backend]**
  - [x] Write `tests/integration/test_correction.py`: correcting the unit price on the discrepant invoice to the agreed price turns it `reconciled` with a difference of zero; the original value remains readable; the result survives a new connection. **[Agent: testing-expert]**
  - [x] Verify: start the server in the background and record its PID. Correct a value through the real route and report the recalculated status and difference. Stop the server, start it again, and confirm the correction and the new result are still there. Stop the server by PID. Delete any captured output. **[Agent: testing-expert]**

- [x] **Slice 12: Draft a note and summarise the batch**

  > The last of the required reviewer features.

  - [x] Write `reconciliation/notes.py`. Draft a short note for a discrepant invoice from the verified figures, naming the invoice number and the purchase-order reference. State no cause and suggest no wrongdoing. **[Agent: python-backend]**
  - [x] Add `POST /invoices/{id}/note` to save a reviewer's edit. Provide no send control anywhere. A note edit does not trigger a recalculation. **[Agent: python-backend]**
  - [x] Add `GET /summary` and `summary.html`: counts per status, the recoverable total as the sum of positive differences on discrepant invoices only, duplicates and unresolved reported separately, the differences described with their amounts, and one improvement naming the invoices that support it. **[Agent: python-backend]**
  - [x] Verify: start the server in the background and record its PID. Report the real summary output. Confirm the recoverable total holds only the genuine overcharge and excludes the duplicate, the unresolved invoice and the underbill. Open a discrepant invoice and confirm the draft note cites its records and offers no send control. Stop the server by PID. Delete any captured output. **[Agent: python-backend]**

- [x] **Slice 13: Report the demonstration results**

  > The brief requires a results table showing expected against observed behaviour.

  - [x] Produce the results table for the five required checks from the brief: the clean invoice matching its records; the wrong unit price giving the expected difference; the duplicate excluded from payable totals; the missing reference staying unresolved with no invented match; and a correction rerunning reconciliation and surviving a restart. Give the expected behaviour and the observed behaviour for each. Name any check that failed and why. **[Agent: testing-expert]**
  - [x] Record the settled ambiguities with the gap, the chosen behaviour and the reason. Record the run instructions with **both** credential commands, because an external assessor will not have the in-house wrapper: `aws login` (in-house) and `aws configure sso` once followed by `aws sso login --profile <name>` (standard AWS CLI). Add the `export AWS_PROFILE=<name>` step for a non-`default` profile, and state that credentials are needed for live extraction only — `--from-cache` needs none. Record the rounding rule: the platform holds all money as whole cents and never rounds during a calculation. **[Agent: python-backend]**
  - [x] Verify: run the full flow from a clean checkout path — `--reset-db --from-cache --seed-check` — and confirm the table matches the real behaviour. Report the real output. Delete any scratch files. **[Agent: testing-expert]**

- [x] **Slice 14: Feature Testing & Regression**

  > Verifies the whole feature end-to-end against functional-spec.md, run after all implementation slices are complete.
  - [x] Read functional-spec.md acceptance criteria in full. Generate acceptance-level tests that verify the entire feature as a whole — not individual slices. Cover applicable layers (unit for pure logic, integration for service interactions, e2e for user flows) based on the project's testing stack. Write tests with RED validation (must fail before implementation is confirmed done). Annotate each test with `@spec: 001-invoice-reconciliation-review` and `@regression` if suitable for long-term regression. **[Agent: testing-expert]**
  - [x] Run all generated tests. All must pass. Fix any failures before proceeding. **[Agent: testing-expert]**

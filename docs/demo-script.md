# Demo script

A step-by-step walkthrough for a live demo in front of an assessor. Keep
this open on a second screen and work down it. Each section names what to
run, what to point at, what to say, and what to answer if asked.

All commands below were run and confirmed working before this script was
written, and checked again on 9 October 2026. 249 tests pass.

---

## Opening 60 seconds

Say this before you touch anything:

> "This tool reads an invoice image, matches it against a purchase order
> and a receipt, and tells a reviewer whether it's safe to pay, wrong, a
> duplicate, or missing a reference. One pipeline does the matching —
> a command-line batch job and a web page both call the same
> reconciliation function, so there's no second copy of the logic to keep
> in sync.
>
> I'll run the batch headless first, against six invoices, with no AWS
> access at all, and show it reproduces a hand-calculated answer key
> exactly. Then I'll open the reviewer web page, correct a misread value,
> and show the result update live. Then I'll show two edge cases: an
> invoice the tool refuses to guess a match for, and a duplicate it
> correctly leaves out of the money total."

---

## Timing

- **3-minute path:** run table A in full, then only rows 1–5 of table B
  (open the discrepant invoice, show the evidence, correct it, watch the
  status change — skip the restart proof). Skip table C; mention the two
  edge cases in one sentence each instead of showing them.
- **5-minute path:** all of table A, all of table B including the restart
  proof, and rows 1–2 of table C (unresolved and duplicate). Skip the
  corrupted-cache row if time is short.
- Rows marked **[cut if short]** are the first to drop.

---

## A. The headless run (2 minutes)

Proves the batch reproduces a hand-calculated answer key, with no network
call.

| Step | What you do | What to point at on screen | What to say | If asked |
| --- | --- | --- | --- | --- |
| A1 | In a terminal, run:<br>`AWS_PROFILE=nope AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null uv run python -m invoice_reconciliation.cli --reset-db --from-cache --seed-check` | The four `AWS_*` variables in the command | "I've disabled every AWS credential on this machine before running this, so there's no way this call reaches the network." | *"Why disable credentials instead of just not having any?"* → "So the proof doesn't depend on this machine happening to be logged out. It's a deliberate block, not an accident of the environment." |
| A2 | Let it finish and point at the `Processed 6 invoice(s)` block | Each of the six lines, especially `status=` and `difference=` | "Six invoices, six different outcomes: one clean, three discrepant for three different reasons, one duplicate, one with no purchase-order reference at all." | *"What are the three discrepant reasons?"* → "Wrong price, wrong quantity, and an undercharge — a negative difference." |
| A3 | Point at the `Seed check results` block | The four `[PASS]` lines and the final `Seed comparison: PASSED` line | "Each of these four results was checked against a hand-calculated answer key stored in the repo, not against anything the program produced itself. All four match exactly." | *"Why only four, if six invoices ran?"* → "The answer key covers the four cases the original brief specifies. The other two — quantity overbill and undercharge — are cases I added myself because the brief's quantity rule had nothing to test it against; they're checked by the automated test suite instead, not this file." |
| A4 **[cut if short]** | Run `echo $?` | The printed `0` | "Exit code zero. A non-zero code here would mean either the rules disagreed with the answer key, or an invoice failed to process — those are reported as two different codes on purpose, so one problem never hides the other." | *"What's the difference between those two problems?"* → "One says the model or pipeline broke. The other says the result is wrong even though nothing broke. They need different people to look at them." |

**Confirmed output (this run):**

```
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

---

## B. The reviewer UI (5 minutes)

Proves the web page reads and writes the same database the batch command
just filled, and that a correction recalculates the result in place.

Run table A first — the web page needs the database the batch command
just built. Do not pass `--db-path`; the batch command already wrote to
the default `invoice_reconciliation.sqlite` in the repository root.

| Step | What you do | What to point at on screen | What to say | If asked |
| --- | --- | --- | --- | --- |
| B1 | Run:<br>`uv run uvicorn invoice_reconciliation.web.app:create_app --factory` | The `Uvicorn running on http://127.0.0.1:8000` line | "This starts the review page against the same database file the batch command just wrote." | *"Why `--factory`?"* → "The app builds its database path at startup rather than import time, so the same code can point at a test database in the test suite and the real one here. `--factory` tells uvicorn to call it as a function, not import it as a ready-made object." |
| B2 | Open `http://127.0.0.1:8000/invoices` | The queue table: six rows, each with a status, expected amount, billed amount, difference | "This is the queue. Every invoice the batch just processed, with its status and figures, in one list." | — |
| B3 | Click the "discrepant" filter link | The table narrowing to three rows | "Filtering by status is a plain link, not a client-side script — this whole product is server-rendered pages, no JavaScript framework." | *"Why no frontend framework?"* → "There's no interactivity this product needs that a server-rendered form and a link can't do. Adding one would be a dependency with no job to do." |
| B4 | Click into the wrong-price invoice (status `discrepant`, difference $20.00) | The invoice image on the left, the matched purchase order and receipt, and the seven extracted fields with their correction forms | "Here's the evidence: the invoice image, the purchase order and receipt it matched against, and the field the model extracted — in this case, `total_cents` was read as 12000, a hundred and twenty dollars, one hundred cents too high against the agreed price." | *"How do you know the extraction is reading the fields correctly?"* → "You can see the image right next to the read value and check it yourself. That's the point of putting them side by side." |
| B5 | In the `total_cents` correction form, enter `10000` and submit | The page reloading, the status badge changing from `discrepant` to `reconciled`, and the `total_cents` row showing `12000` under "original" and `10000` under "current" | "I've corrected the billed amount to match what the purchase order agreed. The status just moved from discrepant to reconciled — nothing else changed, the same reconciliation function the batch command used just ran again on this one invoice." | *"Why does the original value still say 12000?"* → "That's what extraction actually produced. A correction records a new current value; it never overwrites the history of what the model first read." |
| B6 | Open `http://127.0.0.1:8000/summary` | The "Recoverable total" figure, and the "Source" column of the discrepant invoices | "The recoverable total just moved from eighty dollars to sixty, because this invoice no longer owes anything. Each remaining difference says where it comes from: quantity-overbill on quantity, undercharge on unit price." | *"Why eighty and not some other number?"* → "It's the sum of the two overcharges, twenty plus sixty — the quantity overbill is also an overcharge. The undercharge is excluded on purpose; see edge case C3." |
| B6a **[cut if short]** | Scroll to "Suggested improvements" | The line "No issue recurs yet", and the two "Single case" entries | "Before the correction, the unit price differed on two invoices, so the page showed it as a recurring issue. After the correction, every issue type occurs once, and the page says so instead of claiming a pattern. Code counts the issue types; one model call only phrases them, and code fixes the conclusion — for example, recheck unit prices against the purchase order before shipping." | *"Is the model deciding what the problem is?"* → "No. Code ranks the issue types by count from the saved results. The model writes one plain sentence per issue, and a verifier rejects blame words, dollar figures and wrong invoice names. Hover the ⓘ icon to see whether the model or the prepared fallback wrote it." |
| B7 **[cut if short]** | Stop the server (`Ctrl-C` or kill by PID), then start it again with the same command | The queue and the detail page loading again with no errors | "I've just killed the whole server process and started a brand-new one. No memory carried over — only the database file did." | — |
| B8 **[cut if short]** | Open `/invoices/2` again in the new process | The status still `reconciled`, `total_cents` still showing `10000` as current | "The correction is still there. It's not a server-side session value — it was written to the database the moment I submitted it." | *"What if two reviewers correct the same invoice at once?"* → "Each write is one transaction — a correction row and a field update together, committed or rolled back as one unit. There's no login system in this product, so every correction is attributed to a fixed placeholder reviewer rather than a real identity; that's a recorded limitation, not an oversight." |

**Confirmed in this run:**

- `/invoices`, `/invoices/2`, `/summary` all returned HTTP 200.
- Before correction: invoice 2 (`wrong-price`) showed `total_cents` original
  `12000`, current `12000`, status `discrepant`, recoverable total `$80.00`.
- `POST /invoices/2/fields/total_cents` with `value=10000` returned `303 See
  Other`.
- After correction: status `reconciled`; `total_cents` original `12000`,
  current `10000`; recoverable total `$60.00`.
- Summary before the correction: "Unit price differs · Recurring, 2
  invoices" (wrong-price, undercharge). After it: "No issue recurs yet",
  then "Unit price differs · Single case" (undercharge) and "Quantity
  differs · Single case" (quantity-overbill). The Source column shows
  quantity-overbill `Quantity` and undercharge `Unit price`.
- Server stopped by PID, a brand-new server process started against the
  same database file, invoice 2 read back as `reconciled` with
  `total_cents` current still `10000` — no server state carried over, only
  the file on disk.

---

## C. Failure and edge cases (3 minutes)

| Step | What you do | What to point at on screen | What to say | If asked |
| --- | --- | --- | --- | --- |
| C1 | Open `/invoices/4` (`missing-reference`, status `unresolved`) | The seven extracted fields (all present), and the "no matched purchase order" message where a match would normally show | "This invoice has no purchase-order reference printed on it anywhere. Every field the model could read is shown — nothing is missing on the extraction side — but there's nothing to match it against, so it's `unresolved`, not reconciled and not guessed at." | *"Why not just match it on the amount?"* → "Two purchase orders in this batch agree on supplier, SKU, quantity, and price, and differ only by their ID. Matching on amount alone would pick one of them at random and could pick the wrong one. Matching requires supplier ID and purchase-order ID together, so an invoice with no usable reference stays unresolved instead of getting an invented match." |
| C2 | Open `/invoices/3` (`duplicate`, `count_as_payable=False`) | The status badge `duplicate`, and the `/summary` page showing it counted but not in the recoverable total | "Two invoices in this batch share the same supplier and invoice number. The first one received counts as payable; this one is flagged a duplicate and excluded." | *"How do you decide which copy is the real one?"* → "Whichever was received first, by timestamp. That's the only ordering rule available once two invoices agree on supplier and invoice number." |
| C3 | Open `/invoices/6` (`undercharge`, difference **−$10.00**) and then `/summary` | The negative difference on the detail page; the recoverable total on the summary page unchanged by this invoice | "This invoice billed ten dollars less than it should have. It's flagged discrepant — the difference is real and worth a reviewer's attention — but it does not reduce the eighty-dollar recoverable total. An underbill must never offset an overcharge; they're two separate problems, not a number to net against each other." | *"Isn't that leaving real savings on the table?"* → "No — the ten dollars isn't lost, it's a separate fact: this supplier undercharged once. Netting it against a different invoice's overcharge would hide both problems behind one smaller number." |
| C4 **[cut if short]** | Run:<br>`uv run pytest tests/integration/test_failure_isolation.py -v` | The five `PASSED` lines, in particular `test_a_corrupt_cache_entry_leaves_every_other_invoice_processed` | "This test corrupts one invoice's saved response — malformed JSON — and proves the other five still process correctly, and the corrupted one comes back `failed` with a readable reason instead of crashing the batch. I'm running it as a test here rather than live in the CLI, because the batch command doesn't expose a flag to point at a scratch cache — that's a real gap, not a dodge." | *"Why not show it in the CLI directly?"* → "The CLI's cache location isn't a command-line option today. Showing it as a passing test is the honest way to demonstrate the behavior without editing a tracked file on stage." |

| C5 **[cut if short]** | Run, with every AWS credential blocked and a scratch database:<br>`AWS_PROFILE=nope AWS_EC2_METADATA_DISABLED=true AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null uv run python -m invoice_reconciliation.cli --extract --reset-db --no-model-notes --db-path /tmp/no-credentials.sqlite`, then `echo $?` | Six `FAILED — ModelCallError: could not build the Bedrock client: ProfileNotFound` lines, `status=failed` for all six, and exit code `2` | "This time I asked for live extraction with no usable AWS profile. No model call can work, but the batch does not stop: each invoice is saved as failed with the reason, and the exit code says a processing problem, not a wrong answer." | *"What about a throttle on just one call?"* → "Same path. The client turns any Bedrock or network error into one error type, and ingest catches it per invoice. A test throttles only one invoice and checks the other five still extract." |

**Confirmed in this run:**

```
tests/integration/test_failure_isolation.py::test_a_corrupt_cache_entry_leaves_every_other_invoice_processed PASSED
tests/integration/test_failure_isolation.py::test_a_corrupt_cache_entry_carries_no_amounts_and_a_readable_reason PASSED
tests/integration/test_failure_isolation.py::test_an_absent_cache_entry_leaves_every_other_invoice_processed PASSED
tests/integration/test_failure_isolation.py::test_an_absent_cache_entry_carries_no_amounts_and_a_readable_reason PASSED
tests/integration/test_failure_isolation.py::test_failed_invoice_never_reports_as_unresolved_in_the_same_run PASSED

5 passed in 0.11s
```

---

## If something breaks

| Symptom | Likely cause | Recovery |
| --- | --- | --- |
| The headless command (A1) tries to reach AWS and hangs or errors | `--from-cache` was left off, or typed as `--extract` by habit | Stop it (`Ctrl-C`), re-run the exact command in A1 — `--from-cache`, not `--extract`. The four `AWS_*` variables only block credentials; they do not change which code path runs. |
| The web page (B2) shows an empty queue or a 404 | The batch command was never run first, or was run with a different `--db-path` | Run A1 again with no `--db-path` override, so it writes to the default `invoice_reconciliation.sqlite`, then restart the server. |
| `uvicorn: command not found` or an import error on startup | Dependencies not installed, or run outside `uv run` | Run `uv sync` once, then re-run the exact `uv run uvicorn ...` command from B1 — do not drop `uv run` or the `--factory` flag. |
| A correction (B5) returns a 422 error instead of redirecting | The typed value was not a plain integer (e.g. `$100.00` or `100.00` instead of `10000`) | `total_cents` and `unit_cents` take a whole number of cents, not a dollar string. Re-type as `10000`, not `100.00`. |
| Two uvicorn processes are now both bound to port 8000 | The old server (B7) was not actually stopped before starting the new one | Find both with `lsof -i :8000`, stop each by PID, then start one fresh instance. |

---

## Q&A reference

Short answers for likely questions not already placed above.

**Why no LangGraph or other agent framework?**
The flow is linear: ingest, extract, reconcile, persist. A graph runtime
would add a dependency without removing any work. There's no branching
agent behavior to coordinate, and no checkpointer to manage — durable
state is just the SQLite file itself.

**How do you know the arithmetic is right?**
Every money value is stored as a whole number of integer USD cents, start
to finish — never a float. One function converts dollars to cents; every
other module calls it instead of doing its own math. The answer keys in
`tasks/invoices/expected-seed-results.json` and
`expected-new-fixtures.json` were calculated by hand and stored separately
from anything the program produces, so the check in table A is a
comparison against an independent source, not the program checking its
own work.

**What if the model returns something unexpected?**
Money fields are typed as strings in the extraction schema, but the model
still returns a plain JSON number for one call in roughly three, measured
across real runs. The cents converter accepts a string or a number for
this reason. Both defenses are needed — the schema alone does not stop it,
and the converter alone would be undocumented if the schema did not also
say what's expected.

**How is this reproducible without your AWS account?**
Every real Bedrock response used in this demo is committed to the repo
under `tests/fixtures/bedrock_responses/`. `--from-cache` replays them
with no network call. The AWS SDK import itself is deferred until a live
call is actually requested, so `--from-cache` never touches it at all.

**What would you do next with more time?**
Add a `--cache-dir` flag to the CLI, so the corrupted-cache edge case
(table C, row 4) can be demonstrated live instead of only by test. Add a
real reviewer identity in place of the fixed placeholder, now that
corrections are attributed to someone. Extend the quantity-mismatch rule
to a second fixture with a different billed quantity, so it's tested
against more than one shape of overbill.

**Which part are you least confident about?**
The duplicate-detection ordering rule. It orders by `received_at` to
decide which of two matching invoices counts as the original, because the
brief names supplier ID and invoice number as the match key but says
nothing about which copy should be the one that gets paid. That's a
reasonable default, not a rule the brief actually specifies — it's
recorded as a settled ambiguity in `README.md`, not asserted as obviously
correct.

---

## Cross-reference

- Full flag list and exit codes: `README.md`, "All CLI flags".
- Settled ambiguities (eleven, numbered): `README.md`, "Settled ambiguities".
- Six defects found during the build: `README.md`, "Six defects found
  during the build" — the Bedrock-Mantle one (`AnthropicBedrockMantle`
  returning 404 against a service with its own, separate entitlement) is
  the one most worth telling if asked which defect mattered most; it shows
  a measured assumption overturned by testing, not by inspection.
- Domain rules as implemented: `tasks/invoices/domain.md`.

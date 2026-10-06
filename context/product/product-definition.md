# Product Definition: Invoice Reconciliation

- **Version:** 1.0
- **Status:** Proposed

> **Source note:** Sections below are drafted from the supplied exercise fixtures
> (`tasks/invoices/domain.md`, `seed.json`, `expected-seed-results.json`, the invoice images) and
> from best-practice assumptions where the fixtures are silent. Assumption-derived content is
> labelled **[Assumption]** so it can be confirmed or corrected. Full exploration notes live in
> `context/product/brownfield.md`.

---

## 1. The Big Picture (The "Why")

### 1.1. Project Vision & Purpose

Suppliers bill for goods that were ordered and received, but the three records — the purchase order,
the receipt, and the invoice — do not always agree. Checking them by hand is slow, and the errors
that cost real money (an inflated unit price, the same invoice submitted twice) are exactly the ones
that look unremarkable on a single page.

The goal is a system that ingests supplier invoices as documents, extracts the billed fields, matches
each invoice to its purchase order and receipt, and classifies it as **reconciled**, **discrepant**,
**duplicate**, or **unresolved** — showing a reviewer the exact overcharge in each case, and never
inventing a recoverable amount it cannot defend.

The north star ⭐: **a reviewer trusts the output enough to act on it, because every number is traced
back to the documents it came from.**

### 1.2. Target Audience

An **accounts-payable reviewer** who receives supplier invoices and must decide which to pay, which
to query, and which to reject before payment goes out. They know the commercial rules but are not
engineers; they need the system's reasoning visible, not hidden.

A secondary audience is the **reviewer of this exercise**, who must be able to re-run the supplied
seed cases and confirm the system reproduces `expected-seed-results.json` exactly.

### 1.3. User Personas

- **Persona 1: "Priya the AP Reviewer"**
  - **Role:** Accounts-payable specialist at a mid-sized distributor.
  - **Goal:** Clear the day's invoice queue confidently — pay the clean ones, flag the overcharges
    with a precise figure to quote back to the supplier, and catch the duplicates before they are
    paid twice.
  - **Frustration:** Comparing three documents field by field is tedious, and near-identical purchase
    orders make it easy to match an invoice to the wrong one. Tools that "auto-approve" without
    showing their work are worse than useless, because an incorrect match is more expensive than no
    match at all.

- **Persona 2: "Marco the Reviewing Engineer"** **[Assumption]**
  - **Role:** Evaluates the delivered system.
  - **Goal:** Run one command, see the seed cases reproduce the expected results, and read how each
    classification was reached.
  - **Frustration:** Systems whose results cannot be reproduced, or whose domain rules quietly differ
    from the ones specified.

### 1.4. Success Metrics

- **Correctness on the known cases:** the four seed invoices classify exactly as
  `expected-seed-results.json` specifies — `clean` → reconciled (0), `wrong-price` → discrepant
  (2,000 cents), `duplicate` → duplicate (not payable), `missing-reference` → unresolved (no
  computed amount).
- **Correct extraction across layouts:** fields are read correctly from both invoice layouts,
  including the two-column layout `b`, rather than from fixed positions on the page.
- **No fabricated recoveries:** duplicates and unresolved invoices are never included in any
  recoverable total. A reported recoverable figure consists only of genuine overcharges.
- **Corrections flow through:** when a reviewer corrects a mis-extracted field, the classification
  and discrepancy recalculate from the corrected value, and the change is recorded.
- **Explainability:** for every invoice, the web view shows which purchase order and receipt it
  matched, what was expected, what was billed, and why the status was assigned — with the invoice
  image available to check an extracted value against.
- **Reproducibility:** a reviewer can re-run the seed set from the fixtures with a single documented
  command and get identical results.

---

## 2. The Product Experience (The "What")

### 2.1. Core Features

- **Invoice intake and field extraction** — read supplier invoices supplied as images and extract
  invoice number, supplier, purchase-order reference, SKU, quantity, unit price, and total,
  tolerating differing layouts.
- **Matching against purchase orders and receipts** — join on supplier ID *and* purchase-order ID;
  where the reference is missing or conflicting, stop and mark the invoice unresolved rather than
  guessing from the amount.
- **Discrepancy calculation** — expected amount = ordered quantity x agreed unit price; compare
  billed quantity against both ordered and received quantity; report billed minus expected in
  integer USD cents, a positive result being an overcharge.
- **Duplicate detection** — identify repeat submissions by supplier ID and invoice number, and
  exclude them from payable and recoverable totals.
- **Review and correction (web view)** — a browser-based invoice queue showing each invoice with its
  status, the figures behind it, and the purchase order and receipt it matched, with the invoice
  image viewable alongside; the reviewer corrects a mis-extracted field inline and the result
  recalculates.
- **Reconciliation summary** — a per-run view of invoice counts by status and the genuine
  recoverable overcharge, with duplicates and unresolved items reported separately and never
  summed in. **[Assumption]**

### 2.2. User Journey

1. Priya loads a batch of supplier invoices (the supplied images, plus any added cases) together
   with the current purchase-order and receipt records.
2. The system extracts the billed fields from each invoice document.
3. Each invoice is matched to its purchase order and receipt by supplier and purchase-order
   reference.
4. Each invoice is classified: **reconciled** where billed equals expected; **discrepant** with the
   exact cent difference where it does not; **duplicate** where the supplier and invoice number have
   already been seen; **unresolved** where the reference is missing or conflicting.
5. Priya opens the queue in her browser. For the discrepant invoice she sees billed 12,000 against
   expected 10,000 and a 2,000-cent overcharge to raise with the supplier, alongside the purchase
   order and receipt it matched. For the unresolved invoice she sees that no purchase order could be
   identified — not a guess.
6. Where a field was read incorrectly, Priya opens the invoice detail, compares the extracted value
   against the invoice image shown beside it, and corrects the field inline; the expected amount, the
   difference, and the status recalculate from the corrected value, and the correction is recorded.
7. She finishes with a summary: what is clean and payable, what is genuinely recoverable, what is a
   duplicate to suppress, and what needs a reference before it can be judged.

---

## 3. Project Boundaries

### 3.1. What's In-Scope for this Version

- Ingesting supplier invoices supplied as images, in both provided layouts.
- Extracting invoice number, supplier ID, purchase-order ID, SKU, quantity, unit price, and total.
- Matching invoices to purchase orders and receipts on supplier ID plus purchase-order ID.
- The four statuses — reconciled, discrepant, duplicate, unresolved — computed per the supplied
  rules, in integer USD cents.
- Comparing billed quantity against both ordered and received quantity.
- A **web-based reviewer interface**: an invoice queue, per-invoice detail showing the matched
  purchase order and receipt and the invoice image, and inline correction of an extracted field with
  recalculation and a record of what changed.
- Reproducing the supplied seed cases against `expected-seed-results.json`, with the seed cases
  retained so a reviewer can run the same checks.
- Extending the fixture set with two to four additional invoices under the same rules, including one
  field-correction case and its expected recalculation.
- A summary view separating genuine recoverable overcharges from duplicates and unresolved items.
- Documenting any unresolved domain ambiguity rather than silently inventing a rule to cover it.

### 3.2. What's Out-of-Scope (Non-Goals)

Explicitly excluded by the supplied rules:

- Tax handling.
- Partial billing.
- Foreign exchange and multiple currencies.
- Payment terms.
- Totalling duplicates or unresolved matches as recoverable amounts.
- Adding domain rules beyond those specified.

Out of scope for this version **[Assumption]**:

- Supplier-facing features — portals, dispute workflows, automated emails to suppliers.
- Executing or scheduling payments.
- Integration with live ERP or accounting systems; the purchase-order and receipt records come from
  the supplied fixtures.
- User accounts, authentication, and role-based permissions.
- Multi-line invoices beyond the single-line structure of the supplied fixtures — to be confirmed,
  as the fixtures contain only one line item each.
- A mobile application.
- Machine-learned matching or anomaly detection; matching follows the stated deterministic rules.

---

## 4. Open Questions

Recorded rather than silently resolved, per `tasks/invoices/domain.md` ("Record any unresolved
ambiguity in your README. Do not silently add domain rules.").

1. **Multi-line invoices.** Every supplied invoice has a single line item. Whether the system must
   handle invoices with several lines — and how a per-line discrepancy rolls up to an invoice status
   — is unspecified. Current assumption: single line item, matching the fixtures.
2. **Quantity mismatches.** The rules require comparing billed quantity against both ordered and
   received quantity, but the seed data contains no case where they differ, and
   `expected-seed-results.json` shows no distinct status for it. Current assumption: a quantity
   mismatch produces a `discrepant` status with the difference computed from the agreed unit price.
3. **Which copy of a duplicate is payable.** The rules identify duplicates by supplier and invoice
   number but do not state whether the first-seen copy remains payable. Current assumption: the
   first occurrence is judged on its merits and subsequent copies are marked `duplicate` and
   excluded.
4. **Undercharges.** A positive difference is an overcharge; the treatment of a negative difference
   (supplier billing less than agreed) is not stated. Current assumption: reported as `discrepant`
   with a negative difference, and excluded from recoverable totals.
5. ~~**Interface shape.**~~ **Settled:** the reviewer experience is a **web view** — an invoice queue
   with per-invoice detail (matched purchase order and receipt, expected vs billed figures, the
   invoice image) and inline field correction. The seed-case check must still be runnable
   reproducibly for a reviewer; how that is exposed alongside the web view is an architecture
   concern.

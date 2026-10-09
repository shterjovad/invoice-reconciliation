# Product Definition: Invoice Reconciliation

- **Version:** 1.1
- **Status:** Proposed

> **Source note:** This document comes from two sources. The first is the supplied material in
> `tasks/invoices/`: `domain.md`, `seed.json`, `expected-seed-results.json` and the invoice images.
> The second is the assignment brief for the invoice reconciliation platform, section
> "Alternative B". The brief has authority where the two disagree. A label **[Assumption]** marks
> each item that comes from neither source.

---

## 1. The Big Picture (The "Why")

### 1.1. Project Vision and Purpose

Suppliers bill a business for goods. Three records describe each purchase: the purchase order, the
receipt and the invoice. These three records do not always agree.

A person who compares them by hand works slowly. The errors that cost money are small ones. A unit
price rises above the agreed price. The same invoice arrives twice. Neither error looks unusual on
the page.

This product reads supplier invoices as documents. It finds the billed values. It matches each
invoice to its purchase order and its receipt. Then it gives the invoice one of four statuses:
**reconciled**, **discrepant**, **duplicate** or **unresolved**. It shows the reviewer the exact
overcharge. It never reports money that it cannot support.

The goal: **the reviewer trusts the result and acts on it, because each number points back to its
source document.**

### 1.2. Target Audience

The user is an **accounts-payable reviewer**. This person receives supplier invoices. They decide
which invoices to pay, which to question and which to refuse. They know the commercial rules. They
are not engineers. They must see how the product reached each result.

A second audience is the **assessor of this exercise**. This person must run the supplied cases
again and confirm that the product gives the results in `expected-seed-results.json`.

### 1.3. User Personas

- **Persona 1: "Priya the AP Reviewer"**
  - **Role:** accounts-payable specialist at a medium-sized distributor.
  - **Goal:** clear the invoice queue each day. Pay the correct invoices. Mark each overcharge with
    a precise figure for the supplier. Find the duplicates before anyone pays them twice.
  - **Problem:** a comparison of three documents, value by value, takes a long time. Two purchase
    orders look almost the same, so it is easy to match an invoice to the wrong one. A tool that
    approves invoices without evidence is worse than no tool. A wrong match costs more than no
    match.

- **Persona 2: "Marco the Assessor"** **[Assumption]**
  - **Role:** assesses the delivered product.
  - **Goal:** run one command. See the supplied cases give the expected results. Read how the
    product reached each status.
  - **Problem:** products that give different results on each run. Products whose rules differ from
    the supplied rules.

### 1.4. Success Metrics

- **Correct results on the known cases.** The four supplied invoices get the exact statuses in
  `expected-seed-results.json`: `clean` reconciles at 0; `wrong-price` is discrepant at 2,000 cents;
  `duplicate` is a duplicate and is not payable; `missing-reference` stays unresolved with no
  amount.
- **Correct reading of both layouts.** The product reads the values correctly from both layouts.
  This includes the two-column layout `b`. It does not read from fixed positions on the page.
- **No invented recoveries.** The recoverable total holds only true overcharges. It never holds
  duplicates or unresolved invoices.
- **Corrections take effect.** When the reviewer corrects a value, the product calculates the status
  and the difference again. It records the change.
- **Visible reasoning.** For each invoice, the web view shows the matched purchase order and
  receipt, the expected amount, the billed amount and the reason for the status. The invoice image
  stays available for a comparison.
- **Repeatable results.** The assessor runs one documented command and gets the same results.
- **Operation without credentials.** The assessor can run the full flow from saved model responses,
  with no key and no account.

---

## 2. The Product Experience (The "What")

### 2.1. Core Features

- **Invoice intake and value extraction.** The product reads supplier invoices that arrive as
  images. It finds the invoice number, the supplier, the purchase-order reference, the SKU, the
  quantity, the unit price and the total. It reads both layouts.
- **Matching to purchase orders and receipts.** The product matches on the supplier and the
  purchase-order reference together. If the reference is absent or wrong, the product stops and
  marks the invoice unresolved. It does not guess from the amount.
- **Difference calculation.** The expected amount is the ordered quantity multiplied by the agreed
  unit price. The product compares the billed quantity with the ordered quantity and the received
  quantity. It reports the billed amount minus the expected amount, in integer USD cents. A positive
  result is an overcharge.
- **Duplicate detection.** The product finds repeated invoices by the supplier and the invoice
  number. It keeps them out of the payable totals and the recoverable totals.
- **Review and correction in a web view.** The web view lists the invoices in a queue with a status
  filter. For one invoice it shows the status, the figures, the matched purchase order, the matched
  receipt and the invoice image. The reviewer corrects a value in place, and the product calculates
  the result again.
- **Drafted note about a difference.** For a discrepant invoice, the product drafts a short note. The
  note states the difference and names its source records. The note stays a draft.
- **Batch summary.** The summary gives the count of invoices in each status and the recoverable
  overcharge. It reports duplicates and unresolved invoices apart from that total. It also gives one
  repeated problem or one improvement, with the invoices that support it.
- **Operation after a failure.** One unreadable invoice does not stop the batch. A failure of the
  model does not stop it either. The queue shows each failed invoice with a clear status.

### 2.2. User Journey

1. Priya loads a batch of supplier invoices with the current purchase orders and receipts.
2. The product reads the billed values from each invoice document.
3. The product matches each invoice to its purchase order and receipt by supplier and reference.
4. The product gives each invoice a status. **Reconciled** means the billed amount equals the
   expected amount. **Discrepant** means they differ, and the product gives the exact difference.
   **Duplicate** means the supplier and invoice number appeared before. **Unresolved** means the
   reference is absent or wrong.
5. Priya opens the queue in her browser. On the discrepant invoice she sees billed 12,000 against
   expected 10,000, and an overcharge of 2,000 cents for the supplier. She also sees the matched
   purchase order and receipt. On the unresolved invoice she sees that the product found no purchase
   order. It did not guess.
6. Where the product read a value incorrectly, Priya opens the invoice. She compares the value with
   the image next to it. She corrects the value. The product calculates the expected amount, the
   difference and the status again, and records the correction.
7. For a discrepant invoice, Priya reads the draft note and edits it for the supplier.
8. Priya ends with the summary. It shows the payable invoices, the money to recover, the duplicates
   to stop and the invoices that need a reference.

---

## 3. Project Boundaries

### 3.1. In Scope for this Version

- Reading supplier invoices that arrive as images, in both supplied layouts.
- Finding the invoice number, supplier, purchase-order reference, SKU, quantity, unit price and
  total.
- Matching invoices to purchase orders and receipts on the supplier and the purchase-order
  reference.
- The four statuses, in integer USD cents, under the supplied rules.
- A comparison of the billed quantity with the ordered quantity and the received quantity.
- A **web view** for the reviewer: a queue with a status filter, a detail view with the matched
  records and the invoice image, and correction of a value with a new calculation and a record of
  the change.
- A drafted note about each difference that names its source records.
- A batch summary that separates true overcharges from duplicates and unresolved invoices, and that
  gives one improvement.
- Operation after a failure: one bad invoice or one model failure does not stop the batch, and the
  failures stay visible.
- Six to eight invoices in the working set. The four supplied invoices stay unchanged. The set adds
  an invoice that bills too many units, an invoice that bills too little, and a case that needs a
  correction.
- Five or more reference cases with answers calculated in advance and stored apart from the output
  of the product.
- A check that runs the supplied cases again and reproduces `expected-seed-results.json`.
- Replay of saved model responses, with no credentials.
- A written record of each ambiguity in the supplied rules.

### 3.2. Out of Scope (Non-Goals)

The supplied rules exclude these:

- Tax.
- Part billing.
- Foreign exchange and more than one currency.
- Payment terms.
- Duplicates and unresolved invoices in a recoverable total.
- Rules beyond the supplied rules.

The brief marks these as optional, and this version excludes them:

- Invoices as PDF files.
- Export of the queue and of the calculations.
- A history of reviewer decisions and note versions.

This version also excludes these **[Assumption]**:

- Features for the supplier: portals, dispute processes and automatic messages.
- Payments, and the schedule of payments.
- Connections to live ERP systems and accounting systems. The purchase orders and receipts come
  from the supplied files.
- User accounts, sign-in and permissions.
- Invoices that bill more than one item. Each invoice bills one item.
- Operation on a server. The product runs on the machine of the reviewer.
- Machine learning for the matching. The product follows the stated rules.

---

## 4. Recorded Ambiguities

The supplied rules leave four points open. `tasks/invoices/domain.md` says: "Record any unresolved
ambiguity in your README. Do not silently add domain rules." This project therefore does two things
for each point. It defines the behaviour, so that the behaviour is testable. It also records the
gap. The functional specification at `context/spec/001-invoice-reconciliation-review/` holds the
full entries.

1. **Invoices with more than one item.** Each supplied invoice bills one item. The product supports
   one item per invoice.
2. **Quantity differences.** The rules ask for the comparison, but no supplied case has different
   quantities. The product gives the status `discrepant` and calculates the difference at the agreed
   unit price. A new example invoice tests this.
3. **The payable copy of a duplicate.** The product judges the first copy on its own figures and
   marks later copies as duplicates. The supplied data demonstrates this.
4. **A supplier that bills too little.** The product gives the status `discrepant` with a negative
   difference, and excludes it from the recoverable total. A new example invoice tests this.

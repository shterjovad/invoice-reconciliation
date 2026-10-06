# Functional Specification: Invoice Reconciliation and Review

- **Roadmap Item:** Phase 1 and Phase 3 together. This covers the full reviewer flow, from invoice documents to a queue the reviewer can correct.
- **Status:** Draft
- **Author:** shterjovad

---

## 1. Overview and Rationale (The "Why")

A business gets invoices from its suppliers. Each invoice must agree with two other records. A purchase order records what the business agreed to buy. A receipt records what the supplier delivered.

When the three records disagree, the business can pay too much. It can also pay the same bill twice.

A person who checks this by hand works slowly and makes mistakes. The errors that cost money are small. A unit price is a little above the agreed price. A second copy of a paid invoice arrives. Neither error looks unusual on the page.

This feature gives the reviewer a queue of supplier invoices. For each invoice, the platform shows four things:

- what the supplier billed
- what the business agreed to pay
- the records that these figures come from
- the exact difference between the two amounts

If the platform cannot identify the purchase order, it says so. It does not guess.

Two principles control every decision in this specification. Both keep the reviewer's trust in the result:

- **The platform does not invent a figure that it cannot support.** If the platform cannot match an invoice, it shows the invoice as unresolved. It does not attach the invoice to an order that looks correct.
- **The recoverable amount is money that the business can pursue.** The platform reports duplicate invoices and unresolved invoices apart from this amount. It does not add them to it.

**Success means three things.** The reviewer can work through a batch and see the status, the figures and the source records for every invoice. The four supplied example invoices give the exact results that someone recorded in advance. The reviewer corrects a value that the platform read incorrectly, and the new result stays correct after a restart.

---

## 2. Functional Requirements (The "What")

### 2.1 Read invoices and match them to records

- **As a** reviewer, **I want** the platform to read each invoice and find its purchase order and receipt, **so that** I can compare the billed amount with the agreed amount.
  - The platform reads invoices that arrive as images. It finds seven values: the invoice number, the supplier, the purchase-order reference, the item, the quantity, the unit price and the total.
  - Invoices arrive in more than one layout. The platform reads all layouts correctly.
  - The platform matches the purchase order on **both** the supplier and the purchase-order reference. It never matches on the amount alone. Two of the orders are the same, except for their reference.
  - The platform keeps the purchase order and the receipt that it used for each match. The reviewer can then see the basis of the match.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens an invoice that the platform read correctly, then they see the invoice number, supplier, purchase-order reference, item, quantity, unit price and total from that invoice.
    - [ ] When the reviewer opens an invoice in either of the two supplied layouts, then they see all seven values filled in correctly.
    - [ ] When the reviewer opens a matched invoice, then they see the reference of the purchase order and the reference of the receipt that the platform matched.
    - [ ] When the reviewer opens an invoice whose reference points to one of two almost identical orders, then they see the platform match the order with that exact reference.

### 2.2 Give each invoice a status

- **As a** reviewer, **I want** a clear status on every invoice, **so that** I can see which invoices need my attention.
  - Each invoice has one status of four: **reconciled**, **discrepant**, **duplicate** or **unresolved**.
  - An invoice is **reconciled** when the billed amount equals the expected amount.
  - An invoice is **discrepant** when the billed amount differs from the expected amount.
  - An invoice is a **duplicate** when the same supplier sent an earlier invoice with the same invoice number.
  - An invoice is **unresolved** when the purchase-order reference is absent, or when it disagrees with the records.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens the queue, then they see exactly one status on each invoice: reconciled, discrepant, duplicate or unresolved.
    - [ ] When the reviewer opens an invoice that bills the agreed amount, then they see the status reconciled.
    - [ ] When the reviewer opens an invoice that bills more than the agreed amount, then they see the status discrepant.
    - [ ] When the reviewer opens a second invoice with the same supplier and invoice number as an earlier one, then they see the status duplicate.
    - [ ] When the reviewer opens an invoice with no purchase-order reference, then they see the status unresolved.

### 2.3 Calculate the difference

- **As a** reviewer, **I want** the exact difference on each discrepant invoice, **so that** I can give the supplier a precise figure.
  - The expected amount is the ordered quantity multiplied by the agreed unit price.
  - The difference is the billed amount minus the expected amount. A positive difference shows that the supplier billed too much.
  - The platform compares the billed quantity with the ordered quantity and with the received quantity.
  - The platform shows all amounts in US dollars, exact to the cent.
  - The platform ignores tax, part shipments, foreign currency and payment terms.
  - **The rounding rule:** the platform holds every amount as a whole number of cents. It converts
    each amount that it reads into cents once, and it keeps that value. It therefore never rounds
    during a calculation, and no result changes by a cent. The documents record this rule.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens a discrepant invoice, then they see the billed amount, the expected amount and the difference, each exact to the cent.
    - [ ] When the reviewer opens the invoice that bills five units at $24.00 against an order of five units at $20.00, then they see billed $120.00, expected $100.00 and a difference of $20.00.
    - [ ] When the reviewer opens a reconciled invoice, then they see a difference of $0.00.
    - [ ] When the reviewer opens a matched invoice, then they see the billed quantity next to the ordered quantity and the received quantity.
    - [ ] When the reviewer opens an unresolved invoice, then they see no expected amount and no difference, in place of a zero or an estimate.

### 2.4 Keep the totals honest

- **As a** reviewer, **I want** the platform to keep duplicates and unresolved invoices out of the recoverable amount, **so that** I can defend the figure that I report.
  - The recoverable total contains only true overcharges on matched invoices.
  - The platform counts duplicate invoices and unresolved invoices apart from this total. It never adds them to it.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens the summary for a batch with one $20.00 overcharge, one duplicate and one unresolved invoice, then they see a recoverable total of $20.00.
    - [ ] When the reviewer opens the summary, then they see the duplicate count and the unresolved count apart from the recoverable total.
    - [ ] When the reviewer opens the summary, then they see that the count of invoices to pay does not include the duplicate.

### 2.5 Use the review queue

- **As a** reviewer, **I want** one queue that I can filter by status, **so that** I can find the exceptions quickly.
  - The queue shows each invoice in the batch with its status and its main figures.
  - The reviewer can filter the queue by status.
  - When the reviewer opens an invoice, the platform shows the invoice document, the values that it read, the matched purchase order, the matched receipt and the calculation.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens the queue, then they see each invoice of the batch with its status.
    - [ ] When the reviewer filters the queue by a status, then they see only the invoices with that status.
    - [ ] When the reviewer opens one invoice, then they see the invoice document next to the values that the platform read from it.
    - [ ] When the reviewer opens a matched invoice, then they see the purchase order, the receipt and the calculation of expected against billed on the same screen.

### 2.6 Correct a value

- **As a** reviewer, **I want** to correct a value that the platform read incorrectly, **so that** a reading error does not become a payment error.
  - The reviewer can change any value that the platform read from the invoice.
  - When the reviewer saves a correction, the platform calculates the expected amount, the difference and the status again. It uses the corrected value.
  - The platform continues to show the first value that it read. The reviewer can then understand the change.
  - The platform keeps corrections. They stay in place after a restart.

  - **Acceptance Criteria:**
    - [ ] When the reviewer changes a value and saves it, then they see a new expected amount, a new difference and a new status from the corrected value.
    - [ ] When the reviewer corrects the unit price on a discrepant invoice to the agreed price, then they see the status become reconciled and the difference become $0.00.
    - [ ] When the reviewer opens a corrected invoice, then they see the first value and the corrected value.
    - [ ] When the reviewer closes the platform, starts it again and opens a corrected invoice, then they see the correction and the new result.

### 2.7 Draft a note about a difference

- **As a** reviewer, **I want** a short draft note about each difference, **so that** I can start my message to the supplier.
  - For a discrepant invoice, the platform drafts a short note. The note describes the difference and names the records that support it.
  - The note stays a draft for the reviewer to edit. The platform does not send it.
  - The platform builds the note only from figures that it has checked. The note does not give a cause for the difference. It does not suggest a crime.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens a discrepant invoice, then they see a draft note with the billed amount, the expected amount and the difference.
    - [ ] When the reviewer reads the draft note, then they see the invoice number and the purchase-order reference that support it.
    - [ ] When the reviewer opens a draft note, then they see that they can edit it and that the platform gives no control to send it.

### 2.8 Read the batch summary

- **As a** reviewer, **I want** a summary of the batch, **so that** I can report the result without a manual count.
  - The summary shows the number of invoices in each status.
  - The summary describes the price differences and the quantity differences, with their amounts.
  - The platform gives one repeated problem or one improvement from the batch. It names the invoices that support it.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens the summary, then they see a count of invoices for each of the four statuses.
    - [ ] When the reviewer opens the summary, then they see each price difference and each quantity difference with its amount.
    - [ ] When the reviewer opens the summary, then they see one improvement with the invoices that support it.

### 2.9 Continue after a failure

- **As a** reviewer, **I want** one bad invoice to not stop the batch, **so that** I can work on the invoices that processed correctly.
  - If the platform cannot read an invoice, the other invoices continue to process. The same applies if the reading service does not answer.
  - The queue shows each invoice that failed, with a status that gives the reason. The platform does not remove it.
  - The platform gives no amount to a failed invoice. It does not estimate.

  - **Acceptance Criteria:**
    - [ ] When the platform cannot read one invoice in a batch, then the reviewer sees all the other invoices processed and in the queue.
    - [ ] When the platform cannot read an invoice, then the reviewer sees that invoice in the queue with a status that shows the failure.
    - [ ] When the reviewer opens an invoice that failed, then they see no expected amount and no difference, in place of a zero or an estimate.
    - [ ] When the reading service does not answer for a full batch, then the reviewer sees a message about the failure, in place of an empty queue.

### 2.10 Check what the reading service returns

- **As a** reviewer, **I want** the platform to check each set of values before it uses them, **so that** a bad reading becomes a visible failure and not a wrong figure.
  - The platform checks the shape of each result from the reading service. It confirms that all seven values are present and of the right kind.
  - If a result is incomplete, or if a value is not of the right kind, the platform marks that invoice as failed. It gives the invoice no amounts.
  - The platform keeps the identifier of each source document and each reference record. Each result can then point back to its origin.

  - **Acceptance Criteria:**
    - [ ] When the reading service returns an incomplete set of values, then the reviewer sees that invoice marked as failed with no amounts.
    - [ ] When the reading service returns a value of the wrong kind, then the reviewer sees that invoice marked as failed and the reason for the failure.
    - [ ] When the reviewer opens any invoice, then they see the name of its source document.
    - [ ] When the reviewer opens a matched invoice, then they see the references of the purchase order and the receipt that support the result.

### 2.11 Keep the review state

- **As a** reviewer, **I want** the platform to keep my work, **so that** I do not repeat it after a restart.
  - The platform saves the values that it read, the results, the corrections and the notes.
  - The platform keeps this state when it stops and starts again.
  - A reviewer can run the batch again without losing the earlier corrections.

  - **Acceptance Criteria:**
    - [ ] When the reviewer closes the platform and starts it again, then they see the same queue, the same statuses and the same figures.
    - [ ] When the reviewer corrects a value, closes the platform and starts it again, then they see the correction and the new result.
    - [ ] When the reviewer edits a draft note, closes the platform and starts it again, then they see the edited note.

### 2.12 Check the platform against known answers

- **As a** reviewer who assesses this platform, **I want** to run it against examples with known answers, **so that** I can confirm the results myself.
  - The reviewer can start a check at any time. The check compares the results of the platform with the answers that someone calculated in advance.
  - The check covers five or more reference cases. One case corrects a value that the platform read incorrectly, and gives the result that must follow.
  - The expected answers stay in a separate place from the output of the platform.
  - The check reports a clear result. It names each case that does not agree.
  - The reviewer can run the check without the reading service. The check then uses saved responses from earlier real calls.

  - **Acceptance Criteria:**
    - [ ] When the reviewer runs the check, then they see a result for each reference case that shows agreement or disagreement with the expected answer.
    - [ ] When the reviewer runs the check and all cases agree, then they see one clear pass result.
    - [ ] When a case does not agree, then the reviewer sees the name of the case, the expected answer and the answer from the platform.
    - [ ] When the reviewer runs the check without the reading service, then they see the check finish from saved responses and report the same results.

### 2.13 Test the chosen behaviour with examples

- **As a** reviewer who assesses this platform, **I want** examples that cover the undefined rules, **so that** the platform demonstrates its choices.
  - The working set holds six to eight invoices.
  - It keeps the four supplied examples without change: the correct invoice, the invoice with the wrong unit price, the second copy and the invoice with no purchase-order reference.
  - It adds an invoice that bills **more units than the business ordered and received**. This demonstrates the quantity comparison.
  - It adds an invoice that bills **less than the agreed amount**. This demonstrates the treatment of an underbill.
  - It adds an invoice with a value to correct, and the result that the correction must give.
  - Someone calculates the correct answer for each added invoice in advance. These answers stay apart from the output of the platform.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens the queue for the full working set, then they see six to eight invoices.
    - [ ] When the reviewer opens the four supplied invoices, then they see the results that someone recorded for them earlier.
    - [ ] When the reviewer opens the invoice that bills too many units, then they see the status discrepant and a difference at the agreed unit price.
    - [ ] When the reviewer opens the invoice that bills less than the agreed amount, then they see the status discrepant and a negative difference.
    - [ ] When the reviewer opens the summary for a batch with an underbilled invoice, then they see that the recoverable total does not include the negative difference.
    - [ ] When the reviewer compares the recorded answers with the output, then they see that the two stay in separate places.

### 2.14 Report the demonstration results

- **As a** reviewer who assesses this platform, **I want** one table of the required checks, **so that** I can see the expected behaviour and the real behaviour together.
  - The project gives a table of the five required checks from the brief.
  - For each check, the table gives the expected behaviour and the behaviour that the platform showed.
  - The table names each check that failed and gives the reason. The project does not hide a failure.

  - **Acceptance Criteria:**
    - [ ] When the reviewer opens the results table, then they see a row for each of the five required checks.
    - [ ] When the reviewer reads a row, then they see the expected behaviour next to the behaviour that the platform showed.
    - [ ] When a check fails, then the reviewer sees the row marked as failed with the reason.

---

## 3. Scope and Boundaries

### In-Scope

- Reading supplier invoices that arrive as images, in the two supplied layouts.
- Matching invoices to purchase orders and receipts on the supplier and the purchase-order reference.
- The four statuses: reconciled, discrepant, duplicate and unresolved. A failed invoice also gets a visible status.
- Expected amounts, billed amounts and differences, exact to the cent, in one currency.
- A comparison of the billed quantity with the ordered quantity and the received quantity.
- A review queue with a status filter. A detail view shows the document, the values read, the matched records and the calculation.
- Correction of a value, with a new calculation and storage across a restart. The first value stays visible.
- A draft note about a difference that names its source records.
- A batch summary with counts, differences and one improvement.
- Processing that continues after one failure.
- A check on the shape of each result from the reading service, before the platform uses it.
- Kept identifiers: each result points back to its source document and its reference records.
- Saved review state: the values, results, corrections and notes survive a restart.
- A rounding rule in the documents. The platform holds all money as whole cents and never rounds during a calculation.
- A check against five or more reference cases that runs without the reading service.
- A results table for the five required checks, with the expected behaviour and the real behaviour.
- A working set of six to eight invoices. It adds an invoice that bills too many units and an invoice that bills too little. These demonstrate the behaviour for the two undefined rules.

### Out-of-Scope

The supplied rules exclude these:

- Tax, part shipments, foreign currency and payment terms.
- Duplicate invoices and unresolved invoices in a recoverable total.
- Commercial rules beyond the supplied rules.

This feature excludes these:

- Messages to a supplier. All notes stay drafts.
- Payments, and the schedule of payments.
- Connections to accounting systems and purchasing systems. The specification supplies the purchase orders and the receipts.
- Sign-in, user accounts and permissions.
- Operation on a machine other than the machine of the reviewer.
- Invoices that bill more than one item. Each invoice covers one item.
- Invoices as PDF files. *(The brief makes this optional.)*
- Export of the queue and of the calculations. *(Optional.)*
- A history of reviewer decisions and note versions. The platform keeps only the first value next to its correction. *(Optional.)*

Later work covers this:

- The written AI-workflow document and the configuration manifest.

### Recorded ambiguities and the chosen behaviour

The supplied rules do not define four points. This specification **settles** each one, so that the behaviour is testable. It also **records** each one as an ambiguity in the supplied rules. The brief asks for both. These entries go into the written assumptions for the project, with the gap, the behaviour and the reason.

- **Invoices with more than one item.**
  - *Gap:* each supplied invoice bills one item. No rule says whether an invoice can bill more.
  - *Behaviour:* an invoice covers one item.
  - *Reason:* this agrees with each supplied example and keeps the work inside the time limit. The documents record it as a limitation.

- **Quantity differences.**
  - *Gap:* the rules ask for a comparison of the billed quantity with the ordered quantity and the received quantity. But no supplied example has different quantities, and no expected result covers this case.
  - *Behaviour:* the platform gives the status **discrepant**. It calculates the difference at the agreed unit price.
  - *Reason:* this uses the four statuses of the supplied expected answers. A fifth status would add a rule, and the supplied rules forbid this.
  - *Evidence:* **a new example invoice covers this case.** The behaviour is therefore tested, not only stated. See 2.11.

- **The payable copy of a duplicate.**
  - *Gap:* the rules identify duplicates by supplier and invoice number. No rule says which copy the business pays.
  - *Behaviour:* the platform judges the first copy on its own figures. It marks later copies as duplicates and excludes them from the totals.
  - *Reason:* **the supplied examples demonstrate this behaviour.** The supplied data holds a reconciled invoice and a later copy with the same invoice number, and it marks the later copy as a duplicate.

- **A supplier that bills too little.**
  - *Gap:* the rules call a positive difference an overcharge. They say nothing about a negative difference.
  - *Behaviour:* the platform gives the status **discrepant** with a negative difference. The recoverable total excludes it.
  - *Reason:* a negative difference is still a disagreement, and the reviewer must see it. But it is not money for the business to pursue, so it must not increase the recoverable total.
  - *Evidence:* **a new example invoice covers this case.** The behaviour is therefore tested, not only stated. See 2.11.

---

## Change Log

_No amendments yet._

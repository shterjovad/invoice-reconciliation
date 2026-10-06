# Product Roadmap: Invoice Reconciliation

_This roadmap outlines our strategic direction based on customer needs and business goals. It focuses on the "what" and "why," not the technical "how."_

> **Anchoring note:** A capability assessment (`context/product/brownfield.md`, `## Capabilities`)
> found **no implemented application features** — the repository contains only exercise fixtures and
> workflow scaffolding. Every item below is therefore upcoming work; nothing is marked complete.
> Phases are ordered by dependency: the reconciliation rules are proven against known-good data
> before extraction is allowed to introduce uncertainty, and the reviewer interface is built on top
> of results that are already trustworthy.

---

### Phase 1

_The highest priority features that form the core foundation of the product. The goal of this phase is a reconciliation engine that is provably correct against the supplied oracle, before any image reading is involved._

- [ ] **Runnable Project Setup**
  - [ ] **Declared Dependencies:** Make the project installable and runnable from a clean checkout with its dependencies declared, including the imaging library the fixture renderer needs — today that renderer fails on a missing dependency, so the fixtures cannot be regenerated.
  - [ ] **Regenerable Fixtures:** Confirm the supplied invoice images can be regenerated from the seed data, so the test set is reproducible rather than only a set of committed files.

- [ ] **Reference Data Foundation**
  - [ ] **Purchase Order & Receipt Records:** Load the agreed SKU, quantity, and unit price for each purchase order, and the delivered quantity for each receipt, so every invoice has something to be judged against.
  - [ ] **Invoice Records:** Hold what a supplier billed — invoice number, supplier, purchase-order reference, SKU, quantity, unit price, and total — independently of how those values were obtained.

- [ ] **Matching & Classification**
  - [ ] **Match on Supplier and Purchase Order:** Pair each invoice with its purchase order and receipt using supplier ID together with purchase-order ID, so that near-identical orders cannot be confused with one another.
  - [ ] **Flag Unresolved Invoices:** Where the purchase-order reference is missing or conflicting, mark the invoice unresolved and compute no amounts, rather than guessing a match from the billed total.
  - [ ] **Detect Duplicate Submissions:** Identify repeat submissions by supplier and invoice number so the same bill is never paid twice.

- [ ] **Discrepancy Calculation**
  - [ ] **Expected vs Billed Amounts:** Calculate the expected amount from ordered quantity and agreed unit price, compare it with what was billed, and report the difference in exact USD cents.
  - [ ] **Quantity Checks:** Compare the billed quantity against both the ordered and the received quantity, so over-billing by volume is caught as well as by price.
  - [ ] **Honest Recoverable Totals:** Keep duplicates and unresolved invoices out of any recoverable figure, so a reported recovery is one the business can actually pursue.

- [ ] **Proof Against Known Cases**
  - [ ] **Seed Case Verification:** Reproduce the supplied expected results for the four seed invoices exactly — including the differing record shapes for duplicate and unresolved entries — and keep this check runnable on demand so a reviewer can confirm it independently of the interface.

---

### Phase 2

_With the rules proven, this phase takes on the harder input problem: reading invoices as documents rather than as prepared data._

- [ ] **Invoice Document Intake**
  - [ ] **Read Invoices From Images:** Extract the billed fields from supplier invoices supplied as images, so reconciliation can run from the documents a supplier actually sends.
  - [ ] **Handle Differing Layouts:** Read invoices correctly whether fields are laid out in one column or two, so a supplier changing their template does not break processing.
  - [ ] **Record Extraction Confidence:** Keep the extracted values traceable back to the source document, so a reviewer can tell what was read and check it against the invoice itself.

- [ ] **End-to-End Reconciliation**
  - [ ] **Documents to Classified Results:** Run the full path — invoice image in, classified and costed result out — and confirm the seed cases still reproduce the expected results when driven from the images rather than from prepared data.

---

### Phase 3

_The reviewer experience. Built last because it presents results that are already correct and explainable._

- [ ] **Invoice Review Queue**
  - [ ] **At-a-Glance Invoice Queue:** Show every invoice in a batch with its status — reconciled, discrepant, duplicate, or unresolved — so a reviewer can see immediately what needs attention.
  - [ ] **Invoice Detail With Evidence:** For each invoice, show the matched purchase order and receipt, the expected and billed figures, and the reason for its status, alongside the invoice image, so no number has to be taken on trust.

- [ ] **Correction & Recalculation**
  - [ ] **Inline Field Correction:** Let a reviewer correct a mis-extracted field directly against the invoice image when the system has read it wrongly.
  - [ ] **Recalculate From Corrections:** Recompute the expected amount, the difference, and the status from the corrected value, and record what was changed, so corrections are auditable rather than silent.

- [ ] **Reconciliation Summary**
  - [ ] **Batch Summary View:** Summarise a run by status, reporting the genuine recoverable overcharge separately from duplicates and unresolved items, so the headline figure is never inflated by invoices that cannot be pursued.

---

### Phase 4

_Completing the exercise deliverables. These are required for review and are deliberately sequenced after the product works, so they describe what was actually built._

- [ ] **Extended Test Coverage**
  - [ ] **Additional Invoice Cases:** Add two to four further invoices under the same rules, including one case exercising a field correction with its expected recalculation, and at least one covering a scenario the supplied seed data cannot test.
  - [ ] **Resolve or Record Ambiguity:** Settle the open domain questions — multi-line invoices, quantity mismatches, which copy of a duplicate stays payable, and undercharges — or record them explicitly as unresolved, without inventing rules.

- [ ] **Reproducibility & Workflow Write-Up**
  - [ ] **Reviewer Run Instructions:** Document how to run the application and the seed-case check from a clean checkout, building on the dependency setup established in Phase 1.
  - [ ] **AI Workflow Documentation:** Complete the workflow write-up and configuration manifest — tools and models used, configuration inventory, one worked example with a correction, and a credential-free replay path.

# Product Roadmap: Invoice Reconciliation

_This roadmap gives the direction of the work. It covers the "what" and the "why". It does not cover
the technical "how"._

> **Note on the order.** A capability check found **no application code** in this repository. It
> holds only the supplied files and the workflow tools. Each item below is therefore new work.
> Nothing is complete. The phases follow the dependencies. The rules get proof against known data
> before extraction adds doubt. The reviewer interface comes after the results are correct.

---

### Phase 1

_The first group of features. The goal of this phase is a reconciliation engine with proof against
the supplied answers, before any image reading starts._

- [ ] **Runnable Project Setup**
  - [ ] **Declared Dependencies:** Make the project run from a clean copy, with its dependencies declared. This includes the imaging library for the fixture renderer. The renderer fails today, because that library is absent, so nobody can generate the images again.
  - [ ] **Regenerable Fixtures:** Confirm that the supplied invoice images regenerate from the seed data. The test set then repeats, and is not only a set of files.

- [ ] **Reference Data Foundation**
  - [ ] **Purchase Order and Receipt Records:** Load the agreed SKU, quantity and unit price for each purchase order. Load the delivered quantity for each receipt. Each invoice then has records to judge it against.
  - [ ] **Invoice Records:** Hold what the supplier billed: the invoice number, supplier, purchase-order reference, SKU, quantity, unit price and total. Hold these apart from the method that found them.

- [ ] **Matching and Status**
  - [ ] **Match on Supplier and Purchase Order:** Match each invoice to its purchase order and receipt on the supplier and the purchase-order reference. Two almost identical orders then stay separate.
  - [ ] **Mark Unresolved Invoices:** Where the purchase-order reference is absent or wrong, mark the invoice unresolved and calculate no amounts. Do not guess the match from the billed total.
  - [ ] **Find Duplicate Invoices:** Find repeated invoices by supplier and invoice number. The business then does not pay the same bill twice.

- [ ] **Difference Calculation**
  - [ ] **Expected Against Billed:** Calculate the expected amount from the ordered quantity and the agreed unit price. Compare it with the billed amount. Report the difference in exact USD cents.
  - [ ] **Quantity Checks:** Compare the billed quantity with the ordered quantity and the received quantity. The product then finds overcharges by volume as well as by price.
  - [ ] **Honest Recoverable Totals:** Keep duplicates and unresolved invoices out of the recoverable figure. The reported figure is then money that the business can pursue.

- [ ] **Proof Against Known Cases**
  - [ ] **Seed Case Check:** Reproduce the supplied expected results for the four invoices exactly. This includes the different record shapes for duplicate entries and unresolved entries. Keep this check available at any time, apart from the interface.

---

### Phase 2

_The rules now have proof. This phase takes the harder input problem: the product reads invoices as
documents, not as prepared data._

- [ ] **Invoice Document Intake**
  - [ ] **Read Invoices From Images:** Find the billed values in supplier invoices that arrive as images. The product then runs from the documents that a supplier sends.
  - [ ] **Read Both Layouts:** Read invoices correctly in one-column form and two-column form. A change of supplier template then does not stop the work.
  - [ ] **Keep the Source Visible:** Keep each value connected to its source document. The reviewer can then see what the product read and compare it with the invoice.
  - [ ] **Continue After a Failure:** Continue the batch when one invoice fails to read, or when the model does not answer. Show each failed invoice with a clear status. Give it no amount.

- [ ] **Replay Without Credentials**
  - [ ] **Save Model Responses:** Save the real responses from the model and run the full flow from them. An assessor with no account can then repeat the results. The brief requires this.

- [ ] **Full Flow**
  - [ ] **Documents to Results:** Run the full path, from invoice image to a status with its figures. Confirm that the supplied cases still give the expected results from the images.

---

### Phase 3

_The reviewer experience. This comes last, because it presents results that are already correct._

- [ ] **Invoice Review Queue**
  - [ ] **Invoice Queue:** Show each invoice of a batch with its status: reconciled, discrepant, duplicate or unresolved. The reviewer then sees the exceptions at once.
  - [ ] **Status Filter:** Filter the queue by status, so that the reviewer finds one group quickly.
  - [ ] **Invoice Detail With Evidence:** For each invoice, show the matched purchase order and receipt, the expected and billed figures, and the reason for the status. Show the invoice image beside them. The reviewer then trusts no number on faith.

- [ ] **Correction and Recalculation**
  - [ ] **Correct a Value:** Let the reviewer correct a value against the invoice image, where the product read it incorrectly.
  - [ ] **Calculate Again:** Calculate the expected amount, the difference and the status again from the corrected value. Record the change, and keep the first value. The correction is then clear, not silent.
  - [ ] **Keep Corrections:** Keep each correction after a restart.

- [ ] **Summary and Note**
  - [ ] **Batch Summary:** Summarise a run by status. Report the true recoverable overcharge apart from the duplicates and the unresolved invoices. The headline figure then stays honest.
  - [ ] **Draft a Note:** Draft a short note for each difference. State the figures and name the source records. Keep the note a draft.
  - [ ] **One Improvement:** Give one repeated problem or one improvement from the batch, with the invoices that support it.

---

### Phase 4

_The remaining deliverables for the exercise. These come after the product works, so that they
describe the real result._

- [ ] **Extended Test Coverage**
  - [ ] **More Invoice Cases:** Extend the set to six to eight invoices under the same rules. Add an invoice that bills too many units and an invoice that bills too little, because the supplied data tests neither. Add a case that needs a correction, with its expected result.
  - [ ] **Five Reference Cases:** Give five or more reference cases with answers calculated in advance. Keep these answers apart from the output of the product.
  - [ ] **Record the Ambiguities:** Record each open point in the supplied rules, with the chosen behaviour and the reason. Do not invent rules.

- [ ] **Documents for the Assessor**
  - [ ] **Run Instructions:** Describe how to run the application and the case check from a clean copy.
  - [ ] **AI Workflow Record:** Complete the workflow document and the configuration manifest: the tools and models, the configuration files, one worked example with a correction, and the replay path without credentials.

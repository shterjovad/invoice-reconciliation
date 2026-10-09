---
name: invoice-reconciliation-exercise-brief
description: "The authoritative assignment brief lives outside the repo at <home>/Desktop/Provectus Test Task and overrides the repo's domain.md on several requirements."
metadata: 
  node_type: memory
  type: project
  originSessionId: <session-id>
  modified: 2026-10-06T16:34:48.803Z
---

The real brief for this project is **not in the repository**. It is here:

- `<home>/Desktop/Provectus Test Task/client-ai-project-research.html` — the full brief.
  Read "Alternative B · Invoice reconciliation". Strip the HTML tags to read it.
- `<home>/Desktop/Provectus Test Task/client-ai-starter-pack/` — the original starter pack
  with all seven task folders.

This is a Junior AI Engineer take-home assignment, task B. The limit is **8 hours in total**,
which includes data preparation and documentation.

**The brief requires work that the repo's `tasks/invoices/domain.md` does not mention:**

- Six to eight invoices. `domain.md` says "add two to four", which understates it.
- A model-drafted discrepancy note that cites its source records.
- Analytics: counts by status, a summary of the differences, and one process improvement.
- Batch resilience. One bad invoice, or a model failure, must not stop the other invoices.
  Failures must stay visible.
- Five reference cases, one of them a correction of an extracted field. Expected values must be
  stored apart from the application's output.
- A way to replay saved real responses without an API key.

The brief marks these as **optional**: PDF invoices, export of the queue, and a stored history of
reviewer decisions.

**Why:** I read `domain.md` first and built the product definition and roadmap from it. The brief
then changed the scope. Anyone who plans work from the repository alone will miss these
requirements.

**How to apply:** Read the brief before you plan or cut scope. Where the brief and `domain.md`
disagree, the brief wins. See [[writing-style-asd-ste100-orwell]] — the brief scores
communication, which is why the writing rules matter.

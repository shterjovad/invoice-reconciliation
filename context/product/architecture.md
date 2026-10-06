# System Architecture Overview: Invoice Reconciliation

_Local-only application. The stack below was specified directly and is treated as decided; the
alternatives listed exist to record what was considered and why it was not chosen, not to reopen the
choice._

**Guiding constraints**

1. **One pipeline, two entry points.** A headless batch command and the web view run the *same*
   ingest → extract → reconcile → persist path. The web view reads and mutates the same database; it
   is not a parallel implementation.
2. **Money is integer USD cents, everywhere.** No floating-point money arithmetic at any layer.
3. **A reviewer with no AWS credentials must be able to run the whole thing** from recorded model
   responses.
4. **The oracle's record shape is binding** — the seed check compares against the supplied shape, not
   a normalised one.

---

## 1. Application & Technology Stack

- **Language:** Python 3.11+ — the host default interpreter is 3.11.6, which satisfies this.
  _Alternatives: 3.12/3.13 (available, but 3.11 is the verified baseline and avoids churn)._
- **Web Framework:** FastAPI — serves the reviewer UI and the correction endpoints.
  _Alternatives: Flask (lighter, but FastAPI's typed request models and validation suit the
  correction payloads); Django (far more than a local, auth-free review tool needs)._
- **View Layer:** Jinja2 server-rendered templates — the reviewer UI is a queue, a detail view, and a
  correction form; server rendering keeps all reconciliation logic in Python with no client-side
  state to drift from the database.
  _Alternatives: React/Vue SPA (adds a build toolchain and a second place for money formatting to go
  wrong, for a single-user local tool)._
- **ASGI Server:** Uvicorn — the standard FastAPI development server.
- **Batch Entry Point:** A headless command (`python -m ...`) running ingest → extract → reconcile →
  persist. This is what makes the seed check reproducible and is the primary target of the replay
  mode.
- **Fixture Rendering:** Pillow — required by `tasks/invoices/render_invoices.py`. **Pin `Pillow >=
  10.1`**: the script calls `ImageFont.load_default(size=...)`, and the `size` parameter is not
  available on older releases.
- **Dependency Management:** `uv` with a `pyproject.toml` and committed lockfile — `uv` 0.7.8 is
  already on the host and gives a reproducible 3.11 environment from a clean checkout.
  _Alternatives: Poetry (also installed, slower); bare pip + requirements.txt (no lockfile)._
  **Note:** nothing the project needs is currently installed on the default interpreter — not
  `anthropic`, `boto3`, `fastapi`, `jinja2`, `Pillow`, or `pytest`. A fresh virtualenv is mandatory.
  A stale x86_64 Python 3.10.6 on the host has some of these but is **unusable** on this arm64
  machine; do not plan around it.

---

## 2. Data & Persistence

- **Primary Database:** SQLite — single-file local state, already anticipated by `.gitignore`
  (`*.sqlite`, `*.db`, "Local application state (regenerate from fixtures)"). The database is a
  rebuildable artifact, never a source of truth: the fixtures are.
  _Alternatives: PostgreSQL (needs a running service, unjustified for a local single-user tool);
  in-memory only (would lose corrections between the batch run and the web view, breaking the
  shared-pipeline requirement)._
- **Database Access:** A **thin hand-rolled data layer over the standard library's `sqlite3`** — no
  ORM. The schema is small and the queries are simple joins, so an ORM would add a dependency and an
  abstraction without earning either. Keeping SQL explicit also keeps the integer-cents columns
  visible at the point of query.
  _Alternatives: SQLAlchemy Core (more structure and easier future migrations, but neither is needed
  for a local tool rebuilt from fixtures); a full ORM (clearly excessive here)._
- **Money Representation:** **Integer cents in `INTEGER` columns.** No `REAL`/float money column may
  exist anywhere in the schema.
- **Money Conversion (single most important rule):** Extraction returns **dollar strings** (`"24.00"`,
  `"120.00"`). Convert to cents in **exactly one module**, using `Decimal` or a string split on the
  decimal point — **never `float(x) * 100`**, which silently rounds (e.g. `float("24.00")*100` is not
  reliably `2400` across values). This converter carries its own unit tests covering trailing zeros,
  missing decimal places, and values with no decimal point at all.
- **Core Entities:** purchase orders, receipts, invoices, extracted-field records, and reconciliation
  results.
- **Mutable Extraction Records:** Extracted field values are stored **separately from the
  reconciliation result** and are mutable, with the original model-extracted value retained alongside
  any reviewer correction. The exercise explicitly requires demonstrating a field correction and the
  recalculation it produces, so reconciliation must be a recomputable function of current field
  values — not a one-shot pipeline that discards its inputs.
- **Correction Audit:** Each correction records which field changed, the value before and after, and
  that it was reviewer-sourced, so a recalculated figure can be explained.

---

## 3. Infrastructure & Deployment

- **Deployment Model:** **None — local only.** No cloud deployment, no containers, no CI/CD. This is
  an explicit out-of-scope decision from the brief, not an omission.
- **Runtime:** A local `uv`-managed virtualenv; Uvicorn bound to localhost for the web view.
- **Database Lifecycle:** The SQLite file is regenerable from the fixtures by re-running the batch
  command; it is git-ignored and never committed.
- **Repository Hygiene:** No Dockerfile, compose file, or workflow configuration is to be added.
  _Note: Docker 28.0.4 is available on the host but is deliberately unused._

---

## 4. External Services & APIs

- **Model Provider:** **AWS Bedrock**, region **us-east-1**, accessed through the `anthropic` Python
  SDK's Bedrock client.
  _Alternatives: the Anthropic API directly (would require a committed API key, which the project's
  secret policy discourages); local OCR such as Tesseract (brittle across the two layouts, and the
  images carry no text layer)._
- **Model ID:** `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. **The `us.` inference-profile prefix
  is required** — a bare model ID fails with `ValidationException: Invocation with on-demand
  throughput isn't supported`. This is verified, not assumed: a test call against
  `tasks/invoices/images/wrong-price.png` returned all seven fields correctly (854 input / 96 output
  tokens).
- **Extraction Contract:** The model returns the seven invoice fields — invoice number, supplier ID,
  purchase-order ID, SKU, quantity, unit price, total. Monetary fields come back as **dollar
  strings** and are converted to cents by the single converter in section 2. A missing
  purchase-order reference must be representable (null), since the `missing-reference` fixture
  depends on it.
- **Credentials:** **SigV4 via the standard AWS credential chain.** There is no API key. Access is
  **session-based, established with `aws login`** (the active identity resolves to the account root,
  `arn:aws:iam::...:root`), and the `[default]` profile resolves to us-east-1. The application must
  resolve credentials through the chain rather than reading keys from the environment; there is no
  `~/.aws/credentials` file holding static keys.
  **Run instructions must say `aws login`, not `aws sso login`.** The IAM Identity Center profiles
  present in `~/.aws/config` belong to unrelated work and are not used by this project.
- **Response Cache & Replay Mode:** Raw Bedrock responses are **cached to disk, keyed by invoice
  image**, with a flag to run the pipeline from cache instead of calling the model. This is a **hard
  requirement**: the exercise requires documenting how to "replay saved real responses without
  credentials", and because access depends on a session that lapses, a reviewer may have no working
  AWS access at all. The cached responses are committed so replay works from a clean checkout. Replay
  must exercise the real parsing and reconciliation path — only the network call is bypassed.
- **No other external services.** No authentication provider, no payments, no analytics — all out of
  scope.

---

## 5. Observability & Monitoring

- **Logging:** Python's standard `logging` to console and a local file. Each batch run logs per
  invoice: which purchase order and receipt it matched, the extracted values used, the computed
  expected and billed amounts, and the resulting status — so any classification can be explained
  after the fact.
  _Alternatives: structured JSON logging (more than a local tool needs, though harmless)._
- **Model Call Records:** Each extraction logs the model ID, whether the response came from the live
  API or the cache, and the token counts. `manifest.template.json` requires in-application model
  parameters to be recorded separately from development tooling, so these belong in the application's
  own records.
- **Seed Verification as the Primary Check:** The reconciliation output for the four seed invoices is
  compared against `tasks/invoices/expected-seed-results.json` **in that file's exact per-status
  record shape**: a `duplicate` record carries `count_as_payable` and omits the cent fields entirely,
  while an `unresolved` record carries explicit `null` for `expected_cents`/`difference_cents` and
  omits `billed_cents`. **The comparison must not normalise these shapes.**
- **Testing:** `pytest`, with a golden-file test against the oracle plus targeted unit tests for the
  money converter and the classification rules. No test framework is configured in the repo today, so
  this is new.
- **No external monitoring.** No Sentry, OpenTelemetry, or metrics backend — out of scope for a local
  tool.

---

## 6. Secrets & Configuration

- **`ai-workflow/.env.example` carries variable names only:** `AWS_REGION` and `BEDROCK_MODEL_ID`.
- **`MODEL_API_KEY` is to be removed** from that file. Bedrock authenticates with SigV4 from the AWS
  credential chain, so there is no key to commit — leaving the name in place would misrepresent how
  the application authenticates.
- `.gitignore` already ignores `.env` and `.env.*` while force-including `.env.example`; no
  credentials, tokens, or account identifiers may appear in any committed file.

---

## 7. Open Items for `/awos:tech`

- The on-disk cache format and key derivation for recorded model responses (content hash versus
  `file_id`).
- Whether extraction runs invoices concurrently or serially — serial is the simpler default for four
  fixtures.
- How the batch command surfaces the seed-check result (exit code, printed diff, or both).
- The four open domain questions carried from the product definition (multi-line invoices, quantity
  mismatches, which duplicate copy stays payable, undercharges) remain product decisions, not
  architectural ones.

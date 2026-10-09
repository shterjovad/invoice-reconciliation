# System Architecture Overview: Invoice Reconciliation

_The product runs on one local machine. The user specified the stack below, so these choices are
settled. The alternatives record what I considered and why I did not choose it. They do not reopen
the decision._

**Controlling constraints**

1. **One pipeline, two entry points.** A headless batch command and the web view run the *same*
   path: ingest, extract, reconcile, persist. The web view reads and writes the same database. It is
   not a second implementation.
2. **All money is integer USD cents.** No layer uses floating-point arithmetic for money.
3. **An assessor with no AWS credentials must run the full flow** from saved model responses.
4. **The supplied answer file controls the comparison.** The seed check compares against the exact
   record shape of that file. It does not normalise the shape.

---

## 1. Application and Technology Stack

- **Language:** Python 3.11+. The default interpreter on this machine is 3.11.6, which meets this.
  _Alternatives: 3.12 or 3.13. Both exist on the machine, but 3.11 is the tested base._
- **Web Framework:** FastAPI. It serves the reviewer interface and the correction endpoints.
  _Alternatives: Flask is smaller, but FastAPI validates the correction requests through typed
  models. Django gives far more than a local tool needs._
- **View Layer:** Jinja2 templates, rendered on the server. The interface is a queue, a detail view
  and a correction form. Server rendering keeps all the reconciliation logic in Python. No state on
  the client can then disagree with the database.
  _Alternatives: a React or Vue application. This adds a build toolchain and a second place where
  money formatting can fail, for a tool with one user._
- **ASGI Server:** Uvicorn, the standard development server for FastAPI.
- **Batch Entry Point:** a headless command (`python -m ...`). It runs ingest, extract, reconcile and
  persist. This command makes the seed check repeatable. It is also the main target of the replay
  mode.
- **Fixture Rendering:** Pillow. The script `tasks/invoices/render_invoices.py` needs it. **Pin
  `Pillow >= 10.1`.** The script calls `ImageFont.load_default(size=...)`, and older versions do not
  accept the `size` parameter.
- **Dependency Management:** `uv`, with a `pyproject.toml` and a committed lockfile. Version 0.7.8
  is already on the machine. It builds a repeatable 3.11 environment from a clean copy.
  _Alternatives: Poetry is also present but slower. Plain pip with a requirements file gives no
  lockfile._
  **Note:** the default interpreter has none of the packages that this project needs. It lacks
  `anthropic`, `boto3`, `fastapi`, `jinja2`, `Pillow` and `pytest`. A new virtual environment is
  therefore necessary. An old x86_64 Python 3.10.6 on this machine holds some of these packages, but
  `fastapi` cannot import there on arm64. **Do not use that interpreter.**

---

## 2. Data and Persistence

- **Primary Database:** SQLite. It keeps the local state in one file. The `.gitignore` file already
  expects this: it ignores `*.sqlite` and `*.db` under the comment "Local application state
  (regenerate from fixtures)". The database is a rebuildable artifact. It is never the source of
  truth. The supplied files are.
  _Alternatives: PostgreSQL needs a running service, which a local tool with one user does not
  justify. Memory-only storage would lose the corrections between the batch run and the web view,
  which breaks the shared-pipeline rule._
- **Database Access:** a thin layer over the standard library module `sqlite3`. **No ORM.** The
  schema is small and the queries are simple joins. An ORM would add a dependency and a layer of
  abstraction, and earn neither. Explicit SQL also keeps the integer-cent columns visible in the
  query.
  _Alternatives: SQLAlchemy Core gives more structure and easier migrations later. A local tool that
  rebuilds from files needs neither._
- **Money Representation:** **integer cents in `INTEGER` columns.** No `REAL` column for money can
  exist in the schema.
- **Money Conversion — the most important rule in this document.** Extraction returns money as
  **dollar strings**, such as `"24.00"` and `"120.00"`. **The model does not do this reliably.**
  Three identical test calls on the same image returned JSON numbers once (`24.0`) and strings
  twice. The project therefore defends at both ends: the response schema types the money fields as
  strings, and the converter accepts a string or a number.
  **One module** converts to cents. For a numeric input it first formats the value as text with two
  decimal places, then splits on the decimal point and parses each half as an integer. It **never
  uses `float(x) * 100`**, which rounds silently. This converter has its own unit tests. They cover
  trailing zeros, missing decimal places, values with no decimal point, negative values,
  over-precision, and **the numeric forms `24.0`, `120.0` and `5`**.
- **Core Entities:** purchase orders, receipts, invoices, extracted values and reconciliation
  results.
- **Changeable Extraction Records:** the extracted values live **apart from the reconciliation
  result**, and the reviewer can change them. The product keeps the first value from the model next
  to any correction. The exercise asks for a correction and the result that follows it.
  Reconciliation must therefore recalculate from the current values. It cannot be a single pass that
  discards its input.
- **Correction Record:** each correction records the changed field, the value before, the value
  after, and that the reviewer made the change. Each new figure then has an explanation.

---

## 3. Infrastructure and Deployment

- **Deployment Model:** **none. The product runs locally.** No cloud, no containers, no CI. The
  brief states this, so it is a decision and not an omission.
- **Runtime:** a local virtual environment that `uv` manages. Uvicorn binds to localhost for the web
  view.
- **Database Lifecycle:** the batch command regenerates the SQLite file from the supplied data. Git
  ignores the file. Nobody commits it.
- **Repository Hygiene:** do not add a Dockerfile, a compose file or a workflow file.
  _Note: Docker 28.0.4 is on the machine. This project does not use it._

---

## 4. External Services and APIs

- **Model Provider:** **AWS Bedrock**, region **us-east-1**, through the classic
  **`bedrock-runtime`** client in `boto3`, calling `invoke_model` with the
  `anthropic_version: "bedrock-2023-05-31"` body. **Corrected 2026-10-06:** this entry previously
  required `AnthropicBedrockMantle`. That targets `bedrock-mantle...api.aws`, a separate AWS service
  with its own entitlement, and it returns `404` for every model on this account. Classic
  `bedrock-runtime` works with the same credentials, region and model ID.
  _Alternatives: the Anthropic API needs a committed API key, which the secret policy of this
  project discourages. Local OCR such as Tesseract breaks across the two layouts, and the images
  carry no text layer._
- **Model ID:** `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. **The `us.` inference-profile prefix
  is necessary.** A bare model ID fails with `ValidationException: Invocation with on-demand
  throughput isn't supported`. A test call proved this. The call used
  `tasks/invoices/images/wrong-price.png` and returned all seven values correctly, with 854 input
  tokens and 96 output tokens.
- **Extraction Contract:** the model returns seven values: the invoice number, supplier ID,
  purchase-order ID, SKU, quantity, unit price and total. The monetary values come back as **dollar
  strings**. The single converter in section 2 turns them into cents. The contract must allow a null
  purchase-order reference, because the `missing-reference` example depends on it.
- **Credentials:** **SigV4, through the standard AWS credential chain.** There is no API key. The
  session comes from **`aws login`**, and the identity resolves to the account root. The `[default]`
  profile resolves to us-east-1. The application reads credentials from the chain. It does not read
  them from environment variables. There is no `~/.aws/credentials` file with static keys.
  **The run instructions say `aws login`, not `aws sso login`.** The IAM Identity Center profiles in
  `~/.aws/config` belong to other work. This project does not use them.
- **Response Cache and Replay Mode:** the product saves each raw Bedrock response to disk, under a
  key from the invoice image. A flag then runs the pipeline from the cache in place of the model.
  **The brief requires this.** It asks for instructions to "replay saved real responses without
  credentials". The session can also lapse, so an assessor may have no AWS access at all. The saved
  responses go into the repository, so replay works from a clean copy. Replay must run the real
  parsing and reconciliation path. It bypasses only the network call.
- **Failure Handling:** a failed call, or an invalid response, must not stop the batch. The product
  marks that invoice with a visible failure status and gives it no amount.
- **No other external services.** No sign-in provider, no payments, no analytics.

---

## 5. Observability and Monitoring

- **Logging:** the standard `logging` module, to the console and to a local file. For each invoice,
  a batch run logs the matched purchase order and receipt, the values used, the expected and billed
  amounts, and the status. Each result then has an explanation afterwards.
  _Alternatives: structured JSON logs. A local tool does not need them, but they do no harm._
- **Model Call Records:** each extraction logs the model ID, the token counts, and the source of the
  response: the live API or the cache. `ai-workflow/manifest.template.json` asks for the model
  parameters of the application, apart from the development tools. These records belong to the
  application.
- **The Seed Check is the Main Test:** the product compares its results for the four supplied
  invoices with `tasks/invoices/expected-seed-results.json`. It compares against **the exact record
  shape of that file**. A `duplicate` record holds `count_as_payable` and omits the cent fields. An
  `unresolved` record holds explicit `null` for `expected_cents` and `difference_cents`, and omits
  `billed_cents`. **The comparison must not normalise these shapes.**
- **Testing:** `pytest`. It runs a golden-file test against the supplied answers, plus unit tests for
  the money converter and the status rules. This repository has no test framework today, so this is
  new.
- **Reference Cases:** the project keeps five or more reference cases with answers calculated in
  advance. These answers stay in a separate file from the output of the product.
- **No external monitoring.** No Sentry, no OpenTelemetry, no metrics service.

---

## 6. Secrets and Configuration

- **`.env.example` (repository root) holds only variable names:** `AWS_REGION` and `BEDROCK_MODEL_ID`. Both are optional overrides of the in-code defaults. No API-key variable exists; AWS credentials resolve through the standard credential chain (SigV4).
- **`MODEL_API_KEY` is gone.** The starter pack's `ai-workflow/.env.example` carried that name; it
  was removed rather than shipped, per the pack's own "Remove this example if no variables were
  used." Bedrock authenticates with SigV4 from the credential
  chain, so there is no key to commit. The name would misrepresent how the application connects.
- `.gitignore` already ignores `.env` and `.env.*`, and keeps `.env.example`. No committed file may
  hold a credential, a token or an account number.

---

## 7. Open Items for `/awos:tech`

- The format of the response cache on disk, and the key for each entry: a hash of the content, or
  the `file_id`.
- Serial or concurrent extraction. Serial is the simpler default for a small set of invoices.
- How the batch command reports the seed check: an exit code, a printed comparison, or both.
- How the product stores the drafted note, and whether an edit by the reviewer persists.

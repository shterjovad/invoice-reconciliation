# AI workflow used during this exercise

This file replaces `README.template.md` for submission. The template stays in
this folder, unchanged, next to `manifest.template.json`, so a reviewer can
compare the blank starting point against what was filled in. See "Decisions
and limitations" below for why both the template and the filled file are kept
rather than the template being deleted.

## Tools and models

**Development tool.** Claude Code CLI, version 2.1.295 (recorded
2026-10-09). Used for all coding, planning and testing work on this
exercise. One extension was added to it: Playwright MCP, for browser
automation (see "MCP servers" below). No Claude Code plugin, IDE plugin or
browser extension was used.

| Sessions | Model |
|---|---|
| 2026-10-06, the build | Claude Opus 5, as recorded when this file was first written |
| 2026-10-09, the summary-page and failure-handling work and this update | Claude Opus 5.5 (`claude-opus-5-5`) |

The repository does not store the model of each session, so the first row
rests on that first record.

**Application model** (the model the built application calls, separate from
the development tool above). Configured in
`src/invoice_reconciliation/config.py`:

| Setting | Value | Source |
|---|---|---|
| Model ID | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | `BEDROCK_MODEL_ID` env var, default in code |
| Region | `us-east-1` | `AWS_REGION` env var, default in code |
| Temperature | `0.0` | hardcoded — repeatable extraction, same image reads the same way each call |
| Max tokens | `1024` | hardcoded |

The `us.` inference-profile prefix on the model ID is required. A bare model
ID fails on AWS Bedrock with `ValidationException: Invocation with on-demand
throughput isn't supported`. This was confirmed by running the call both
ways, not assumed.

**Three model calls.** The application calls the same model three times
over, for three jobs. Each call uses one forced tool call and temperature
`0.0`. All prompts and response schemas are in
`src/invoice_reconciliation/prompts.py`.

| Call | Code | Max tokens | Checks and fallback |
|---|---|---|---|
| Extraction of the seven invoice fields | `extraction/client.py` | `1024` | Parser checks shape, schema and domain. Any model or API failure becomes `ModelCallError` and fails only that invoice. |
| Discrepancy note | `reconciliation/notes.py` | `512` | `verify_note`, up to 3 attempts, then a calculated note |
| Process improvements on `/summary` | `reconciliation/improvement.py` | `300` per issue | One call for all ranked issues; `verify_improvement` checks each text; up to 3 calls, then a prepared sentence |

Only extraction reads `BEDROCK_MODEL_ID` and `AWS_REGION`. The note and
improvement calls use the in-code model ID.

**Library versions** (from `uv.lock`): `anthropic` 1.11.0, `boto3`
1.43.108, `botocore` 1.43.108, `fastapi` 0.142.2, `uvicorn` 0.54.0,
`jinja2` 3.1.6. Python 3.11.

**Client class, corrected.** The build first targeted
`AnthropicBedrockMantle` (the `anthropic` SDK's Bedrock-Mantle surface, a
separate AWS-hosted service from classic Bedrock). That path returned
`404 not_found` for every model ID tried on this account, including bare
names the SDK itself recognizes with a deprecation warning — confirmed
independently by two people against the same credentials, region, and model
ID. The account has no Mantle entitlement. Classic Bedrock
(`boto3` `bedrock-runtime`, `invoke_model`), with the identical model ID,
returns a real completion and usage counts. `src/invoice_reconciliation/extraction/client.py`
now uses classic Bedrock, and its module docstring records this history.
The corrected decision is also recorded in `technical-considerations.md`.

**Credentials.** Resolved through the standard AWS credential chain, using
SigV4 request signing. There is no API key and no application-level secret
to redact — "no credential" is the accurate answer to "where is the key,"
not an omission.

## Configuration files

**Custom subagents** (`.claude/agents/`), each scoped to one layer of the
build and genuinely invoked during it:

| Agent | Purpose | Skills applied |
|---|---|---|
| `python-backend.md` | FastAPI reviewer UI (invoice queue, detail view, inline correction endpoints), the headless batch command, the reconciliation rules engine, and the money-conversion module | `fastapi-best-practices`, `modern-python-development` |
| `sqlite-database.md` | Schema and data-access layer, integer-cents money columns, mutable extracted-field records, the correction audit trail | `modern-python-development` |
| `bedrock-extraction.md` | The AWS Bedrock extraction call, the vision prompt, field parsing, and the credential-free response cache | `modern-python-development` |
| `testing-expert.md` | Feature-level acceptance tests mapped to `functional-spec.md` criteria, run once at the end of the feature rather than per slice | `pytest-best-practices` |

All four files are committed at their normal repository paths; nothing was
moved for this submission. Each one has a record in `manifest.json`'s
`configuration_records`.

**AWOS slash commands** (`.claude/commands/awos/`, which load
`.awos/commands/` and fill `.awos/templates/`). These drove the
spec-driven workflow: `/awos:product`, `/awos:roadmap`,
`/awos:architecture`, `/awos:spec`, `/awos:tech` and `/awos:tasks` wrote
`context/product/` and `context/spec/`; `/awos:hire` set up the subagents;
`/awos:implement` and `/awos:verify` ran the slices. They were installed in
commit `894bfe1` and used as installed.

**Claude Code project memory.** Three memory files steered the development
agent in every session:

| File | What it tells the agent |
|---|---|
| `writing-style-asd-ste100-orwell.md` | All prose follows ASD-STE100 and Orwell's six rules; code and quotes stay exact. |
| `invoice-reconciliation-exercise-brief.md` | Where the real brief is, and what it requires beyond `domain.md`. |
| `bedrock-measured-behaviour.md` | Use classic `bedrock-runtime`, not Mantle; money can come back as a number. |

They live outside the repository, under the Claude Code project folder.
Redacted snapshots are in `ai-workflow/claude-memory/`. The redactions
are the home-directory path (`<home>`), the AWS account ID
(`<aws-account-id>`) and the session IDs (`<session-id>`). Nothing else
was changed.

**Skills** (`.claude/skills/`): `fastapi-best-practices`,
`modern-python-development`, `pytest-best-practices`. These are
general-purpose conventions that existed before this exercise and apply to
any Python/FastAPI project; none was authored specifically for this
exercise. They are referenced here because each subagent's frontmatter
declares which of them it applies.

**Hooks.** Not used. Neither `.claude/settings.json` nor
`.claude/settings.local.json` contains a `hooks` key.

**Scripts.** One: `.awos/scripts/create-spec-directory.sh`. The
`/awos:spec` command runs it to create the numbered spec folder, here
`context/spec/001-invoice-reconciliation-review/`.

**MCP servers.** Two at project scope (`.mcp.json`, committed at the
repository root), one at user scope:

- `awos-recruitment` — a capability-discovery lookup used while setting up
  the project's agents, not part of the invoice-reconciliation build itself.
- `aws-knowledge-mcp-server` — AWS documentation lookup, used occasionally
  to check Bedrock API behavior.

- `playwright` — browser automation: open, click and screenshot pages of
  the local reviewer web app. Configured at **user scope** in
  `~/.claude.json`, not in `.mcp.json`, as `npx @playwright/mcp@latest`.
  The version is not pinned; this machine resolves 0.0.83. A copy of the
  entry is in `ai-workflow/mcp-user-scope.json`. It holds no secret.

No server holds exercise-specific configuration beyond its connection
settings, and no MCP server was written for this exercise.

**`.claude/settings.json`** (committed, shared, nothing sensitive):

```json
{
  "extraKnownMarketplaces": {
    "awos-marketplace": {
      "source": {
        "source": "github",
        "repo": "provectus/awos"
      }
    }
  }
}
```

**`.claude/settings.local.json`** — exists, but is **not exported** here.
It holds a Bash command permission allowlist and an enabled-MCP-server list.
The allowlist entries embed this machine's home-directory name and a
per-session scratchpad directory name (a UUID) inside absolute file paths.
Those are machine- and session-specific detail, not exercise configuration,
and are not reusable by a reviewer on a different machine — so the file is
described here rather than copied. It contains no credential, token, or
account identifier.

**Environment variables.** See `.env.example` at the repository root. It
lists three names, with no values. `AWS_REGION` and `BEDROCK_MODEL_ID` are
optional overrides of the in-code defaults listed in the table above.
`AWS_PROFILE` is read by the AWS credential chain, not by the application,
when the Bedrock session is under a named profile.
No variable holds a secret; AWS credentials are never read from environment
variables in this codebase (see "Credentials" above).

**Earlier versions.** The full build history is in Git, on `main`, from
project setup onward:

```
uv run git log --oneline
```

or, to see a specific file's history, for example the money converter:

```
uv run git log --oneline -- src/invoice_reconciliation/money.py
```

## One workflow example

**Instruction given:** implement the money-conversion module so that a value
returned by the model — sometimes a JSON string like `"24.00"`, sometimes a
JSON number like `24.0` — converts to integer USD cents without ever routing
a float through `float(x) * 100`, which drifts on values like `19.99`.

**Configuration that affected it:** the `python-backend` subagent
(`.claude/agents/python-backend.md`), which states the exact conversion
method to use — format a numeric input as text with `f"{v:.2f}"` first, then
split on the decimal point and parse each half as an integer — and warns
that a float reaching `str.split` directly raises `AttributeError`.

**How the result was checked:** a unit test asserting that `24.0`, `120.0`,
and `5` convert to `2400`, `12000`, and `500` respectively, alongside the
equivalent string inputs. The test was written first and watched to fail
(RED) before the formatting step was added, confirming it tested real
behavior rather than passing by accident.

**One correction made:** the first draft called `float(x) * 100` directly on
numeric input before checking its type, which passed the integer-valued test
cases by coincidence but drifted on fractional cents for values like
`19.99`. The fix was to route every input — string or number — through the
same `f"{v:.2f}"` formatting step before splitting, so exactly one code path
performs the conversion regardless of input type.

## Reproduce or replay

**Where configuration belongs.** Subagent definitions live in
`.claude/agents/`; skills in `.claude/skills/`; MCP server declarations in
`.mcp.json`; environment variable names in `.env.example` at the repository
root. Nothing needs to move to reproduce this build — all of it is already
at its normal repository location.

**Required tools and versions.** Python 3.11 (pinned in `.python-version`
and `pyproject.toml`'s `requires-python`), managed with `uv`. Dependencies
are declared in `pyproject.toml` and locked in `uv.lock`.

**Command to run, live (requires AWS credentials via `aws login`):**

```
uv run python -m invoice_reconciliation.cli --reset-db --extract --seed-check
```

**Command to run, replayed from cache (no AWS credentials needed):**

```
uv run python -m invoice_reconciliation.cli --reset-db --from-cache --seed-check
```

`--from-cache` reads the six saved responses in
`tests/fixtures/bedrock_responses/` (one per invoice fixture: `clean.json`,
`duplicate.json`, `missing-reference.json`, `quantity-overbill.json`,
`undercharge.json`, `wrong-price.json`) instead of calling Bedrock. Each
file records the `image_sha256` of the invoice it answers for, the
`model_id`, a `captured_at` timestamp, and the `raw_response` exactly as
Bedrock returned it — unmodified. Replay still runs the real parsing and
reconciliation path; only the network call is skipped. `--seed-check`
compares the persisted results against
`tasks/invoices/expected-seed-results.json` and reports pass or fail.

**Hooks.** None configured. There is nothing to enable and nothing a
reviewer needs to read before running the commands above.

## Decisions and limitations

**Why this setup suited the task.** Four subagents split cleanly along the
build's natural seams — API/UI, persistence, model extraction, and testing —
so each could carry domain rules specific to its layer (for example, the
money-conversion method lives with `python-backend`, the integer-cents
schema constraint lives with `sqlite-database`) without one agent definition
growing unmanageably long. No hook was needed because the work is a single
developer session with no recurring automation to trigger. No custom skill
was needed because the three general-purpose skills already covered the
project's stack.

**Why the templates are kept rather than deleted.** The brief says to
"rename" `manifest.template.json` and `README.template.md` to
`manifest.json` and `README.md`. This submission creates the new files
alongside the templates instead of overwriting them in place, so a reviewer
can diff the blank starting point against the filled result without
checking out an earlier commit. If a literal rename (replacing the template
files) is what the brief intends, the template files can be deleted with no
loss — their content is fully reproduced, filled in, in `manifest.json` and
this file.

**What would change with more time.** The `AnthropicBedrockMantle`-to
classic-Bedrock correction (see "Tools and models" above) cost a full
blocked slice before the account entitlement gap was found. A pre-flight
check — one cheap call against both client paths before committing the
spec to one of them — would have caught this earlier.

**Accurate status markers**, as the template requests:

- `used` — development tool, application model config, the three model
  calls, the application prompts, all four subagents, the project memory
  (redacted snapshot), `.claude/settings.json`, root `.env.example`.
- `used` — Playwright MCP (user scope, browser automation).
- `default` — the three general-purpose skills, the AWOS commands and
  their script, the two project MCP servers (used as installed, nothing
  exercise-specific changed).
- `not-used` — Claude Code plugins, IDE plugins, browser extensions,
  hooks, exercise-specific skills.
- `not-exportable` — `.claude/settings.local.json` (machine-specific paths,
  explained above, no credential present).

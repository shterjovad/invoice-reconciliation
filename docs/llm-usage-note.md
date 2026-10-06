# LLM usage note

This note states which tools and models built this project, which parts of
the code they generated, one instruction given to an agent, and one
correction that followed a check.

## Tools and models

**Development.** Claude Code CLI, running Claude Opus 5, as the orchestrating
agent. Four custom subagents handled specialist work:

- `bedrock-extraction` — the Bedrock call, the vision prompt, field parsing,
  and the response cache.
- `python-backend` — FastAPI routes, the reconciliation rules engine, and
  the money conversion module.
- `sqlite-database` — the schema and the data-access layer.
- `testing-expert` — feature-wide acceptance tests at the end of the build.

Their definitions are in `.claude/agents/`.

**Application (runtime).** The deployed app calls `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
on AWS Bedrock, through the `bedrock-runtime` service in `us-east-1`, at
temperature 0.0. This is the model that reads each invoice image and
returns the seven extracted fields.

## Generated components

Agents generated the great majority of the source tree: the FastAPI routes
and templates, the SQLite schema and repository layer, the reconciliation
rules engine, the money conversion module, the Bedrock extraction client and
cache, and the test suite (159 tests across unit, integration, and
feature-wide layers). Domain rules came from `tasks/invoices/domain.md`, not
from agent invention — ambiguous points were recorded, not resolved
silently, in the README's "Settled ambiguities" section.

## One representative instruction

Instruction given to the `python-backend` agent, Slice 11 ("Correct a value
and watch the result change"):

> Validate a reviewer's corrected money value by running it through
> `money.dollars_to_cents` before writing it to the database.

This instruction was wrong, and the agent said so instead of following it.

## One correction and verification step

The agent read how `total_cents` and `unit_cents` are actually stored: as
integer USD cents held in text form, where `"12000"` means $120.00. That is
the same contract `reconciliation/rules.py` reads back (`_parse_cents`).
`money.dollars_to_cents`, by contrast, treats its input as a dollar amount.
Passed `"12000"`, it returns `1200000` — wrong by a factor of 100.

The agent declined the instruction and validated the correction with a
plain base-10 integer parse instead, documenting the reason in
`src/invoice_reconciliation/web/routes.py`:

```
- ``unit_cents`` and ``total_cents`` hold integer USD cents as text
  (``"12000"`` == $120.00), the same contract
  ``reconciliation/rules.py:_parse_cents`` reads back. They are
  validated with that same plain base-10 integer parse, never with
  ``money.dollars_to_cents`` — that function treats its input as a
  dollar amount and would reinterpret ``"12000"`` as $12,000.00.
```

I checked this myself before accepting it: I read `_parse_cents` in
`rules.py`, confirmed it treats `total_cents` as a plain integer count of
cents, and ran `dollars_to_cents("12000")` by hand against the function body
in `money.py` to confirm it returns `1200000`, not `12000`. The instruction
was wrong and the check caught it before anything shipped.

A second, separate correction, recorded in the README's "Six defects" list
and the technical specification: a stored note of mine recorded
`AnthropicBedrockMantle` as the verified Bedrock client class for this
environment. An agent tried it, got a `404` on every model ID tried, on
this account, and stopped instead of silently switching clients. I
reproduced the `404` independently, tried five model IDs (all `404`), and
confirmed that classic `bedrock-runtime` returns a real completion with the
same credentials, region, and model ID. `AnthropicBedrockMantle` targets a
separate AWS service (`bedrock-mantle.us-east-1.api.aws`) with its own
entitlement, distinct from classic Bedrock
(`bedrock-runtime.us-east-1.amazonaws.com`). The note and the technical
specification were both corrected, with the reason stated in each. A
measured claim, recorded as fact, was overturned by testing.

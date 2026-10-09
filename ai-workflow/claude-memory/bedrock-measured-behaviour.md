---
name: bedrock-measured-behaviour
description: "Measured Bedrock facts for this project — use classic bedrock-runtime, NOT AnthropicBedrockMantle (404s on this account), and money fields come back as JSON numbers about one call in three."
metadata: 
  node_type: memory
  type: project
  originSessionId: <session-id>
  modified: 2026-10-06T19:03:00.257Z
---

Two facts the user measured against real Bedrock calls. Neither is a guess, and both contradict
what a reasonable person would assume from the SDK docs.

**1. CORRECTED 2026-10-06 — use classic Bedrock, not Mantle.**
An earlier version of this memory said the client class is `AnthropicBedrockMantle(aws_region=...)`.
**That is wrong for this account and cost a blocked slice.** Retested directly:

- `AnthropicBedrockMantle` targets `bedrock-mantle.us-east-1.api.aws` — a *different AWS service*
  from classic Bedrock, with its own model registry and its own entitlement.
- On account `<aws-account-id>` it returns `404 not_found` for **every** model tried: the full
  inference-profile ID, the bare name, Sonnet 4.5, Sonnet 5, Opus 5. Not one worked.
- The SDK itself knows the models exist — it emitted a deprecation warning for `claude-sonnet-4-5`
  while the endpoint still 404'd. So the account has no Mantle entitlement at all.
- Classic Bedrock via `boto3` `bedrock-runtime.invoke_model`, with the **same credentials, same
  region, same model ID**, returns a real completion and usage counts.

**Use `bedrock-runtime`.** An external assessor is also far likelier to have it than Mantle.

**2. The model returns money fields as JSON numbers about one call in three.**
Three identical calls on `tasks/invoices/images/wrong-price.png` gave two shapes:
- One returned numbers: `quantity: 5`, `unit_price_usd: 24.0`, `total_usd: 120.0`.
- Two returned strings.

A float that reaches `s.split(".")` raises `AttributeError`. That crashes the invoice. It is not a
wrong figure, so a money test that only uses strings will never catch it.

**The two defences, both needed:**
- The response schema types the money fields as `"string"`. This removes the variance at source.
- The converter accepts `str | float | int`. For a number it formats with `f"{v:.2f}"` first, then
  splits and parses as integers. A schema is a request, not a guarantee.

**Also:** do not validate money with a regular expression such as `^\d+\.\d{2}$` in the parser. It
rejects `24.0` and marks a good invoice `failed`. Let the converter decide what is valid, so one
rule governs.

**Why:** the technical specification first typed the converter `dollars_to_cents(s: str)`. That
signature would have crashed on roughly one run in three, and no test in the original table would
have found it.

**How to apply:** test the converter with `24.0`, `120.0` and `5`, not only with strings. See
[[invoice-reconciliation-exercise-brief]] for the brief, which demands saved real responses — those
saved responses will contain whichever shape the model returned on the day.

Model ID: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. The `us.` prefix is required.

"""Classic AWS Bedrock client construction and the one live extraction call.

This project originally targeted ``AnthropicBedrockMantle`` (the
`anthropic` SDK's Bedrock-Mantle surface, a separate AWS-hosted service
from classic Bedrock). That path was verified to 404 on every model ID
tried, including bare names the SDK itself recognises with a deprecation
warning — confirmed independently by two people against the same
credentials, region and model ID. The account has no Mantle entitlement
at all. Classic Bedrock (``boto3`` ``bedrock-runtime``, ``invoke_model``),
with the identical model ID, was confirmed working against the same
credentials and region. This module now uses that path. The change and
its reason are recorded in ``technical-considerations.md``,
``architecture.md`` and ``tasks.md``.

The client is built **lazily, inside the live call only**. Nothing at
import time, and nothing in a module-level constant, touches the AWS
credential chain. That laziness is what makes a credential-free replay
run (reading saved responses from the cache) possible: importing this
module, or importing anything that imports it, never requires AWS
credentials to be present.

Structured output is requested via a single forced tool call (``tool_choice``
pinned to the one extraction tool), whose ``input_schema`` is the seven-field
schema in ``prompts.py`` (``EXTRACTION_SCHEMA``). Classic Bedrock's
``invoke_model`` has no ``output_config`` structured-output parameter, so the
forced single tool call is still how the response shape is constrained.

**Model and API failures.** Every failure of the call itself (no
credentials, throttling, an access or validation error from Bedrock, a
network timeout, a response body that is not JSON) is raised as one
project error, ``ModelCallError``, with a readable reason. The caller
(``db.ingest``) catches it for each invoice, marks that invoice failed and
continues, so one failed call never stops the batch.
"""

from __future__ import annotations

import base64
import json

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.prompts import EXTRACTION_PROMPT, EXTRACTION_SCHEMA


class ModelCallError(Exception):
    """The model call itself failed: no credentials, throttling, network, or
    a response body that could not be read."""


def _describe(exc: Exception) -> str:
    """A readable reason, with the Bedrock error code when there is one."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return f"{type(exc).__name__} ({code}): {exc}"
    return f"{type(exc).__name__}: {exc}"

__all__ = ["build_client", "extract_invoice_fields"]

_TOOL_NAME = "record_invoice_fields"
_ANTHROPIC_VERSION = "bedrock-2023-05-31"


def build_client(config: ModelConfig):
    """Construct the ``boto3`` ``bedrock-runtime`` client. Call this only on the live path.

    ``boto3`` is imported lazily inside this function (not at module
    import time) so that importing
    ``invoice_reconciliation.extraction.client`` — which a replay run
    does, to reach ``extract_invoice_fields``'s sibling parsing code —
    never requires the AWS credential chain to resolve. The credential
    chain is only consulted once this function actually runs.
    """
    try:
        import boto3

        return boto3.client("bedrock-runtime", region_name=config.region)
    except Exception as exc:  # noqa: BLE001 - any failure here means no model access
        raise ModelCallError(f"could not build the Bedrock client: {_describe(exc)}") from exc


def _image_content_block(image_bytes: bytes, *, media_type: str = "image/png") -> dict:
    encoded = base64.standard_b64encode(image_bytes).decode("utf-8")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": encoded,
        },
    }


def extract_invoice_fields(client, config: ModelConfig, image_bytes: bytes) -> dict:
    """Run one live extraction call against classic Bedrock. Returns the raw response body.

    The caller (the pipeline, in a later slice) is responsible for passing
    the raw response to ``extraction.parser.parse`` and for persisting it
    via ``extraction.cache`` — this function performs only the network
    call. Separating the two keeps the parsing path identical between a
    live call and a cache replay.

    The returned value is the parsed JSON response body (a ``dict``) in
    the Anthropic Messages-API shape (``content``, ``usage``, ``model``,
    ...) — the same shape ``extraction.parser.parse`` already expects,
    since it reads ``content``/``usage`` generically from either an
    attribute or a dict key.

    ``config.temperature`` is sent in the request body: classic Bedrock's
    ``invoke_model`` accepts it (verified against a live call), unlike the
    Bedrock-Mantle surface this project tried first, which rejected it
    outright. ``temperature=0.0`` is set for repeatable extraction.
    """
    tool = {
        "name": _TOOL_NAME,
        "description": "Record the seven extracted invoice fields.",
        "input_schema": EXTRACTION_SCHEMA,
    }

    body = {
        "anthropic_version": _ANTHROPIC_VERSION,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        "messages": [
            {
                "role": "user",
                "content": [
                    _image_content_block(image_bytes),
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    }

    from botocore.exceptions import BotoCoreError, ClientError

    try:
        response = client.invoke_model(
            modelId=config.model_id,
            body=json.dumps(body),
        )
        return json.loads(response["body"].read())
    except (BotoCoreError, ClientError) as exc:
        # BotoCoreError covers NoCredentialsError, timeouts and connection
        # errors; ClientError covers Bedrock's own refusals, such as
        # ThrottlingException or AccessDeniedException.
        raise ModelCallError(f"model call failed: {_describe(exc)}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelCallError(f"model response could not be read: {_describe(exc)}") from exc

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
"""

from __future__ import annotations

import base64
import json

from invoice_reconciliation.config import ModelConfig
from invoice_reconciliation.prompts import EXTRACTION_PROMPT, EXTRACTION_SCHEMA

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
    import boto3

    return boto3.client("bedrock-runtime", region_name=config.region)


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

    response = client.invoke_model(
        modelId=config.model_id,
        body=json.dumps(body),
    )
    return json.loads(response["body"].read())

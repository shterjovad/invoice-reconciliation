"""Model configuration: the model ID, region, and call parameters.

One frozen ``ModelConfig`` is the single source for these values. No other
module hardcodes the model ID, the region, or the call parameters — every
caller imports ``ModelConfig`` from here.

The ``us.`` inference-profile prefix on the model ID is required: a bare
model ID fails on AWS Bedrock with ``ValidationException: Invocation with
on-demand throughput isn't supported``. This is measured behaviour, not a
guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["ModelConfig", "DEFAULT_MODEL_ID", "DEFAULT_REGION"]

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_REGION = "us-east-1"


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """Call parameters for the extraction model.

    ``model_id`` and ``region`` default to the values read from the
    ``BEDROCK_MODEL_ID`` and ``AWS_REGION`` environment variables, falling
    back to the project defaults above when those variables are unset.

    ``temperature = 0.0`` for repeatable extraction: the same invoice image
    should read the same way on every call.
    """

    model_id: str = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    region: str = os.environ.get("AWS_REGION", DEFAULT_REGION)
    max_tokens: int = 1024
    temperature: float = 0.0

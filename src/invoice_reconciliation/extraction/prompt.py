"""Re-export of the extraction prompt — the text itself now lives in
``invoice_reconciliation.prompts``, alongside every other prompt and
response schema this project sends to a model.

Kept as a thin re-export (rather than deleted) so any import of
``invoice_reconciliation.extraction.prompt.EXTRACTION_PROMPT`` keeps
working unchanged.
"""

from __future__ import annotations

from invoice_reconciliation.prompts import EXTRACTION_PROMPT

__all__ = ["EXTRACTION_PROMPT"]

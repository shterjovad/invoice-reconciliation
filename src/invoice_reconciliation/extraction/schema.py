"""Re-export of the extraction response schema — the schema itself now
lives in ``invoice_reconciliation.prompts``, alongside every other prompt
and response schema this project sends to a model.

Kept as a thin re-export (rather than deleted) so any import of
``invoice_reconciliation.extraction.schema.EXTRACTION_SCHEMA`` keeps
working unchanged.
"""

from __future__ import annotations

from invoice_reconciliation.prompts import EXTRACTION_SCHEMA

__all__ = ["EXTRACTION_SCHEMA"]

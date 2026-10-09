"""The headless batch command: the CLI entry point to the pipeline.

Wires ``db/ingest.py`` (ingest), ``pipeline.run_batch`` (reconcile), and
``seed_check.py`` (optional comparison against the oracle) together, then
prints a human-readable report. This module contains no reconciliation
logic of its own: it parses arguments, calls the shared pipeline, prints a
summary, and returns an exit code. The web view calls the same
``pipeline`` functions directly; this file is not a second implementation
of anything.

Usage::

    uv run python -m invoice_reconciliation.cli --reset-db --seed-check
    uv run python -m invoice_reconciliation.cli --extract --reset-db --seed-check
    uv run python -m invoice_reconciliation.cli --from-cache --seed-check

Flags (technical-considerations.md section 2.7, plus ``--extract``):

====================  ===========================================================
Flag                  Purpose
====================  ===========================================================
--extract             Read invoice field values from each image via a live
                       Bedrock call, instead of the prepared seed values.
                       Needs AWS credentials. Default: off (prepared-record
                       ingest, the path the 82-test suite and the seed
                       check depend on).
--from-cache          Run extraction from saved responses. No credentials needed.
--refresh-cache       Force live calls and overwrite the saved responses.
--seed-check          After persisting, compare results with expected-seed-results.json.
--reset-db            Drop and rebuild the database before ingest.
--ingest-dir PATH     Source directory. Default tasks/invoices/.
--db-path PATH        Database location.
--log-level           Logging verbosity.
====================  ===========================================================

``--from-cache`` replays saved Bedrock responses from
``tests/fixtures/bedrock_responses/`` through the same extraction-ingest
path as ``--extract``: only the network call is swapped out for a cache
read, so ``extraction.parser.parse`` and every downstream step run
identically. It never touches the AWS credential chain. ``--refresh-cache``
instead forces a live call for every invoice and overwrites the saved
responses. The two flags are mutually exclusive.

Exit codes
----------

Two signals are kept separate, per technical-considerations.md section
2.8: whether any invoice failed to process, and whether the seed
comparison passed. They are different problems ("the model failed" vs.
"the rules are wrong") and are never merged into one number.

====  ========================================================================
Code  Meaning
====  ========================================================================
0     Success. All invoices processed without a ``failed`` status, and
      (if ``--seed-check`` was passed) the seed comparison passed.
1     Seed comparison failed (``--seed-check`` was passed and at least one
      case did not match the oracle), AND no invoice had status
      ``failed`` and the batch did not crash. This is a rules/data
      problem, not a processing crash.
2     One or more invoices processed with status ``failed``, OR the batch
      itself raised an unhandled exception before persisting a result for
      every invoice (e.g. malformed money text the rules engine cannot
      parse — see "Known gap" below). This is a processing problem,
      regardless of whether ``--seed-check`` was requested.
3     Both: at least one invoice has status ``failed`` AND (if requested)
      the seed comparison also failed. Reported as a combination so a
      reviewer sees both facts, rather than one masking the other. Note
      this code is reachable only via the ``failed``-status path, not via
      a batch crash — a crash aborts before ``--seed-check`` runs at all,
      so a crash alone always reports as code 2, never 3.
====  ========================================================================

Processing failure (exit codes 2/3) always takes priority in the sense
that it is reported whenever present, regardless of the seed-check
outcome; it is not silently absorbed into the seed-check's code 1.

Failure isolation: ``rules.reconcile``'s ``_parse_cents`` raises
``MoneyFormatError`` on malformed money text. ``rules.reconcile`` itself
stays pure and keeps raising — it never swallows this. ``pipeline.
recalculate_one`` is what catches ``MoneyFormatError`` per invoice and
records a ``failed`` status with no amounts, so one invoice with malformed
stored money does not stop the rest of the batch from processing. A
genuine programming bug elsewhere is not caught here and still propagates
to the ``except Exception`` below, which exists only as a last-resort
process-boundary guard (so a reviewer gets exit code 2 and a message
instead of a raw traceback), not as a substitute for per-invoice isolation.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.db.ingest import (
    DEFAULT_DB_PATH,
    DEFAULT_SEED_PATH,
    ExtractionIngestOutcome,
    run_ingest,
)
from invoice_reconciliation.pipeline import InvoiceOutcome, run_batch
from invoice_reconciliation.reconciliation.rules import ReconciliationResult
from invoice_reconciliation.seed_check import DEFAULT_EXPECTED_PATH, run_seed_check

__all__ = ["build_parser", "main"]

logger = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_SEED_CHECK_FAILED = 1
EXIT_PROCESSING_FAILED = 2
EXIT_PROCESSING_AND_SEED_CHECK_FAILED = 3

_STATUS_FAILED = "failed"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the batch command."""
    parser = argparse.ArgumentParser(
        prog="invoice_reconciliation.cli",
        description=(
            "Headless batch command: ingest fixtures, run reconciliation for "
            "every invoice, optionally compare results against the seed "
            "oracle, and print a reviewer-readable report."
        ),
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help=(
            "Read invoice field values from each image via a live Bedrock "
            "call, instead of the prepared seed values. Needs AWS "
            "credentials. Default: off."
        ),
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Run extraction from saved responses. No credentials needed.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Force live calls and overwrite the saved responses.",
    )
    parser.add_argument(
        "--no-model-notes",
        action="store_true",
        help=(
            "Do not call the model to draft discrepancy notes; use the "
            "calculated note instead. Drafting is on by default, so the "
            "notes are already written by the time a reviewer opens the "
            "web view. Without credentials the batch falls back on its "
            "own, so this flag is only needed to skip the calls outright."
        ),
    )
    parser.add_argument(
        "--seed-check",
        action="store_true",
        help="After persisting, compare results with expected-seed-results.json.",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Drop and rebuild the database before ingest.",
    )
    parser.add_argument(
        "--ingest-dir",
        type=Path,
        default=Path("tasks/invoices"),
        help="Source directory. Default tasks/invoices/.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Database location. Default {DEFAULT_DB_PATH}.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity. Default WARNING.",
    )
    return parser


def _format_amount(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:.2f}"


def _print_outcome(outcome: InvoiceOutcome) -> None:
    result: ReconciliationResult = outcome.result
    print(
        f"  [{outcome.file_id}] status={result.status} "
        f"expected={_format_amount(result.expected_cents)} "
        f"billed={_format_amount(result.billed_cents)} "
        f"difference={_format_amount(result.difference_cents)}"
        + (
            f" count_as_payable={result.count_as_payable}"
            if result.count_as_payable is not None
            else ""
        )
    )


def _print_extraction_report(outcomes: list[ExtractionIngestOutcome]) -> None:
    """Print one line per invoice's live-extraction outcome.

    Reports the model id and token counts the real response carried — not
    ``ModelConfig`` — so a replayed entry (a later slice) can be told apart
    from a live one by what it actually reports serving.
    """
    print(f"Extraction ({len(outcomes)} invoice(s)):")
    for outcome in outcomes:
        if outcome.succeeded:
            print(
                f"  [{outcome.file_id}] OK model={outcome.model_id} "
                f"input_tokens={outcome.input_tokens} "
                f"output_tokens={outcome.output_tokens} "
                f"source={outcome.source}"
            )
        else:
            print(f"  [{outcome.file_id}] FAILED — {outcome.error}")

    failed = [o.file_id for o in outcomes if not o.succeeded]
    if failed:
        print(f"Extraction: {len(failed)} invoice(s) FAILED to extract: {failed}")
    else:
        print("Extraction: all invoices extracted successfully.")


def _print_batch_report(outcomes: list[InvoiceOutcome]) -> int:
    """Print the per-invoice summary and return the number of failures."""
    print(f"Processed {len(outcomes)} invoice(s):")
    failed_count = 0
    for outcome in outcomes:
        _print_outcome(outcome)
        if outcome.result.status == _STATUS_FAILED:
            failed_count += 1

    if failed_count:
        print(f"Processing: {failed_count} invoice(s) FAILED to process.")
    else:
        print("Processing: all invoices processed (no 'failed' status).")

    return failed_count


def main(argv: list[str] | None = None) -> int:
    """Run the batch pipeline from parsed CLI arguments. Returns an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.from_cache and args.refresh_cache:
        parser.error("--from-cache and --refresh-cache are mutually exclusive")

    seed_path = args.ingest_dir / "seed.json"
    images_dir = args.ingest_dir / "images"
    expected_path = args.ingest_dir / "expected-seed-results.json"
    if not expected_path.exists():
        expected_path = DEFAULT_EXPECTED_PATH

    print(
        f"Ingesting fixtures from {args.ingest_dir} into {args.db_path} "
        f"(reset_db={args.reset_db}, extract={args.extract}, "
        f"from_cache={args.from_cache}, refresh_cache={args.refresh_cache})..."
    )
    try:
        extraction_outcomes = run_ingest(
            db_path=args.db_path,
            seed_path=seed_path if seed_path.exists() else DEFAULT_SEED_PATH,
            images_dir=images_dir,
            reset_db=args.reset_db,
            use_extraction=args.extract,
            use_cache=args.from_cache,
            refresh_cache=args.refresh_cache,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report, don't crash
        # ingest isolates each invoice's failures itself; reaching here
        # means a failure outside any one invoice (for example an
        # unreadable seed file).
        logger.exception("Ingest raised an unhandled exception")
        print(f"Ingest: CRASHED before completing — {type(exc).__name__}: {exc}")
        return EXIT_PROCESSING_FAILED
    if extraction_outcomes is not None:
        _print_extraction_report(extraction_outcomes)

    with connect(args.db_path) as conn:
        try:
            outcomes = run_batch(
                conn, draft_notes_with_model=not args.no_model_notes
            )
        except Exception as exc:  # noqa: BLE001 - CLI boundary: report, don't crash
            logger.exception("Batch processing raised an unhandled exception")
            print(
                f"Processing: batch processing CRASHED before completing — "
                f"{type(exc).__name__}: {exc}"
            )
            return EXIT_PROCESSING_FAILED

        failed_count = _print_batch_report(outcomes)

        seed_check_failed = False
        if args.seed_check:
            print()
            report = run_seed_check(conn, expected_path=expected_path)
            print("Seed check results:")
            for case in report.cases:
                prefix = "PASS" if case.passed else "FAIL"
                print(f"  [{prefix}] {case.message}")
            if report.comparison_passed:
                print("Seed comparison: PASSED — all cases match the oracle exactly.")
            else:
                seed_check_failed = True
                failed_cases = [case.file_id for case in report.cases if not case.passed]
                print(f"Seed comparison: FAILED — mismatched cases: {failed_cases}")

    if failed_count and seed_check_failed:
        return EXIT_PROCESSING_AND_SEED_CHECK_FAILED
    if failed_count:
        return EXIT_PROCESSING_FAILED
    if seed_check_failed:
        return EXIT_SEED_CHECK_FAILED
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())

"""Compare the database's reconciliation results against the oracle.

``tasks/invoices/expected-seed-results.json`` is the supplied source of
truth for the four seeded fixtures. Each entry's key set differs by
status — a ``duplicate`` record carries ``count_as_payable`` and omits the
cent keys entirely; an ``unresolved`` record carries explicit ``null`` for
``expected_cents`` and ``difference_cents`` and omits ``billed_cents``
altogether. This module does not normalise those shapes into one uniform
record before comparing: it serialises each stored result into the exact
shape its own status implies, then compares key sets and values against
the oracle's entry for that ``file_id``. A result serialised with an
extra or missing key fails even if every value it does share is correct —
that is the point of comparing shape, not just values.

Two signals stay separate on purpose. Whether an invoice failed to
process (``status == "failed"``, or no invoice found for an oracle
``file_id``) and whether the seed comparison passed are different facts;
nothing here merges them into one exit code. ``SeedCheckReport`` exposes
both independently; only ``main`` picks one process exit code from the
comparison result.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from invoice_reconciliation.db import repository
from invoice_reconciliation.db.connection import connect
from invoice_reconciliation.db.ingest import DEFAULT_DB_PATH

__all__ = [
    "CaseResult",
    "SeedCheckReport",
    "DEFAULT_EXPECTED_PATH",
    "run_seed_check",
    "main",
]

DEFAULT_EXPECTED_PATH = Path("tasks/invoices/expected-seed-results.json")


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The comparison outcome for one ``file_id``."""

    file_id: str
    passed: bool
    invoice_found: bool
    expected: dict | None
    actual: dict | None
    message: str


@dataclass(frozen=True, slots=True)
class SeedCheckReport:
    """The overall outcome of a seed check run.

    ``comparison_passed`` is the only signal this module's exit code is
    derived from. ``any_invoice_missing`` is reported alongside it but is
    a distinct fact: an oracle entry whose invoice does not exist in the
    database at all is itself a case failure (and so already folds into
    ``comparison_passed`` being False), but a caller may want to know
    specifically whether that was the cause.
    """

    cases: list[CaseResult]

    @property
    def comparison_passed(self) -> bool:
        return all(case.passed for case in self.cases)

    @property
    def any_invoice_missing(self) -> bool:
        return any(not case.invoice_found for case in self.cases)


def _serialise_result(row: sqlite3.Row, *, file_id: str) -> dict:
    """Build the shape-correct record for a stored ``reconciliation_results`` row.

    ``file_id`` lives on the ``invoices`` table, not on
    ``reconciliation_results``, so the caller (which already looked up the
    invoice to get its ``invoice_id``) passes it in explicitly.

    ``count_as_payable`` is stored in SQLite as ``0``/``1``/``NULL`` (no
    native boolean type). It is converted here to a real ``bool`` so that
    ``0`` compares equal to the oracle's JSON ``false`` by Python value
    equality (``0 == False`` is also true in Python, but this still
    performs the conversion explicitly rather than relying on that
    coincidence, so the serialised dict itself carries a true bool as a
    JSON-shaped value would).
    """
    status = row["status"]

    if status == "duplicate":
        return {
            "file_id": file_id,
            "status": status,
            "count_as_payable": bool(row["count_as_payable"]),
        }

    if status == "unresolved":
        return {
            "file_id": file_id,
            "status": status,
            "expected_cents": row["expected_cents"],
            "difference_cents": row["difference_cents"],
        }

    # reconciled / discrepant (and any other status carrying full amounts).
    return {
        "file_id": file_id,
        "status": status,
        "expected_cents": row["expected_cents"],
        "billed_cents": row["billed_cents"],
        "difference_cents": row["difference_cents"],
    }


def _compare_case(file_id: str, expected: dict, actual: dict | None) -> CaseResult:
    if actual is None:
        return CaseResult(
            file_id=file_id,
            passed=False,
            invoice_found=False,
            expected=expected,
            actual=None,
            message=f"{file_id}: no invoice/result found in database for this file_id",
        )

    expected_keys = set(expected.keys())
    actual_keys = set(actual.keys())

    if expected_keys != actual_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        parts = []
        if missing:
            parts.append(f"missing keys {sorted(missing)}")
        if extra:
            parts.append(f"unexpected extra keys {sorted(extra)}")
        return CaseResult(
            file_id=file_id,
            passed=False,
            invoice_found=True,
            expected=expected,
            actual=actual,
            message=f"{file_id}: key set mismatch — {', '.join(parts)} "
            f"(expected {sorted(expected_keys)}, got {sorted(actual_keys)})",
        )

    mismatches = [
        f"{key}: expected {expected[key]!r}, got {actual[key]!r}"
        for key in sorted(expected_keys)
        if expected[key] != actual[key]
    ]
    if mismatches:
        return CaseResult(
            file_id=file_id,
            passed=False,
            invoice_found=True,
            expected=expected,
            actual=actual,
            message=f"{file_id}: value mismatch — " + "; ".join(mismatches),
        )

    return CaseResult(
        file_id=file_id,
        passed=True,
        invoice_found=True,
        expected=expected,
        actual=actual,
        message=f"{file_id}: OK ({actual['status']})",
    )


def run_seed_check(
    conn: sqlite3.Connection,
    *,
    expected_path: Path = DEFAULT_EXPECTED_PATH,
) -> SeedCheckReport:
    """Compare the database's reconciliation results against the oracle file.

    Reads ``expected_path`` (the supplied oracle, never modified), and for
    each entry looks up the matching invoice by ``file_id``, serialises its
    stored ``reconciliation_results`` row into the shape its status implies,
    and compares key sets and values against the oracle entry exactly —
    no normalisation of either side's keys.
    """
    with expected_path.open("r", encoding="utf-8") as f:
        expected_entries: list[dict] = json.load(f)

    cases: list[CaseResult] = []
    for expected in expected_entries:
        file_id = expected["file_id"]

        invoice_row = repository.get_invoice_by_file_id(conn, file_id=file_id)
        actual: dict | None = None
        if invoice_row is not None:
            result_row = repository.get_reconciliation_result(
                conn, invoice_id=invoice_row["invoice_id"]
            )
            if result_row is not None:
                actual = _serialise_result(result_row, file_id=file_id)

        cases.append(_compare_case(file_id, expected, actual))

    return SeedCheckReport(cases=cases)


def _print_report(report: SeedCheckReport) -> None:
    print("Seed check results:")
    for case in report.cases:
        prefix = "PASS" if case.passed else "FAIL"
        print(f"  [{prefix}] {case.message}")

    if report.comparison_passed:
        print("Seed comparison: PASSED — all cases match the oracle exactly.")
    else:
        failed = [case.file_id for case in report.cases if not case.passed]
        print(f"Seed comparison: FAILED — mismatched cases: {failed}")


def main(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    expected_path: Path = DEFAULT_EXPECTED_PATH,
) -> int:
    """Run the seed check against the database at ``db_path`` and print a report.

    Returns ``0`` if the comparison passed, ``1`` otherwise. This exit code
    reflects only the comparison result — it is not conflated with whether
    any invoice failed to process (that fact is available on the returned
    report's ``any_invoice_missing`` / per-case ``invoice_found``, but is a
    separate concern a caller may inspect independently).
    """
    with connect(db_path) as conn:
        report = run_seed_check(conn, expected_path=expected_path)

    _print_report(report)
    return 0 if report.comparison_passed else 1


if __name__ == "__main__":
    sys.exit(main())

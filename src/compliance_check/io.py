"""Reading records and writing results.

The write signature follows the convention used elsewhere in this codebase: the complete
result set is the required positional argument and any narrowing is an optional keyword.
The worst a forgetful caller can do is fail to filter, which is visible, rather than drop
rows from the audit trail, which is not.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from compliance_check.models import CheckResult, Outcome, Record, Status

#: Shared between every output format, so a column cannot exist in the CSV and be missing
#: from the JSON.
RECORD_FIELDS = (
    "record_id",
    "framework",
    "as_of",
    "version",
    "digest",
    "outcome",
    "worst_severity",
    "checked",
    "passed",
    "failed",
    "errored",
    "skipped",
    "note",
)

FINDING_FIELDS = (
    "record_id",
    "requirement_id",
    "title",
    "severity",
    "status",
    "counts_against",
    "detail",
)


class RecordFileError(Exception):
    """The input file could not be turned into records."""


#: Column names that are part of the record's identity rather than its attributes.
_STRUCTURAL = frozenset({"record_id", "id", "category"})


def load_records(path: Path) -> list[Record]:
    """Read records from CSV or JSON.

    A blank CSV cell is dropped rather than stored as an empty string. "The column exists
    and is empty" and "there is no such column" both mean the attribute was not declared,
    and collapsing them here means a rule using `absent` behaves the same whether the
    exporter emitted the column or not.
    """
    if not path.exists():
        raise RecordFileError(f"{path} does not exist")

    if path.suffix.lower() == ".json":
        rows = _read_json(path)
    elif path.suffix.lower() in (".csv", ".tsv", ".txt"):
        rows = _read_csv(path)
    else:
        raise RecordFileError(f"{path.name}: expected a .csv or .json file")

    records: list[Record] = []
    for number, row in enumerate(rows, start=1):
        record_id = str(row.get("record_id") or row.get("id") or f"row-{number}")
        attributes = dict(row.get("attributes") or {})
        for key, value in row.items():
            if key in _STRUCTURAL or key == "attributes":
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if value is None:
                continue
            attributes[key] = value
        records.append(
            Record(
                record_id=record_id,
                category=str(row.get("category") or ""),
                attributes=attributes,
            )
        )
    return records


def _read_csv(path: Path) -> list[dict[str, Any]]:
    # utf-8-sig because a CSV exported from a spreadsheet usually carries a BOM, which
    # otherwise becomes part of the first column's name and makes `record_id` unfindable.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise RecordFileError(f"{path.name}: no data rows")
    return [{k: v for k, v in row.items() if k} for row in rows]


def _read_json(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecordFileError(
            f"{path.name}: invalid JSON ({exc.msg} at line {exc.lineno})"
        ) from exc

    if isinstance(data, dict):
        data = data.get("records", data.get("items", []))
    if not isinstance(data, list):
        raise RecordFileError(f"{path.name}: expected a list of records")
    if not data:
        raise RecordFileError(f"{path.name}: no records")

    rows = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise RecordFileError(f"{path.name}: item {index} is not an object")
        rows.append(item)
    return rows


def _record_row(result: CheckResult) -> dict[str, Any]:
    counts = {status: 0 for status in Status}
    for finding in result.findings:
        counts[finding.status] += 1
    worst = result.worst_severity
    return {
        "record_id": result.record_id,
        "framework": result.framework,
        "as_of": result.as_of.isoformat(),
        "version": result.version or "",
        "digest": result.digest or "",
        "outcome": result.outcome.value,
        "worst_severity": worst.value if worst else "",
        "checked": sum(n for status, n in counts.items() if status is not Status.SKIP),
        "passed": counts[Status.PASS],
        "failed": counts[Status.FAIL],
        "errored": counts[Status.ERROR],
        "skipped": counts[Status.SKIP],
        "note": result.note,
    }


def _finding_rows(result: CheckResult) -> list[dict[str, Any]]:
    return [
        {
            "record_id": result.record_id,
            "requirement_id": finding.requirement_id,
            "title": finding.title,
            "severity": finding.severity.value,
            "status": finding.status.value,
            "counts_against": finding.counts_against,
            "detail": finding.detail,
        }
        for finding in result.findings
    ]


def summarise(results: list[CheckResult]) -> dict[str, Any]:
    by_outcome = {outcome.value: 0 for outcome in Outcome}
    for result in results:
        by_outcome[result.outcome.value] += 1
    return {
        "total": len(results),
        **by_outcome,
        # Anything that is not a clean pass needs a human. Reported explicitly so nobody
        # has to work out that NO_RULES and INDETERMINATE are not successes.
        "needs_attention": sum(1 for r in results if r.outcome is not Outcome.COMPLIANT),
    }


def write_results(
    results: list[CheckResult],
    directory: Path,
    fmt: str = "csv",
    *,
    emit: list[CheckResult] | None = None,
) -> list[Path]:
    """Write `results` to `directory`.

    `results` is always written in full to the JSON report. `emit`, when given, narrows
    only the tabular output -- so a `--failures-only` flag cannot quietly shrink the
    record of what was checked.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tabular = results if emit is None else emit
    written: list[Path] = []

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "summary": summarise(results),
        "records": [_record_row(result) for result in results],
        "findings": [row for result in results for row in _finding_rows(result)],
    }
    report_path = directory / "report.json"
    report_path.write_text(json.dumps(report, indent=2, default=_json_default), encoding="utf-8")
    written.append(report_path)

    if fmt == "json":
        return written

    if fmt == "excel":
        written.extend(_write_excel(tabular, directory))
        return written

    records_path = directory / "results.csv"
    with records_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RECORD_FIELDS))
        writer.writeheader()
        writer.writerows(_record_row(result) for result in tabular)
    written.append(records_path)

    findings_path = directory / "findings.csv"
    with findings_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(FINDING_FIELDS))
        writer.writeheader()
        for result in tabular:
            writer.writerows(_finding_rows(result))
    written.append(findings_path)

    return written


def _write_excel(results: list[CheckResult], directory: Path) -> list[Path]:
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # pragma: no cover - exercised by the error path only
        raise RecordFileError(
            "Excel output needs openpyxl. Install it with: pip install 'compliance-check[excel]'"
        ) from exc

    book = Workbook()
    sheet = book.active
    sheet.title = "results"
    sheet.append(list(RECORD_FIELDS))
    for result in results:
        row = _record_row(result)
        sheet.append([row[field] for field in RECORD_FIELDS])

    detail = book.create_sheet("findings")
    detail.append(list(FINDING_FIELDS))
    for result in results:
        for row in _finding_rows(result):
            detail.append([row[field] for field in FINDING_FIELDS])

    path = directory / "results.xlsx"
    book.save(path)
    return [path]


def _json_default(value: Any) -> str:
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)

"""Loading records and writing results."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from compliance_check.core.evaluate import check_all
from compliance_check.io import (
    FINDING_FIELDS,
    RECORD_FIELDS,
    RecordFileError,
    load_records,
    summarise,
    write_results,
)
from compliance_check.models import Outcome
from compliance_check.rules.registry import Registry
from tests.conftest import SAMPLE_RECORDS, requires_openpyxl


def write_csv(path: Path, rows: list[dict[str, Any]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


class TestLoadRecords:
    def test_the_shipped_sample_loads(self) -> None:
        records = load_records(SAMPLE_RECORDS)
        assert len(records) == 8
        assert [r.record_id for r in records] == [f"R-{n}" for n in range(1, 9)]

    def test_the_category_is_kept_separate_from_the_attributes(self) -> None:
        records = load_records(SAMPLE_RECORDS)
        first = records[0]
        assert first.category == "toys"
        assert "category" not in first.attributes

    def test_a_blank_cell_is_dropped_rather_than_stored_as_empty(self, tmp_path: Path) -> None:
        """ "the column is empty" and "there is no column" both mean "not declared"."""
        path = write_csv(tmp_path / "r.csv", [{"record_id": "A", "colour": "", "size": "big"}])
        record = load_records(path)[0]
        assert "colour" not in record.attributes
        assert record.attributes == {"size": "big"}

    def test_a_whitespace_cell_is_also_dropped(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "r.csv", [{"record_id": "A", "colour": "   "}])
        assert load_records(path)[0].attributes == {}

    def test_id_is_accepted_as_an_alias_for_record_id(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "r.csv", [{"id": "A-1", "size": "big"}])
        assert load_records(path)[0].record_id == "A-1"

    def test_a_missing_id_falls_back_to_the_row_number(self, tmp_path: Path) -> None:
        path = write_csv(tmp_path / "r.csv", [{"size": "big"}, {"size": "small"}])
        assert [r.record_id for r in load_records(path)] == ["row-1", "row-2"]

    def test_a_bom_does_not_hide_the_first_column(self, tmp_path: Path) -> None:
        """Spreadsheets write a BOM, which otherwise becomes part of the column name."""
        path = tmp_path / "bom.csv"
        path.write_text("\ufeffrecord_id,size\nA,big\n", encoding="utf-8")
        assert load_records(path)[0].record_id == "A"

    def test_json_objects_load(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text(
            json.dumps(
                [
                    {"record_id": "A", "category": "toys", "attributes": {"size": "big"}},
                ]
            ),
            encoding="utf-8",
        )
        record = load_records(path)[0]
        assert record.record_id == "A"
        assert record.attributes == {"size": "big"}

    def test_json_flat_fields_become_attributes(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps([{"record_id": "A", "size": "big"}]), encoding="utf-8")
        assert load_records(path)[0].attributes == {"size": "big"}

    def test_json_types_are_preserved(self, tmp_path: Path) -> None:
        """Unlike CSV, JSON carries types, and a rule comparing numbers should get numbers."""
        path = tmp_path / "r.json"
        path.write_text(json.dumps([{"record_id": "A", "n": 36, "flag": False}]), encoding="utf-8")
        attributes = load_records(path)[0].attributes
        assert attributes["n"] == 36
        assert attributes["flag"] is False

    def test_a_records_wrapper_key_is_unwrapped(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps({"records": [{"record_id": "A"}]}), encoding="utf-8")
        assert len(load_records(path)) == 1

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(RecordFileError, match="does not exist"):
            load_records(tmp_path / "nope.csv")

    def test_an_unknown_extension_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text("a: b", encoding="utf-8")
        with pytest.raises(RecordFileError, match="expected a .csv or .json"):
            load_records(path)

    def test_invalid_json_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text("[{\n  broken", encoding="utf-8")
        with pytest.raises(RecordFileError, match="line"):
            load_records(path)

    def test_an_empty_csv_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "r.csv"
        path.write_text("record_id,size\n", encoding="utf-8")
        with pytest.raises(RecordFileError, match="no data rows"):
            load_records(path)

    def test_an_empty_json_list_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(RecordFileError, match="no records"):
            load_records(path)

    def test_a_non_object_json_item_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
        with pytest.raises(RecordFileError, match="item 1 is not an object"):
            load_records(path)


class TestSummarise:
    def test_it_counts_every_outcome(self, registry: Registry, sample_records: Any) -> None:
        results = check_all(list(sample_records), registry, "US", date(2025, 6, 1))
        summary = summarise(results)
        assert summary["total"] == 8
        assert set(summary) >= {o.value for o in Outcome}

    def test_needs_attention_counts_everything_that_is_not_a_clean_pass(
        self, registry: Registry, sample_records: Any
    ) -> None:
        """NO_RULES and INDETERMINATE are not successes, and the summary says so."""
        results = check_all(list(sample_records), registry, "EU", date(2024, 6, 1))
        summary = summarise(results)
        assert summary["no_rules"] == 8
        assert summary["compliant"] == 0
        assert summary["needs_attention"] == 8


class TestWriteResults:
    @pytest.fixture
    def results(self, registry: Registry, sample_records: Any) -> Any:
        return check_all(list(sample_records), registry, "US", date(2025, 6, 1))

    def test_csv_and_json_are_both_written(self, results: Any, tmp_path: Path) -> None:
        written = write_results(results, tmp_path, "csv")
        assert {p.name for p in written} == {"report.json", "results.csv", "findings.csv"}

    def test_the_directory_is_created(self, results: Any, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested"
        write_results(results, target, "csv")
        assert (target / "report.json").exists()

    def test_the_record_csv_has_one_row_per_record(self, results: Any, tmp_path: Path) -> None:
        write_results(results, tmp_path, "csv")
        with (tmp_path / "results.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == len(results)
        assert list(rows[0]) == list(RECORD_FIELDS)

    def test_the_findings_csv_has_one_row_per_finding(self, results: Any, tmp_path: Path) -> None:
        write_results(results, tmp_path, "csv")
        with (tmp_path / "findings.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == sum(len(r.findings) for r in results)
        assert list(rows[0]) == list(FINDING_FIELDS)

    def test_the_report_carries_the_digest(self, results: Any, tmp_path: Path) -> None:
        write_results(results, tmp_path, "csv")
        report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert all(row["digest"] for row in report["records"])

    def test_json_format_writes_only_the_report(self, results: Any, tmp_path: Path) -> None:
        written = write_results(results, tmp_path, "json")
        assert [p.name for p in written] == ["report.json"]

    @requires_openpyxl
    def test_excel_format_writes_a_workbook(self, results: Any, tmp_path: Path) -> None:
        written = write_results(results, tmp_path, "excel")
        assert {p.name for p in written} == {"report.json", "results.xlsx"}

    @requires_openpyxl
    def test_the_excel_workbook_has_both_sheets(self, results: Any, tmp_path: Path) -> None:
        from openpyxl import load_workbook

        write_results(results, tmp_path, "excel")
        book = load_workbook(tmp_path / "results.xlsx")
        assert book.sheetnames == ["results", "findings"]
        assert book["results"].max_row == len(results) + 1


class TestEmitNarrowsOnlyTheTabularOutput:
    """The audit trail must not shrink because somebody passed a filter flag.

    The complete set is the required positional argument and always reaches the JSON
    report; `emit` only narrows the CSV. So the worst a forgetful caller can do is fail to
    filter, which is visible, rather than drop rows, which is not.
    """

    @pytest.fixture
    def results(self, registry: Registry, sample_records: Any) -> Any:
        return check_all(list(sample_records), registry, "US", date(2025, 6, 1))

    def test_the_report_keeps_every_record(self, results: Any, tmp_path: Path) -> None:
        attention = [r for r in results if r.outcome is not Outcome.COMPLIANT]
        assert 0 < len(attention) < len(results)

        write_results(results, tmp_path, "csv", emit=attention)
        report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert len(report["records"]) == len(results)
        assert report["summary"]["total"] == len(results)

    def test_the_csv_is_narrowed(self, results: Any, tmp_path: Path) -> None:
        attention = [r for r in results if r.outcome is not Outcome.COMPLIANT]
        write_results(results, tmp_path, "csv", emit=attention)
        with (tmp_path / "results.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == len(attention)

    def test_the_findings_csv_is_narrowed_too(self, results: Any, tmp_path: Path) -> None:
        attention = [r for r in results if r.outcome is not Outcome.COMPLIANT]
        write_results(results, tmp_path, "csv", emit=attention)
        with (tmp_path / "findings.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == sum(len(r.findings) for r in attention)

    def test_omitting_emit_writes_everything(self, results: Any, tmp_path: Path) -> None:
        write_results(results, tmp_path, "csv")
        with (tmp_path / "results.csv").open(encoding="utf-8") as handle:
            assert len(list(csv.DictReader(handle))) == len(results)

    def test_an_empty_emit_still_leaves_the_report_whole(
        self, results: Any, tmp_path: Path
    ) -> None:
        write_results(results, tmp_path, "csv", emit=[])
        report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert len(report["records"]) == len(results)
        with (tmp_path / "results.csv").open(encoding="utf-8") as handle:
            assert list(csv.DictReader(handle)) == []


class TestFieldConsistency:
    def test_the_record_row_supplies_exactly_the_declared_fields(
        self, registry: Registry, sample_records: Any
    ) -> None:
        from compliance_check.io import _record_row

        row = _record_row(check_all(list(sample_records), registry, "US", date(2025, 6, 1))[0])
        assert set(row) == set(RECORD_FIELDS)

    def test_the_finding_row_supplies_exactly_the_declared_fields(
        self, registry: Registry, sample_records: Any
    ) -> None:
        from compliance_check.io import _finding_rows

        rows = _finding_rows(check_all(list(sample_records), registry, "US", date(2025, 6, 1))[0])
        assert set(rows[0]) == set(FINDING_FIELDS)

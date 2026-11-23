"""The CLI. Exercised through Click's runner, so exit codes are covered too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from compliance_check.cli import main
from tests.conftest import FRAMEWORK_DIR, SAMPLE_RECORDS, requires_openpyxl


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every default away from the repo, so a test cannot write into data/ or audit/."""
    monkeypatch.setenv("CC_RULES_DIR", str(FRAMEWORK_DIR))
    monkeypatch.setenv("CC_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("CC_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("CC_FRAMEWORK", raising=False)


class TestFrameworks:
    def test_it_lists_every_framework(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["frameworks"])
        assert result.exit_code == 0
        for name in ("US", "EU", "JP"):
            assert name in result.output

    def test_it_shows_the_version_timeline(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["frameworks"])
        assert "v2023.1" in result.output
        assert "v2024.1" in result.output
        assert "v2026.1" in result.output

    def test_it_reports_the_declared_gap(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["frameworks"])
        assert "gap" in result.output
        assert "no_rules" in result.output

    def test_it_marks_what_is_in_force_today(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["frameworks"])
        assert "in force today" in result.output

    def test_it_shows_a_digest_per_version(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["frameworks"])
        assert "1d47766a004acef2" in result.output

    def test_a_bad_rules_directory_is_a_clean_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(main, ["frameworks", "-r", str(tmp_path / "nope")])
        assert result.exit_code != 0
        assert "not a directory" in result.output


class TestOne:
    def test_a_compliant_record_exits_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            [
                "one",
                "T-1",
                "--category",
                "toys",
                "--as-of",
                "2025-06-01",
                "-f",
                "US",
                "-a",
                "country_of_origin=Vietnam",
                "-a",
                "certificate_number=US-123456",
                "-a",
                "certificate_expiry=2027-03-01",
                "-a",
                "small_parts=false",
                "-a",
                "instruction_manual=included",
            ],
        )
        assert result.exit_code == 0
        assert "compliant" in result.output

    def test_a_non_compliant_record_exits_one(self, runner: CliRunner) -> None:
        result = runner.invoke(
            main,
            [
                "one",
                "T-2",
                "--category",
                "toys",
                "--as-of",
                "2025-06-01",
                "-f",
                "US",
                "-a",
                "certificate_number=US-123456",
            ],
        )
        assert result.exit_code == 1
        assert "non_compliant" in result.output

    def test_the_resolved_version_and_digest_are_shown(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["one", "T-1", "--as-of", "2025-06-01", "-f", "US"])
        assert "v2024.1" in result.output

    def test_the_same_record_gets_different_verdicts_on_different_dates(
        self, runner: CliRunner
    ) -> None:
        """The whole point of the tool, exercised end to end."""
        attributes = [
            "-a",
            "country_of_origin=Vietnam",
            "-a",
            "certificate_number=US-234567",
            "-a",
            "certificate_expiry=2027-03-01",
            "-a",
            "small_parts=true",
            "-a",
            "age_min_months=36",
            "-a",
            "instruction_manual=included",
        ]
        earlier = runner.invoke(
            main,
            ["one", "T", "--category", "toys", "-f", "US", "--as-of", "2025-06-01", *attributes],
        )
        later = runner.invoke(
            main,
            ["one", "T", "--category", "toys", "-f", "US", "--as-of", "2026-06-01", *attributes],
        )
        assert earlier.exit_code == 0
        assert later.exit_code == 1
        assert "v2024.1" in earlier.output
        assert "v2026.1" in later.output

    def test_a_gap_date_reports_no_rules_and_exits_one(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["one", "T", "-f", "EU", "--as-of", "2024-06-01"])
        assert result.exit_code == 1
        assert "no_rules" in result.output
        assert "no version of EU was in force" in result.output

    def test_a_malformed_attribute_is_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["one", "T", "-a", "no-equals-sign"])
        assert result.exit_code != 0
        assert "field=value" in result.output

    def test_a_bad_as_of_date_is_rejected(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["one", "T", "--as-of", "01/06/2025"])
        assert result.exit_code != 0
        assert "not an ISO date" in result.output

    def test_an_unknown_framework_lists_the_real_ones(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["one", "T", "-f", "XX"])
        assert result.exit_code != 0
        assert "Available: EU, JP, US" in result.output

    def test_it_does_not_write_to_the_audit_log(self, runner: CliRunner, tmp_path: Path) -> None:
        """Exploring a rule must not pollute the record of real checks."""
        runner.invoke(main, ["one", "T", "-f", "US", "--as-of", "2025-06-01"])
        assert not (tmp_path / "audit.jsonl").exists()


class TestRun:
    def test_it_checks_the_shipped_sample(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        assert result.exit_code == 1  # 6 of 8 need attention
        assert "total" in result.output
        assert (tmp_path / "out" / "report.json").exists()

    def test_the_summary_counts_match_the_report(self, runner: CliRunner, tmp_path: Path) -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
        assert report["summary"]["total"] == 8
        assert report["summary"]["compliant"] == 2
        assert report["summary"]["non_compliant"] == 4
        assert report["summary"]["indeterminate"] == 2
        assert report["summary"]["needs_attention"] == 6

    def test_details_are_shown_by_default(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
            ],
        )
        assert "US-LABEL-01" in result.output

    def test_it_writes_the_audit_log_by_default(self, runner: CliRunner, tmp_path: Path) -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        log = tmp_path / "audit.jsonl"
        assert log.exists()
        assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 8

    def test_no_audit_suppresses_it(self, runner: CliRunner, tmp_path: Path) -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
                "--no-audit",
            ],
        )
        assert not (tmp_path / "audit.jsonl").exists()

    def test_a_fully_compliant_batch_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        good = tmp_path / "good.csv"
        good.write_text(
            "record_id,category,country_of_origin,certificate_number,certificate_expiry,"
            "small_parts,instruction_manual\n"
            "G-1,furniture,Poland,US-111111,2027-01-01,false,included\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            main,
            [
                "run",
                "-i",
                str(good),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        assert result.exit_code == 0
        assert "compliant" in result.output

    def test_attention_only_narrows_the_csv_but_not_the_report(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
                "--attention-only",
            ],
        )
        report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
        assert len(report["records"]) == 8

        rows = (tmp_path / "out" / "results.csv").read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 7  # header + 6 needing attention

    @requires_openpyxl
    def test_excel_output_works(self, runner: CliRunner, tmp_path: Path) -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
                "--format",
                "excel",
            ],
        )
        assert (tmp_path / "out" / "results.xlsx").exists()

    def test_a_missing_input_is_a_usage_error(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["run", "-f", "US"])
        assert result.exit_code != 0
        assert "--input" in result.output

    def test_a_missing_input_file_is_a_clean_error(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(main, ["run", "-i", str(tmp_path / "nope.csv")])
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_a_gap_date_is_flagged_in_the_header(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "EU",
                "--as-of",
                "2024-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        assert "none in force" in result.output
        assert result.exit_code == 1


class TestReplay:
    def _seed(self, runner: CliRunner, tmp_path: Path, as_of: str = "2025-06-01") -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                as_of,
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )

    def test_an_untouched_rule_set_replays_clean_and_exits_zero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        self._seed(runner, tmp_path)
        result = runner.invoke(main, ["replay"])
        assert result.exit_code == 0
        assert "reproduced" in result.output
        assert "diverged" not in result.output

    def test_checks_made_during_a_coverage_gap_also_replay_clean(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The regression: these used to come back as no_longer_covered."""
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "EU",
                "--as-of",
                "2024-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        result = runner.invoke(main, ["replay"])
        assert result.exit_code == 0
        assert "no_longer_covered" not in result.output
        assert "reproduced" in result.output

    def test_quiet_hides_the_reproduced_entries(self, runner: CliRunner, tmp_path: Path) -> None:
        self._seed(runner, tmp_path)
        result = runner.invoke(main, ["replay", "--quiet"])
        assert result.exit_code == 0
        assert "R-1  " not in result.output
        assert "reproduced" in result.output  # still in the count summary

    def test_an_absent_log_says_so(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["replay"])
        assert "nothing to replay" in result.output
        assert result.exit_code == 0

    def test_an_edited_rule_set_reports_rules_changed_without_failing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A regulation update is expected, so it must not fail a build."""
        import shutil

        self._seed(runner, tmp_path)
        edited_dir = tmp_path / "edited"
        shutil.copytree(FRAMEWORK_DIR, edited_dir)
        us = edited_dir / "us.yaml"
        us.write_text(
            us.read_text(encoding="utf-8").replace(
                "- { op: min, field: age_min_months, value: 36 }",
                "- { op: min, field: age_min_months, value: 30 }",
            ),
            encoding="utf-8",
        )
        result = runner.invoke(main, ["replay", "-r", str(edited_dir)])
        assert result.exit_code == 0
        assert "rules_changed" in result.output
        # The phrase must survive the CLI's 80-char truncation, which previously cut the
        # digest in half and dropped the explanation entirely.
        assert "edited in place" in result.output


class TestDoctor:
    def test_it_reports_configuration(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "rules_dir" in result.output
        assert "today (UTC)" in result.output

    def test_it_lists_the_operators(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        for op in ("present", "one_of", "all_of", "matches"):
            assert op in result.output

    def test_it_explains_every_outcome(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        for outcome in ("compliant", "non_compliant", "indeterminate", "no_rules"):
            assert outcome in result.output

    def test_it_says_that_no_rules_is_not_compliance(self, runner: CliRunner) -> None:
        """The single most important thing for a reader to understand."""
        result = runner.invoke(main, ["doctor"])
        assert "no_rules is NOT compliance" in result.output

    def test_it_reports_which_version_is_in_force(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["doctor"])
        assert "in force" in result.output

    def test_it_survives_an_unloadable_rules_directory(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(main, ["doctor", "-r", str(tmp_path / "nope")])
        assert result.exit_code == 0
        assert "not loadable" in result.output

    def test_it_summarises_the_audit_log_when_one_exists(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        runner.invoke(
            main,
            [
                "run",
                "-i",
                str(SAMPLE_RECORDS),
                "-f",
                "US",
                "--as-of",
                "2025-06-01",
                "-o",
                str(tmp_path / "out"),
                "--no-details",
            ],
        )
        result = runner.invoke(main, ["doctor"])
        assert "8 entr" in result.output


class TestTopLevel:
    def test_help_lists_every_command(self, runner: CliRunner) -> None:
        result = runner.invoke(main, ["--help"])
        for command in ("run", "one", "frameworks", "replay", "doctor"):
            assert command in result.output

    def test_version_is_reported(self, runner: CliRunner) -> None:
        from compliance_check import __version__

        result = runner.invoke(main, ["--version"])
        assert __version__ in result.output

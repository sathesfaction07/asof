"""The audit log and replay.

The class that matters most here is TestReplayMustNotCryWolf. It pins the bug this module
actually had.
"""

from __future__ import annotations

import copy
import itertools
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from compliance_check import __version__
from compliance_check.core import audit
from compliance_check.core.evaluate import check
from compliance_check.models import Outcome, Status
from compliance_check.rules.registry import Registry
from tests.conftest import FRAMEWORKS, record, registry_from

DATES = tuple(date(2023, 1, 1) + timedelta(days=n) for n in range(0, 1200, 60))


def fill_log(path: Path, registry: Registry, records: list[Any]) -> None:
    for framework, when in itertools.product(FRAMEWORKS, DATES):
        for item in records:
            audit.append(path, check(item, registry, framework, when), item)


class TestAppendAndRead:
    def test_it_creates_the_parent_directory(self, audit_log: Path, registry: Registry) -> None:
        assert not audit_log.parent.exists()
        audit.append(audit_log, check(record(), registry, "US", date(2025, 6, 1)), record())
        assert audit_log.exists()

    def test_entries_accumulate_rather_than_overwrite(
        self, audit_log: Path, registry: Registry
    ) -> None:
        for index in range(3):
            item = record(f"R-{index}")
            audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)
        entries, problems = audit.read(audit_log)
        assert len(entries) == 3
        assert not problems
        assert [e.record.record_id for e in entries] == ["R-0", "R-1", "R-2"]

    def test_one_entry_per_line(self, audit_log: Path, registry: Registry) -> None:
        for index in range(3):
            item = record(f"R-{index}")
            audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)
        assert len(audit_log.read_text(encoding="utf-8").strip().splitlines()) == 3

    def test_the_entry_records_the_tool_version(self, audit_log: Path, registry: Registry) -> None:
        entry = audit.append(audit_log, check(record(), registry, "US", date(2025, 6, 1)), record())
        assert entry.tool_version == __version__

    def test_the_entry_records_the_inputs(self, audit_log: Path, registry: Registry) -> None:
        """Without the inputs the log can be read but not verified."""
        item = record("R-9", "toys", country_of_origin="Vietnam")
        audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)
        entries, _ = audit.read(audit_log)
        assert entries[0].record.attributes == {"country_of_origin": "Vietnam"}
        assert entries[0].record.category == "toys"

    def test_the_entry_records_the_digest(self, audit_log: Path, registry: Registry) -> None:
        audit.append(audit_log, check(record(), registry, "US", date(2025, 6, 1)), record())
        entries, _ = audit.read(audit_log)
        assert entries[0].result.digest
        assert entries[0].result.version == "2024.1"

    def test_a_missing_file_is_reported_not_raised(self, tmp_path: Path) -> None:
        entries, problems = audit.read(tmp_path / "nope.jsonl")
        assert entries == []
        assert "does not exist" in problems[0]

    def test_a_corrupt_line_does_not_lose_the_others(
        self, audit_log: Path, registry: Registry
    ) -> None:
        """One bad line must not make ten thousand good ones unreadable."""
        audit.append(audit_log, check(record("A"), registry, "US", date(2025, 6, 1)), record("A"))
        with audit_log.open("a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
        audit.append(audit_log, check(record("B"), registry, "US", date(2025, 6, 1)), record("B"))

        entries, problems = audit.read(audit_log)
        assert [e.record.record_id for e in entries] == ["A", "B"]
        assert len(problems) == 1
        assert "line 2" in problems[0]

    def test_a_corrupt_line_is_reported_rather_than_skipped_silently(
        self, audit_log: Path, registry: Registry
    ) -> None:
        audit.append(audit_log, check(record(), registry, "US", date(2025, 6, 1)), record())
        with audit_log.open("a", encoding="utf-8") as handle:
            handle.write('{"checked_at": "2025-01-01T00:00:00Z"}\n')
        _, problems = audit.read(audit_log)
        assert problems and "audit schema" in problems[0]

    def test_blank_lines_are_ignored(self, audit_log: Path, registry: Registry) -> None:
        audit.append(audit_log, check(record(), registry, "US", date(2025, 6, 1)), record())
        with audit_log.open("a", encoding="utf-8") as handle:
            handle.write("\n\n")
        entries, problems = audit.read(audit_log)
        assert len(entries) == 1
        assert not problems


class TestReplayMustNotCryWolf:
    """The regression this module actually had.

    The first implementation checked "is a version in force now?" and reported
    NO_LONGER_COVERED whenever the answer was no -- without asking whether the recorded
    check had also found nothing in force. Every check made during a coverage gap therefore
    came back as a change on a completely untouched rule set: 112 false alarms out of 480
    entries, 23%.

    That is fatal for the intended use. `replay` is a CI gate, and a gate that fires on
    a quarter of its input on a good day is a gate people switch off.
    """

    def test_an_untouched_rule_set_reproduces_every_entry(
        self, audit_log: Path, registry: Registry, sample_records: Any
    ) -> None:
        fill_log(audit_log, registry, list(sample_records))
        replays, problems = audit.replay_all(audit_log, registry)

        assert not problems
        assert len(replays) == len(FRAMEWORKS) * len(DATES) * len(sample_records)
        offenders = [r for r in replays if r.status is not audit.ReplayStatus.REPRODUCED]
        assert offenders == [], (
            f"{len(offenders)} of {len(replays)} entries reported a change against "
            f"identical rules; first was {offenders[0].detail if offenders else ''}"
        )

    def test_specifically_a_check_made_during_a_coverage_gap_reproduces(
        self, audit_log: Path, registry: Registry
    ) -> None:
        """The exact shape of the bug: EU has no version in force through 2024."""
        item = record("R-1", "toys")
        result = check(item, registry, "EU", date(2024, 6, 1))
        assert result.outcome is Outcome.NO_RULES
        assert result.version is None

        audit.append(audit_log, result, item)
        entries, _ = audit.read(audit_log)
        replay = audit.replay_entry(entries[0], registry)

        assert replay.status is audit.ReplayStatus.REPRODUCED
        assert not replay.is_defect

    def test_a_check_made_before_a_framework_existed_also_reproduces(
        self, audit_log: Path, registry: Registry
    ) -> None:
        item = record("R-1")
        result = check(item, registry, "JP", date(2023, 1, 1))
        audit.append(audit_log, result, item)
        entries, _ = audit.read(audit_log)
        assert audit.replay_entry(entries[0], registry).status is audit.ReplayStatus.REPRODUCED

    def test_the_no_coverage_case_still_compares_outcomes(
        self, audit_log: Path, registry: Registry
    ) -> None:
        """Fixing the false alarm must not turn the branch into an unconditional pass.

        The first implementation returned before comparing outcomes at all, so a
        no-coverage entry with a wrong recorded verdict was invisible. Now it is caught.
        """
        item = record("R-1")
        audit.append(audit_log, check(item, registry, "EU", date(2024, 6, 1)), item)
        entries, _ = audit.read(audit_log)

        tampered = entries[0].model_copy(deep=True)
        tampered.result.outcome = Outcome.COMPLIANT
        replay = audit.replay_entry(tampered, registry)

        assert replay.status is audit.ReplayStatus.DIVERGED
        assert replay.is_defect
        assert "no version in force, then or now" in replay.detail


class TestReplayDetectsRuleChanges:
    def test_editing_a_version_in_place_is_reported(
        self, audit_log: Path, registry: Registry, mutable_docs: dict[str, Any]
    ) -> None:
        item = record(
            "R-1",
            "toys",
            country_of_origin="X",
            certificate_number="US-123456",
            certificate_expiry="2027-01-01",
            small_parts="false",
            instruction_manual="y",
        )
        audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)

        edited = copy.deepcopy(mutable_docs)
        target = next(v for v in edited["us"]["versions"] if v["version"] == "2024.1")
        toy = next(r for r in target["requirements"] if r["id"] == "US-TOY-AGE-01")
        toy["assert"]["of"][1]["value"] = 999

        entries, _ = audit.read(audit_log)
        replay = audit.replay_entry(entries[0], registry_from(edited))
        assert replay.status is audit.ReplayStatus.RULES_CHANGED
        assert "edited in place" in replay.detail
        assert not replay.is_defect

    def test_the_message_shows_both_digests(
        self, audit_log: Path, registry: Registry, mutable_docs: dict[str, Any]
    ) -> None:
        item = record("R-1", "toys")
        result = check(item, registry, "US", date(2025, 6, 1))
        audit.append(audit_log, result, item)

        edited = copy.deepcopy(mutable_docs)
        target = next(v for v in edited["us"]["versions"] if v["version"] == "2024.1")
        target["requirements"][0]["severity"] = "minor"

        entries, _ = audit.read(audit_log)
        replay = audit.replay_entry(entries[0], registry_from(edited))
        assert result.digest is not None and result.digest in replay.detail

    def test_a_different_version_now_covering_the_date_is_reported(
        self, audit_log: Path, registry: Registry, mutable_docs: dict[str, Any]
    ) -> None:
        item = record("R-1", "toys")
        audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)

        shifted = copy.deepcopy(mutable_docs)
        for version in shifted["us"]["versions"]:
            if version["version"] == "2023.1":
                version["effective_until"] = date(2026, 1, 1)
            if version["version"] == "2024.1":
                version["effective_from"] = date(2026, 1, 1)
                version["effective_until"] = date(2027, 1, 1)
            if version["version"] == "2026.1":
                version["effective_from"] = date(2027, 1, 1)

        entries, _ = audit.read(audit_log)
        replay = audit.replay_entry(entries[0], registry_from(shifted))
        assert replay.status is audit.ReplayStatus.RULES_CHANGED
        assert "is now covered by v2023.1" in replay.detail

    def test_a_withdrawn_version_is_no_longer_covered(
        self, audit_log: Path, registry: Registry, mutable_docs: dict[str, Any]
    ) -> None:
        item = record("R-1", "toys")
        audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)

        pruned = copy.deepcopy(mutable_docs)
        pruned["us"]["versions"] = [v for v in pruned["us"]["versions"] if v["version"] != "2024.1"]

        entries, _ = audit.read(audit_log)
        replay = audit.replay_entry(entries[0], registry_from(pruned))
        assert replay.status is audit.ReplayStatus.NO_LONGER_COVERED
        assert "the check used v2024.1" in replay.detail
        assert not replay.is_defect

    def test_newly_covering_a_previously_uncovered_date_is_reported(
        self, audit_log: Path, registry: Registry, mutable_docs: dict[str, Any]
    ) -> None:
        """The mirror case. The first implementation rendered this as "not vNone"."""
        item = record("R-1", "toys")
        result = check(item, registry, "EU", date(2024, 6, 1))
        assert result.version is None
        audit.append(audit_log, result, item)

        filled = copy.deepcopy(mutable_docs)
        for version in filled["eu"]["versions"]:
            if version["version"] == "2022.1":
                version["effective_until"] = date(2025, 1, 1)

        entries, _ = audit.read(audit_log)
        replay = audit.replay_entry(entries[0], registry_from(filled))
        assert replay.status is audit.ReplayStatus.RULES_CHANGED
        assert "nothing was in force" in replay.detail
        assert "vNone" not in replay.detail


class TestReplayDetectsToolChanges:
    """DIVERGED is a self-check on this codebase, not on the rules.

    No rule edit can trigger it, which makes it look like dead code. It is not: it fires
    when the same rules produce a different answer, which is exactly what a behaviour
    change in the evaluator looks like. The tests below reach it the only way that is
    possible without actually changing the code.
    """

    @pytest.fixture
    def entry(self, audit_log: Path, registry: Registry) -> Any:
        item = record(
            "R-1",
            "toys",
            country_of_origin="Vietnam",
            certificate_number="US-123456",
            certificate_expiry="2027-03-01",
            small_parts="false",
            age_min_months="36",
            instruction_manual="included",
        )
        audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)
        return audit.read(audit_log)[0][0]

    def test_a_changed_verdict_under_identical_rules_is_a_defect(
        self, entry: Any, registry: Registry
    ) -> None:
        assert audit.replay_entry(entry, registry).status is audit.ReplayStatus.REPRODUCED

        tampered = entry.model_copy(deep=True)
        tampered.result.outcome = Outcome.NON_COMPLIANT
        replay = audit.replay_entry(tampered, registry)

        assert replay.status is audit.ReplayStatus.DIVERGED
        assert replay.is_defect
        assert "identical rules" in replay.detail

    def test_the_message_names_both_tool_versions(self, entry: Any, registry: Registry) -> None:
        tampered = entry.model_copy(deep=True)
        tampered.result.outcome = Outcome.NON_COMPLIANT
        tampered.tool_version = "0.0.1"
        replay = audit.replay_entry(tampered, registry)
        assert "0.0.1" in replay.detail
        assert __version__ in replay.detail

    def test_a_changed_finding_under_an_identical_verdict_is_also_a_defect(
        self, entry: Any, registry: Registry
    ) -> None:
        tampered = entry.model_copy(deep=True)
        tampered.result.findings[0].status = Status.ERROR
        replay = audit.replay_entry(tampered, registry)
        assert replay.status is audit.ReplayStatus.DIVERGED
        assert "finding(s) differ" in replay.detail

    def test_findings_are_compared_by_id_not_position(self, entry: Any, registry: Registry) -> None:
        """A reordered rule file must not read as a divergence."""
        tampered = entry.model_copy(deep=True)
        tampered.result.findings.reverse()
        assert audit.replay_entry(tampered, registry).status is audit.ReplayStatus.REPRODUCED

    def test_a_removed_requirement_is_named(self, entry: Any, registry: Registry) -> None:
        tampered = entry.model_copy(deep=True)
        dropped = tampered.result.findings.pop(0)
        replay = audit.replay_entry(tampered, registry)
        assert replay.status is audit.ReplayStatus.DIVERGED
        assert dropped.requirement_id in replay.detail
        assert "absent ->" in replay.detail


class TestReplayAll:
    def test_it_reports_problems_alongside_replays(
        self, audit_log: Path, registry: Registry
    ) -> None:
        item = record("A")
        audit.append(audit_log, check(item, registry, "US", date(2025, 6, 1)), item)
        with audit_log.open("a", encoding="utf-8") as handle:
            handle.write("garbage\n")
        replays, problems = audit.replay_all(audit_log, registry)
        assert len(replays) == 1
        assert len(problems) == 1

    def test_an_absent_log_yields_nothing_to_replay(
        self, tmp_path: Path, registry: Registry
    ) -> None:
        replays, problems = audit.replay_all(tmp_path / "none.jsonl", registry)
        assert replays == []
        assert problems

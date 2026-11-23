"""Applicability scoping and how findings become one verdict."""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import pytest

from compliance_check.core.evaluate import (
    applicability,
    check,
    check_all,
    derive_outcome,
    evaluate_version,
)
from compliance_check.models import Finding, Outcome, Severity, Status
from compliance_check.rules.registry import Registry
from compliance_check.rules.schema import RuleSet
from tests.conftest import FRAMEWORKS, SWEEP_DATES, record, registry_from, requirement, rule_doc


def one_version(requirements: list[dict[str, Any]]) -> Any:
    doc = rule_doc(
        versions=[
            {
                "version": "1.0",
                "effective_from": date(2024, 1, 1),
                "requirements": requirements,
            }
        ]
    )
    return RuleSet.model_validate(doc).versions[0]


def finding(
    status: Status, severity: Severity = Severity.MAJOR, identifier: str = "R-1"
) -> Finding:
    return Finding(
        requirement_id=identifier, title="t", severity=severity, status=status, detail=""
    )


class TestNoVacuousCompliance:
    """`all([])` is True, so "no failures" is not the same as "passed".

    A record that was never examined must not be reported as compliant. This is the most
    dangerous bug this tool could have, so it is asserted as a property over the whole
    shipped rule set rather than only in a unit test.
    """

    def test_a_version_with_no_requirements_is_no_rules_not_compliant(self) -> None:
        version = one_version([])
        result = evaluate_version(record(), version, "TEST", date(2025, 1, 1))
        assert result.outcome is Outcome.NO_RULES
        assert "no requirements" in result.note

    def test_a_record_scoped_out_of_every_requirement_is_no_rules(self) -> None:
        version = one_version(
            [
                requirement("R-1", applies_to={"category": ["toys"]}),
                requirement("R-2", applies_to={"category": ["apparel"]}),
            ]
        )
        result = evaluate_version(record(category="furniture"), version, "TEST", date(2025, 1, 1))
        assert result.outcome is Outcome.NO_RULES
        assert "scoped out" in result.note
        assert all(f.status is Status.SKIP for f in result.findings)

    def test_no_version_in_force_is_no_rules(self, registry: Registry) -> None:
        result = check(record(), registry, "EU", date(2024, 6, 1))
        assert result.outcome is Outcome.NO_RULES
        assert result.version is None
        assert result.digest is None

    def test_the_note_says_what_was_known(self, registry: Registry) -> None:
        result = check(record(), registry, "EU", date(2024, 6, 1))
        assert "no version of EU was in force" in result.note
        assert "2022.1" in result.note and "2025.1" in result.note

    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_nothing_is_ever_compliant_with_zero_judged_requirements(
        self, registry: Registry, sample_records: Any, framework: str
    ) -> None:
        for when in SWEEP_DATES:
            for item in sample_records:
                result = check(item, registry, framework, when)
                judged = [f for f in result.findings if f.status is not Status.SKIP]
                if result.outcome is Outcome.COMPLIANT:
                    assert judged, f"{framework} {when} {item.record_id} passed vacuously"

    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_no_rules_is_never_reported_when_something_was_judged(
        self, registry: Registry, sample_records: Any, framework: str
    ) -> None:
        for when in SWEEP_DATES:
            for item in sample_records:
                result = check(item, registry, framework, when)
                judged = [f for f in result.findings if f.status is not Status.SKIP]
                if result.outcome is Outcome.NO_RULES:
                    assert not judged, f"{framework} {when} {item.record_id}"


class TestApplicability:
    def test_no_scope_means_it_applies(self) -> None:
        req = (
            RuleSet.model_validate(
                rule_doc(
                    versions=[
                        {
                            "version": "1.0",
                            "effective_from": date(2024, 1, 1),
                            "requirements": [requirement()],
                        }
                    ]
                )
            )
            .versions[0]
            .requirements[0]
        )
        assert applicability(req, record()) == (None, "")

    def test_a_matching_scope_means_it_applies(self) -> None:
        version = one_version([requirement(applies_to={"category": ["toys"]})])
        assert applicability(version.requirements[0], record(category="toys"))[0] is None

    def test_scope_matching_is_case_insensitive(self) -> None:
        """A record saying `Toys` and a rule saying `toys` mean the same category."""
        version = one_version([requirement(applies_to={"category": ["toys"]})])
        assert applicability(version.requirements[0], record(category="Toys"))[0] is None

    def test_a_non_matching_scope_skips(self) -> None:
        version = one_version([requirement(applies_to={"category": ["toys"]})])
        status, detail = applicability(version.requirements[0], record(category="apparel"))
        assert status is Status.SKIP
        assert "does not apply" in detail

    def test_an_undeterminable_scope_errors_rather_than_skipping(self) -> None:
        """Skipping would read as "not regulated", which is a pass we cannot justify.

        A requirement scoped to toys, against a record with no category, might apply. The
        honest answer is that we do not know, which routes the record to a human.
        """
        version = one_version([requirement(applies_to={"category": ["toys"]})])
        status, detail = applicability(version.requirements[0], record())
        assert status is Status.ERROR
        assert "cannot tell whether this applies" in detail

    def test_a_blank_scope_field_is_also_undeterminable(self) -> None:
        version = one_version([requirement(applies_to={"category": ["toys"]})])
        assert applicability(version.requirements[0], record(category="  "))[0] is Status.ERROR

    def test_multiple_scope_fields_must_all_match(self) -> None:
        version = one_version(
            [
                requirement(applies_to={"category": ["toys"], "region": ["us"]}),
            ]
        )
        req = version.requirements[0]
        assert applicability(req, record(category="toys", region="us"))[0] is None
        assert applicability(req, record(category="toys", region="eu"))[0] is Status.SKIP

    def test_scoping_on_a_non_category_field_works(self) -> None:
        version = one_version([requirement(applies_to={"battery_type": ["lithium-metal"]})])
        req = version.requirements[0]
        assert applicability(req, record(battery_type="lithium-metal"))[0] is None
        assert applicability(req, record(battery_type="alkaline"))[0] is Status.SKIP

    def test_a_numeric_scope_matches(self) -> None:
        version = one_version([requirement(applies_to={"tier": [1, 2]})])
        req = version.requirements[0]
        assert applicability(req, record(tier=1))[0] is None
        assert applicability(req, record(tier=3))[0] is Status.SKIP


class TestDeriveOutcome:
    """The precedence between the four outcomes."""

    def test_everything_passing_is_compliant(self) -> None:
        assert derive_outcome([finding(Status.PASS)])[0] is Outcome.COMPLIANT

    def test_a_failure_is_non_compliant(self) -> None:
        assert (
            derive_outcome([finding(Status.PASS), finding(Status.FAIL)])[0] is Outcome.NON_COMPLIANT
        )

    def test_a_known_failure_outranks_an_unknown(self) -> None:
        """No point saying "indeterminate" about a record already proven bad."""
        findings = [finding(Status.FAIL), finding(Status.ERROR)]
        assert derive_outcome(findings)[0] is Outcome.NON_COMPLIANT

    def test_an_unevaluable_requirement_is_indeterminate(self) -> None:
        outcome, note = derive_outcome([finding(Status.PASS), finding(Status.ERROR)])
        assert outcome is Outcome.INDETERMINATE
        assert "could not be evaluated" in note

    def test_nothing_judged_is_no_rules(self) -> None:
        assert derive_outcome([])[0] is Outcome.NO_RULES
        assert derive_outcome([finding(Status.SKIP)])[0] is Outcome.NO_RULES

    def test_an_advisory_failure_does_not_change_the_verdict(self) -> None:
        findings = [finding(Status.PASS), finding(Status.FAIL, Severity.ADVISORY)]
        outcome, note = derive_outcome(findings)
        assert outcome is Outcome.COMPLIANT
        assert "advisory" in note

    def test_an_advisory_failure_alongside_a_real_one_still_fails(self) -> None:
        findings = [finding(Status.FAIL, Severity.ADVISORY), finding(Status.FAIL, Severity.MINOR)]
        assert derive_outcome(findings)[0] is Outcome.NON_COMPLIANT

    @pytest.mark.parametrize("severity", [Severity.CRITICAL, Severity.MAJOR, Severity.MINOR])
    def test_every_non_advisory_severity_counts_against(self, severity: Severity) -> None:
        assert derive_outcome([finding(Status.FAIL, severity)])[0] is Outcome.NON_COMPLIANT


class TestAdvisoryMonotonicity:
    """Adding an always-failing advisory requirement must never flip a verdict.

    Checked as a property, because the alternative is that staging a rule as advisory
    quietly starts failing records -- which would make the advisory severity useless.
    """

    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_an_extra_advisory_requirement_changes_nothing(
        self, mutable_docs: dict[str, Any], registry: Registry, sample_records: Any, framework: str
    ) -> None:
        spiked = copy.deepcopy(mutable_docs)
        for doc in spiked.values():
            for version in doc["versions"]:
                version.setdefault("requirements", []).append(
                    {
                        "id": "SPIKE-ADV",
                        "title": "always fails",
                        "severity": "advisory",
                        "assert": {"op": "present", "field": "no_such_field_anywhere"},
                    }
                )
        spiked_registry = registry_from(spiked)
        for when in SWEEP_DATES[::3]:
            for item in sample_records:
                before = check(item, registry, framework, when)
                after = check(item, spiked_registry, framework, when)
                if before.outcome is Outcome.COMPLIANT:
                    assert after.outcome is Outcome.COMPLIANT, (
                        f"{framework} {when} {item.record_id}"
                    )


class TestWorstSeverity:
    def test_it_reports_the_most_severe_failure(self) -> None:
        version = one_version(
            [
                requirement("R-1", severity="minor", assertion={"op": "present", "field": "x"}),
                requirement("R-2", severity="critical", assertion={"op": "present", "field": "y"}),
            ]
        )
        result = evaluate_version(record(), version, "TEST", date(2025, 1, 1))
        assert result.worst_severity is Severity.CRITICAL

    def test_it_is_none_when_nothing_failed(self) -> None:
        version = one_version([requirement("R-1", assertion={"op": "present", "field": "a"})])
        result = evaluate_version(record(a=1), version, "TEST", date(2025, 1, 1))
        assert result.worst_severity is None

    def test_an_advisory_failure_is_not_the_worst_severity(self) -> None:
        version = one_version(
            [
                requirement("R-1", severity="advisory", assertion={"op": "present", "field": "x"}),
            ]
        )
        result = evaluate_version(record(), version, "TEST", date(2025, 1, 1))
        assert result.worst_severity is None

    def test_severity_ranking_is_not_alphabetical(self) -> None:
        """ "critical" < "major" as strings, which is the wrong order."""
        assert Severity.CRITICAL.rank < Severity.MAJOR.rank < Severity.MINOR.rank
        assert Severity.MINOR.rank < Severity.ADVISORY.rank


class TestSummary:
    def test_it_reports_the_version_and_counts(self) -> None:
        version = one_version(
            [
                requirement("R-1", assertion={"op": "present", "field": "a"}),
                requirement("R-2", assertion={"op": "present", "field": "b"}),
                requirement("R-3", applies_to={"category": ["other"]}),
            ]
        )
        result = evaluate_version(record(a=1, category="toys"), version, "TEST", date(2025, 1, 1))
        summary = result.summary()
        assert "v1.0" in summary
        assert "1pass" in summary
        assert "1fail" in summary
        assert "1skip" in summary

    def test_a_no_rules_summary_has_no_version(self, registry: Registry) -> None:
        result = check(record(), registry, "EU", date(2024, 6, 1))
        assert result.summary() == "no_rules"


class TestCheckAll:
    def test_it_returns_one_result_per_record(
        self, registry: Registry, sample_records: Any
    ) -> None:
        results = check_all(list(sample_records), registry, "US", date(2025, 6, 1))
        assert len(results) == len(sample_records)
        assert [r.record_id for r in results] == [r.record_id for r in sample_records]

    def test_the_shipped_sample_produces_a_spread_of_outcomes(
        self, registry: Registry, sample_records: Any
    ) -> None:
        """The sample data is meant to demonstrate every interesting case."""
        results = check_all(list(sample_records), registry, "US", date(2025, 6, 1))
        outcomes = {r.outcome for r in results}
        assert Outcome.COMPLIANT in outcomes
        assert Outcome.NON_COMPLIANT in outcomes
        assert Outcome.INDETERMINATE in outcomes

    def test_a_gap_date_makes_every_record_no_rules(
        self, registry: Registry, sample_records: Any
    ) -> None:
        results = check_all(list(sample_records), registry, "EU", date(2024, 6, 1))
        assert {r.outcome for r in results} == {Outcome.NO_RULES}


class TestRealRecordsAgainstRealRules:
    """Named cases from the shipped sample, so the documented behaviour is pinned."""

    def test_a_lithium_metal_battery_fails_the_us_restriction(
        self, registry: Registry, sample_records: Any
    ) -> None:
        r8 = next(r for r in sample_records if r.record_id == "R-8")
        result = check(r8, registry, "US", date(2025, 6, 1))
        assert result.outcome is Outcome.NON_COMPLIANT
        assert "US-BATT-01" in [f.requirement_id for f in result.failures]

    def test_a_lowercase_certificate_number_fails_the_format_rule(
        self, registry: Registry, sample_records: Any
    ) -> None:
        r7 = next(r for r in sample_records if r.record_id == "R-7")
        result = check(r7, registry, "US", date(2025, 6, 1))
        assert "US-CERT-02" in [f.requirement_id for f in result.failures]

    def test_a_missing_certificate_expiry_is_indeterminate_not_a_failure(
        self, registry: Registry, sample_records: Any
    ) -> None:
        r4 = next(r for r in sample_records if r.record_id == "R-4")
        result = check(r4, registry, "US", date(2025, 6, 1))
        assert result.outcome is Outcome.INDETERMINATE
        errored = [f for f in result.findings if f.status is Status.ERROR]
        assert [f.requirement_id for f in errored] == ["US-CERT-03"]

    def test_a_record_with_no_category_is_indeterminate_on_scoped_rules(
        self, registry: Registry, sample_records: Any
    ) -> None:
        r5 = next(r for r in sample_records if r.record_id == "R-5")
        result = check(r5, registry, "US", date(2025, 6, 1))
        assert result.outcome is Outcome.INDETERMINATE
        errored = {f.requirement_id for f in result.findings if f.status is Status.ERROR}
        assert errored == {"US-TOY-AGE-01", "US-BATT-01"}

    def test_a_record_declaring_unknown_substances_fails_the_eu_chemical_rule(
        self, registry: Registry, sample_records: Any
    ) -> None:
        r4 = next(r for r in sample_records if r.record_id == "R-4")
        result = check(r4, registry, "EU", date(2025, 6, 1))
        assert "EU-CHEM-01" in [f.requirement_id for f in result.failures]

    def test_a_240_volt_appliance_fails_the_jp_voltage_range(
        self, registry: Registry, sample_records: Any
    ) -> None:
        r8 = next(r for r in sample_records if r.record_id == "R-8")
        result = check(r8, registry, "JP", date(2025, 6, 1))
        assert "JP-VOLT-01" in [f.requirement_id for f in result.failures]

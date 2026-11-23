"""Temporal resolution: which rules were in force when.

This is the module the whole project exists for, so it gets property tests over the real
shipped rule sets rather than only hand-picked cases.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, timedelta
from typing import Any

import pytest

from compliance_check.core.evaluate import check
from compliance_check.models import Outcome
from compliance_check.rules.registry import Registry, today_utc
from compliance_check.rules.schema import RuleFileError, RuleSet
from tests.conftest import (
    DAILY_DATES,
    FRAMEWORK_DIR,
    FRAMEWORKS,
    SWEEP_DATES,
    registry_from,
    requirement,
    rule_doc,
)

#: Every boundary in the shipped rule sets, and the day either side of each.
BOUNDARIES = (
    date(2022, 1, 1),
    date(2024, 1, 1),
    date(2025, 1, 1),  # EU
    date(2024, 4, 1),  # JP
    date(2023, 1, 1),
    date(2024, 7, 1),
    date(2026, 1, 1),  # US
)
AROUND_BOUNDARIES = tuple(
    day + timedelta(days=offset) for day in BOUNDARIES for offset in (-1, 0, 1)
)


class TestResolutionIsByIntervalNotByFileOrder:
    """A version is chosen by its dates, never by where it sits in the file.

    If this were order-dependent, reformatting a rule file would silently change verdicts,
    and nothing about the output would show it.
    """

    @pytest.mark.parametrize("seed", range(8))
    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_shuffling_versions_does_not_change_which_one_resolves(
        self, shuffled_docs: Any, registry: Registry, seed: int, framework: str
    ) -> None:
        shuffled = registry_from(shuffled_docs(seed))
        for when in SWEEP_DATES:
            expected = registry.resolve(framework, when)
            actual = shuffled.resolve(framework, when)
            expected_label = expected.version if expected else None
            actual_label = actual.version if actual else None
            assert actual_label == expected_label, f"{framework} {when}"

    @pytest.mark.parametrize("seed", range(4))
    def test_shuffling_does_not_change_any_verdict(
        self, shuffled_docs: Any, registry: Registry, sample_records: Any, seed: int
    ) -> None:
        shuffled = registry_from(shuffled_docs(seed))
        for framework in FRAMEWORKS:
            for when in SWEEP_DATES[::4]:
                for record in sample_records:
                    before = check(record, registry, framework, when)
                    after = check(record, shuffled, framework, when)
                    assert after.outcome is before.outcome
                    assert after.digest == before.digest

    @pytest.mark.parametrize("seed", range(4))
    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_shuffling_does_not_change_a_digest(
        self, shuffled_docs: Any, registry: Registry, seed: int, framework: str
    ) -> None:
        shuffled = registry_from(shuffled_docs(seed))
        for when in SWEEP_DATES:
            expected = registry.resolve(framework, when)
            actual = shuffled.resolve(framework, when)
            assert (expected is None) == (actual is None)
            if expected and actual:
                assert actual.digest == expected.digest


class TestAtMostOneVersionIsEverInForce:
    """The invariant that makes resolution deterministic.

    Overlap is rejected at load time, so this is really a test that the load-time check and
    the runtime `covers` agree about what overlap means.
    """

    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_no_date_in_a_seven_year_daily_sweep_is_covered_twice(
        self, registry: Registry, framework: str
    ) -> None:
        versions = registry.rule_set(framework).versions
        for when in DAILY_DATES:
            covering = [v.version for v in versions if v.covers(when)]
            assert len(covering) <= 1, f"{framework} {when}: {covering}"

    @pytest.mark.parametrize("when", AROUND_BOUNDARIES)
    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_resolution_never_raises_around_a_boundary(
        self, registry: Registry, framework: str, when: date
    ) -> None:
        registry.resolve(framework, when)  # must not raise the multi-match guard

    def test_the_multi_match_guard_fires_if_the_invariant_is_ever_broken(self) -> None:
        """Constructed by bypassing validation, which is the only way to reach this.

        The guard is not reachable through the loader. It is here so that a future change
        which weakens overlap validation fails loudly instead of picking arbitrarily.
        """
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        rule_set = RuleSet.model_validate(doc)
        clone = rule_set.versions[0].model_copy(update={"version": "1.1"})
        rule_set.versions.append(clone)
        registry = Registry({"TEST": rule_set})
        with pytest.raises(RuleFileError, match="2 versions are in force"):
            registry.resolve("TEST", date(2025, 1, 1))


class TestBoundaries:
    """The changeover day belongs to the new version, not the old one."""

    def test_the_us_changeover_day_uses_the_new_version(self, registry: Registry) -> None:
        assert registry.resolve("US", date(2024, 7, 1)).version == "2024.1"

    def test_the_day_before_uses_the_old_version(self, registry: Registry) -> None:
        assert registry.resolve("US", date(2024, 6, 30)).version == "2023.1"

    def test_the_second_us_changeover(self, registry: Registry) -> None:
        assert registry.resolve("US", date(2025, 12, 31)).version == "2024.1"
        assert registry.resolve("US", date(2026, 1, 1)).version == "2026.1"

    def test_before_a_framework_existed_nothing_resolves(self, registry: Registry) -> None:
        assert registry.resolve("US", date(2022, 12, 31)) is None
        assert registry.resolve("JP", date(2024, 3, 31)) is None

    def test_inside_a_declared_gap_nothing_resolves(self, registry: Registry) -> None:
        assert registry.resolve("EU", date(2024, 6, 1)) is None

    def test_the_first_and_last_day_of_the_gap(self, registry: Registry) -> None:
        assert registry.resolve("EU", date(2023, 12, 31)).version == "2022.1"
        assert registry.resolve("EU", date(2024, 1, 1)) is None
        assert registry.resolve("EU", date(2024, 12, 31)) is None
        assert registry.resolve("EU", date(2025, 1, 1)).version == "2025.1"

    @pytest.mark.parametrize("framework", FRAMEWORKS)
    def test_every_declared_span_resolves_to_its_own_version(
        self, registry: Registry, framework: str
    ) -> None:
        for label, start, end, _ in registry.timeline(framework):
            assert registry.resolve(framework, start).version == label
            last = (end - timedelta(days=1)) if end else date(2035, 1, 1)
            assert registry.resolve(framework, last).version == label


class TestAsOfChangesTheAnswer:
    """The point of the project: the same record, two dates, two verdicts."""

    def test_a_toy_compliant_under_2024_rules_fails_under_2026_rules(
        self, registry: Registry
    ) -> None:
        from tests.conftest import record

        toy = record(
            "T-1",
            "toys",
            country_of_origin="Vietnam",
            certificate_number="US-234567",
            certificate_expiry="2027-03-01",
            small_parts="true",
            age_min_months="36",
            instruction_manual="included",
        )
        under_2024 = check(toy, registry, "US", date(2025, 6, 1))
        under_2026 = check(toy, registry, "US", date(2026, 6, 1))

        assert under_2024.version == "2024.1"
        assert under_2024.outcome is Outcome.COMPLIANT
        assert under_2026.version == "2026.1"
        assert under_2026.outcome is Outcome.NON_COMPLIANT
        assert under_2024.digest != under_2026.digest

    def test_the_failing_requirement_is_named(self, registry: Registry) -> None:
        from tests.conftest import record

        toy = record(
            "T-1",
            "toys",
            country_of_origin="Vietnam",
            certificate_number="US-234567",
            small_parts="true",
            age_min_months="36",
            instruction_manual="included",
        )
        result = check(toy, registry, "US", date(2026, 6, 1))
        assert [f.requirement_id for f in result.failures] == ["US-TOY-AGE-01"]
        assert "48" in result.failures[0].detail


class TestUnknownFramework:
    def test_an_unknown_framework_names_the_ones_that_exist(self, registry: Registry) -> None:
        with pytest.raises(RuleFileError, match="Available: EU, JP, US"):
            registry.resolve("XX", date(2025, 1, 1))

    def test_lookup_is_case_insensitive(self, registry: Registry) -> None:
        assert registry.resolve("us", date(2025, 6, 1)).version == "2024.1"


class TestTodayIsUtc:
    """A verdict must not depend on the timezone of the machine that produced it.

    `date.today()` is the obvious choice and would make two checkers disagree for a
    ~20-hour window on every changeover day.
    """

    def test_it_returns_a_date(self) -> None:
        assert isinstance(today_utc(), date)

    def test_it_matches_utc_not_local(self) -> None:
        from datetime import datetime

        assert today_utc() == datetime.now(UTC).date()


class TestTimeline:
    def test_it_is_ordered_by_start_date_regardless_of_file_order(
        self, mutable_docs: dict[str, Any]
    ) -> None:
        docs = copy.deepcopy(mutable_docs)
        docs["us"]["versions"].reverse()
        timeline = registry_from(docs).timeline("US")
        starts = [start for _, start, _, _ in timeline]
        assert starts == sorted(starts)
        assert [label for label, *_ in timeline] == ["2023.1", "2024.1", "2026.1"]

    def test_it_reports_the_requirement_count(self, registry: Registry) -> None:
        counts = {label: count for label, _, _, count in registry.timeline("US")}
        assert counts == {"2023.1": 3, "2024.1": 7, "2026.1": 6}


class TestFromDirectory:
    def test_it_loads_the_shipped_rules(self) -> None:
        registry = Registry.from_directory(FRAMEWORK_DIR)
        assert registry.frameworks() == ["EU", "JP", "US"]

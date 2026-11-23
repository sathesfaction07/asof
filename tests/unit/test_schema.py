"""Rule file validation, digests, and timeline integrity."""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from compliance_check.rules.loader import load_directory, load_rule_file
from compliance_check.rules.schema import RuleFileError, RuleSet
from tests.conftest import FRAMEWORK_DIR, requirement, rule_doc


def build(doc: dict[str, Any]) -> RuleSet:
    return RuleSet.model_validate(doc)


class TestTheShippedRuleFilesAreValid:
    """The repo's own data is covered by a test, so breaking it fails the build.

    Sample data that only gets exercised by somebody following the README is sample data
    that rots.
    """

    def test_every_shipped_file_loads(self) -> None:
        sets = load_directory(FRAMEWORK_DIR)
        assert sorted(sets) == ["EU", "JP", "US"]

    def test_us_has_the_three_versions_the_readme_describes(self) -> None:
        rule_set = load_rule_file(FRAMEWORK_DIR / "us.yaml")
        assert [v.version for v in rule_set.versions] == ["2023.1", "2024.1", "2026.1"]

    def test_eu_has_exactly_the_one_declared_gap(self) -> None:
        rule_set = load_rule_file(FRAMEWORK_DIR / "eu.yaml")
        assert rule_set.gaps() == [(date(2024, 1, 1), date(2025, 1, 1))]

    def test_frameworks_without_a_declared_gap_report_none(self) -> None:
        for name in ("us", "jp"):
            assert load_rule_file(FRAMEWORK_DIR / f"{name}.yaml").gaps() == []


class TestPredicateValidationHappensAtLoadTime:
    """A malformed rule must stop a deployment, not become a runtime error on every record.

    Without this, a typo in an operator name turns a requirement into noise that looks like
    a data quality problem.
    """

    def test_an_unknown_operator_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"op": "is_probably", "field": "a"})],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="unknown operator"):
            build(doc)

    def test_the_error_lists_the_operators_that_do_exist(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"op": "nope", "field": "a"})],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="present"):
            build(doc)

    def test_a_missing_op_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"field": "a"})],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="missing 'op'"):
            build(doc)

    def test_a_comparing_operator_without_a_value_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"op": "min", "field": "a"})],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="needs a 'value'"):
            build(doc)

    def test_a_unary_operator_with_a_value_is_rejected(self) -> None:
        """Almost always a sign the author meant `equals`."""
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [
                        requirement(assertion={"op": "present", "field": "a", "value": 1})
                    ],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="does not take a 'value'"):
            build(doc)

    def test_a_missing_field_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"op": "present"})],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="needs a 'field'"):
            build(doc)

    def test_an_empty_composite_is_rejected_at_load_time(self) -> None:
        """`all_of: []` would pass vacuously, so it never gets as far as evaluation."""
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"op": "all_of", "of": []})],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="non-empty 'of'"):
            build(doc)

    def test_a_composite_given_a_field_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [
                        requirement(
                            assertion={
                                "op": "all_of",
                                "field": "a",
                                "of": [{"op": "present", "field": "b"}],
                            }
                        )
                    ],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="takes 'of', not 'field'"):
            build(doc)

    def test_unexpected_keys_are_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [
                        requirement(assertion={"op": "present", "field": "a", "fields": "b"})
                    ],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="unexpected keys"):
            build(doc)

    def test_nested_predicates_are_validated_too(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [
                        requirement(
                            assertion={
                                "op": "all_of",
                                "of": [
                                    {"op": "present", "field": "a"},
                                    {"op": "wrong", "field": "b"},
                                ],
                            }
                        )
                    ],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="unknown operator"):
            build(doc)

    def test_runaway_nesting_is_stopped(self) -> None:
        predicate: dict[str, Any] = {"op": "present", "field": "a"}
        for _ in range(15):
            predicate = {"op": "all_of", "of": [predicate]}
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion=predicate)],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="nested more than"):
            build(doc)


class TestIntervalValidation:
    def test_a_version_that_ends_before_it_starts_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2025, 1, 1),
                    "effective_until": date(2024, 1, 1),
                    "requirements": [requirement()],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="never in force"):
            build(doc)

    def test_a_zero_length_version_is_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2025, 1, 1),
                    "effective_until": date(2025, 1, 1),
                    "requirements": [requirement()],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="never in force"):
            build(doc)

    def test_overlapping_versions_are_rejected(self) -> None:
        """Two versions in force at once means file order decides the verdict."""
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "effective_until": date(2025, 6, 1),
                    "requirements": [requirement()],
                },
                {
                    "version": "2.0",
                    "effective_from": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        with pytest.raises(RuleFileError, match="both in force"):
            build(doc)

    def test_the_overlap_message_explains_why_it_matters(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "effective_until": date(2025, 6, 1),
                    "requirements": [requirement()],
                },
                {
                    "version": "2.0",
                    "effective_from": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        with pytest.raises(RuleFileError, match="depend on file order"):
            build(doc)

    def test_two_open_ended_versions_overlap(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement()],
                },
                {
                    "version": "2.0",
                    "effective_from": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        with pytest.raises(RuleFileError, match="both in force"):
            build(doc)

    def test_versions_that_meet_exactly_at_a_boundary_do_not_overlap(self) -> None:
        """The whole reason the interval is half-open."""
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "effective_until": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
                {
                    "version": "2.0",
                    "effective_from": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        rule_set = build(doc)
        assert len(rule_set.versions) == 2
        assert rule_set.gaps() == []

    def test_a_gap_is_allowed_and_reported(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "effective_until": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
                {
                    "version": "2.0",
                    "effective_from": date(2026, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        assert build(doc).gaps() == [(date(2025, 1, 1), date(2026, 1, 1))]

    def test_duplicate_version_labels_are_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "effective_until": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
                {
                    "version": "1.0",
                    "effective_from": date(2025, 1, 1),
                    "requirements": [requirement()],
                },
            ]
        )
        with pytest.raises(RuleFileError, match="duplicate version labels"):
            build(doc)

    def test_duplicate_requirement_ids_are_rejected(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement("R-1"), requirement("R-1")],
                }
            ]
        )
        with pytest.raises(RuleFileError, match="duplicate requirement id"):
            build(doc)

    def test_a_framework_with_no_versions_is_rejected(self) -> None:
        with pytest.raises(RuleFileError, match="no versions"):
            build({"framework": "X", "versions": []})


class TestHalfOpenCoverage:
    """`covers` is the single source of truth for "in force", so its edges are pinned."""

    @pytest.fixture
    def version(self) -> Any:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 7, 1),
                    "effective_until": date(2026, 1, 1),
                    "requirements": [requirement()],
                }
            ]
        )
        return build(doc).versions[0]

    def test_the_first_day_is_covered(self, version: Any) -> None:
        assert version.covers(date(2024, 7, 1)) is True

    def test_the_day_before_is_not(self, version: Any) -> None:
        assert version.covers(date(2024, 6, 30)) is False

    def test_the_last_day_before_the_end_is_covered(self, version: Any) -> None:
        assert version.covers(date(2025, 12, 31)) is True

    def test_the_end_date_itself_is_not_covered(self, version: Any) -> None:
        """Half-open. An inclusive end would put the changeover day in two versions."""
        assert version.covers(date(2026, 1, 1)) is False

    def test_an_open_ended_version_covers_the_far_future(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement()],
                }
            ]
        )
        assert build(doc).versions[0].covers(date(9999, 1, 1)) is True


class TestDigest:
    """The digest is what makes a replay meaningful, so what changes it matters."""

    def test_the_same_content_gives_the_same_digest(self) -> None:
        doc = rule_doc()
        assert build(doc).versions[0].digest == build(copy.deepcopy(doc)).versions[0].digest

    def test_a_yaml_round_trip_does_not_change_it(self) -> None:
        for path in sorted(FRAMEWORK_DIR.glob("*.yaml")):
            original = build(yaml.safe_load(path.read_text(encoding="utf-8")))
            reformatted = build(
                yaml.safe_load(
                    yaml.safe_dump(yaml.safe_load(path.read_text(encoding="utf-8")), sort_keys=True)
                )
            )
            before = {v.version: v.digest for v in original.versions}
            after = {v.version: v.digest for v in reformatted.versions}
            assert before == after, path.name

    def test_reordering_requirements_does_not_change_it(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement("R-1"), requirement("R-2"), requirement("R-3")],
                }
            ]
        )
        reversed_doc = copy.deepcopy(doc)
        reversed_doc["versions"][0]["requirements"].reverse()
        assert build(doc).versions[0].digest == build(reversed_doc).versions[0].digest

    def test_changing_a_threshold_does_change_it(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [
                        requirement(assertion={"op": "min", "field": "a", "value": 36})
                    ],
                }
            ]
        )
        changed = copy.deepcopy(doc)
        changed["versions"][0]["requirements"][0]["assert"]["value"] = 48
        assert build(doc).versions[0].digest != build(changed).versions[0].digest

    def test_changing_a_severity_does_change_it(self) -> None:
        doc = rule_doc()
        changed = copy.deepcopy(doc)
        changed["versions"][0]["requirements"][0]["severity"] = "critical"
        assert build(doc).versions[0].digest != build(changed).versions[0].digest

    def test_changing_the_interval_does_change_it(self) -> None:
        doc = rule_doc()
        changed = copy.deepcopy(doc)
        changed["versions"][0]["effective_from"] = date(2024, 2, 1)
        assert build(doc).versions[0].digest != build(changed).versions[0].digest

    def test_editing_the_title_does_not_change_it(self) -> None:
        """A digest over prose would fire on every copy-edit and get ignored."""
        doc = rule_doc()
        changed = copy.deepcopy(doc)
        changed["versions"][0]["requirements"][0]["title"] = "reworded entirely"
        assert build(doc).versions[0].digest == build(changed).versions[0].digest

    def test_editing_a_note_does_not_change_it(self) -> None:
        doc = rule_doc()
        changed = copy.deepcopy(doc)
        changed["versions"][0]["notes"] = "some new explanation"
        assert build(doc).versions[0].digest == build(changed).versions[0].digest

    def test_editing_one_version_leaves_the_others_alone(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "effective_until": date(2025, 1, 1),
                    "requirements": [requirement("R-1")],
                },
                {
                    "version": "2.0",
                    "effective_from": date(2025, 1, 1),
                    "requirements": [requirement("R-2")],
                },
            ]
        )
        changed = copy.deepcopy(doc)
        changed["versions"][1]["requirements"][0]["severity"] = "critical"
        before = {v.version: v.digest for v in build(doc).versions}
        after = {v.version: v.digest for v in build(changed).versions}
        assert before["1.0"] == after["1.0"]
        assert before["2.0"] != after["2.0"]


class TestAppliesToShorthand:
    def test_a_scalar_is_accepted_as_a_one_item_list(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(applies_to={"category": "toys"})],
                }
            ]
        )
        assert build(doc).versions[0].requirements[0].applies_to == {"category": ["toys"]}

    def test_a_list_is_left_alone(self) -> None:
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(applies_to={"category": ["toys", "apparel"]})],
                }
            ]
        )
        assert build(doc).versions[0].requirements[0].applies_to == {
            "category": ["toys", "apparel"]
        }


class TestLoader:
    def test_a_missing_directory_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(RuleFileError, match="is not a directory"):
            load_directory(tmp_path / "nope")

    def test_an_empty_directory_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(RuleFileError, match="no .yaml"):
            load_directory(tmp_path)

    def test_invalid_yaml_names_the_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.yaml"
        bad.write_text("framework: X\n  bad indent: [", encoding="utf-8")
        with pytest.raises(RuleFileError, match="broken.yaml"):
            load_rule_file(bad)

    def test_an_empty_file_is_reported(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(RuleFileError, match="is empty"):
            load_rule_file(empty)

    def test_a_top_level_list_is_rejected(self, tmp_path: Path) -> None:
        wrong = tmp_path / "list.yaml"
        wrong.write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(RuleFileError, match="must be a mapping"):
            load_rule_file(wrong)

    def test_a_missing_file_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(RuleFileError, match="cannot read"):
            load_rule_file(tmp_path / "absent.yaml")

    def test_the_same_framework_in_two_files_is_rejected(self, tmp_path: Path) -> None:
        for name in ("a.yaml", "b.yaml"):
            (tmp_path / name).write_text(
                yaml.safe_dump(rule_doc(framework="DUP")), encoding="utf-8"
            )
        with pytest.raises(RuleFileError, match="declared in both"):
            load_directory(tmp_path)

    def test_a_validation_error_is_attributed_to_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.yaml"
        doc = rule_doc(
            versions=[
                {
                    "version": "1.0",
                    "effective_from": date(2024, 1, 1),
                    "requirements": [requirement(assertion={"op": "nonsense", "field": "a"})],
                }
            ]
        )
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with pytest.raises(RuleFileError, match=r"broken\.yaml.*unknown operator"):
            load_rule_file(path)

    def test_a_pydantic_shape_error_is_also_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "shape.yaml"
        path.write_text("framework: X\nversions: not-a-list\n", encoding="utf-8")
        with pytest.raises(RuleFileError, match="shape.yaml"):
            load_rule_file(path)

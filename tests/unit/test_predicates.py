"""The predicate language."""

from __future__ import annotations

from datetime import date

import pytest

from compliance_check.models import Status
from compliance_check.rules.predicates import (
    ALL_OPS,
    _as_bool,
    _as_date,
    _as_number,
    _loose_equal,
    evaluate_predicate,
    is_present,
)
from tests.conftest import record


class TestIsPresent:
    """`present` must not be truthiness.

    The values most likely to matter in a compliance record are exactly the falsy ones: a
    declared `false`, a stated `0`. Truthiness reports both as missing, which inverts the
    meaning of the field.
    """

    @pytest.mark.parametrize("value", [False, 0, 0.0, "0", "false", [0], {"a": 0}, "x", 1])
    def test_declared_values_are_present_even_when_falsy(self, value: object) -> None:
        assert is_present(value) is True

    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n", [], {}, set()])
    def test_blank_and_missing_are_absent(self, value: object) -> None:
        assert is_present(value) is False

    def test_a_spreadsheet_space_is_not_a_value(self) -> None:
        assert is_present(" ") is False


class TestUnaryOperators:
    def test_present_passes_when_declared(self) -> None:
        status, detail = evaluate_predicate({"op": "present", "field": "a"}, record(a="x"))
        assert status is Status.PASS
        assert "present" in detail

    def test_present_fails_when_blank(self) -> None:
        status, _ = evaluate_predicate({"op": "present", "field": "a"}, record(a="  "))
        assert status is Status.FAIL

    def test_present_fails_when_the_field_does_not_exist(self) -> None:
        status, detail = evaluate_predicate({"op": "present", "field": "a"}, record())
        assert status is Status.FAIL
        assert "missing" in detail

    def test_present_passes_for_a_declared_false(self) -> None:
        status, _ = evaluate_predicate({"op": "present", "field": "a"}, record(a=False))
        assert status is Status.PASS

    def test_absent_is_the_mirror(self) -> None:
        assert evaluate_predicate({"op": "absent", "field": "a"}, record())[0] is Status.PASS
        assert evaluate_predicate({"op": "absent", "field": "a"}, record(a="x"))[0] is Status.FAIL

    def test_truthy_is_available_when_truthiness_is_actually_wanted(self) -> None:
        assert evaluate_predicate({"op": "truthy", "field": "a"}, record(a=1))[0] is Status.PASS
        assert evaluate_predicate({"op": "truthy", "field": "a"}, record(a=0))[0] is Status.FAIL


class TestMissingFieldsAreErrorsNotFailures:
    """A comparison against a field that is not there cannot be judged.

    Returning FAIL would be the shortcut, and it would tell whoever reads the report that
    the value is wrong when the value is missing. Those need different fixes, and the
    distinction is what INDETERMINATE is built on.
    """

    @pytest.mark.parametrize(
        "op,value",
        [
            ("equals", "x"),
            ("not_equals", "x"),
            ("one_of", ["x"]),
            ("not_one_of", ["x"]),
            ("matches", "^x$"),
            ("min", 1),
            ("max", 1),
            ("before", "2025-01-01"),
            ("after", "2025-01-01"),
        ],
    )
    def test_every_comparing_operator_errors_on_an_absent_field(
        self, op: str, value: object
    ) -> None:
        status, detail = evaluate_predicate({"op": op, "field": "a", "value": value}, record())
        assert status is Status.ERROR
        assert "missing" in detail

    def test_a_blank_string_is_treated_the_same_as_absent(self) -> None:
        status, _ = evaluate_predicate({"op": "equals", "field": "a", "value": "x"}, record(a=""))
        assert status is Status.ERROR


class TestEquality:
    """Equality has to survive a trip through CSV, where everything is a string."""

    @pytest.mark.parametrize("declared", ["true", "TRUE", " True ", "yes", "y", "1"])
    def test_spreadsheet_truth_matches_yaml_true(self, declared: str) -> None:
        predicate = {"op": "equals", "field": "a", "value": True}
        assert evaluate_predicate(predicate, record(a=declared))[0] is Status.PASS

    @pytest.mark.parametrize("declared", ["false", "FALSE", " no ", "n", "0"])
    def test_spreadsheet_falsehood_matches_yaml_false(self, declared: str) -> None:
        predicate = {"op": "equals", "field": "a", "value": False}
        assert evaluate_predicate(predicate, record(a=declared))[0] is Status.PASS

    def test_numeric_one_is_not_a_declared_true(self) -> None:
        """`1 == True` in Python. A numeric 1 is not a declaration, so this must not match."""
        assert _loose_equal(1, True) is False
        assert _loose_equal(True, 1) is False

    def test_strings_compare_case_insensitively(self) -> None:
        predicate = {"op": "equals", "field": "a", "value": "none"}
        assert evaluate_predicate(predicate, record(a="None"))[0] is Status.PASS

    def test_a_numeric_string_matches_a_number(self) -> None:
        predicate = {"op": "equals", "field": "a", "value": 36}
        assert evaluate_predicate(predicate, record(a="36"))[0] is Status.PASS

    def test_unrelated_types_simply_do_not_match(self) -> None:
        assert _loose_equal("toys", 5) is False


class TestCoercion:
    @pytest.mark.parametrize(
        "value,expected", [(36, 36.0), ("36", 36.0), (" 36.5 ", 36.5), (0, 0.0)]
    )
    def test_numbers_are_accepted(self, value: object, expected: float) -> None:
        assert _as_number(value) == expected

    @pytest.mark.parametrize("value", ["36 months", "", "abc", None, True, False, ["36"]])
    def test_anything_ambiguous_is_refused(self, value: object) -> None:
        """`"36 months"` is not silently read as 36. Guessing is how a checker invents data."""
        assert _as_number(value) is None

    def test_booleans_are_not_numbers_even_though_python_says_so(self) -> None:
        assert _as_number(True) is None

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("2025-01-01", date(2025, 1, 1)),
            (" 2025-01-01 ", date(2025, 1, 1)),
            ("2025-01-01T12:30:00", date(2025, 1, 1)),
            (date(2025, 1, 1), date(2025, 1, 1)),
        ],
    )
    def test_dates_are_accepted(self, value: object, expected: date) -> None:
        assert _as_date(value) == expected

    @pytest.mark.parametrize("value", ["01/01/2025", "not a date", "", None, 20250101])
    def test_non_iso_dates_are_refused(self, value: object) -> None:
        assert _as_date(value) is None

    @pytest.mark.parametrize(
        "value,expected", [("yes", True), ("NO", False), (True, True), ("maybe", None)]
    )
    def test_boolean_coercion(self, value: object, expected: bool | None) -> None:
        assert _as_bool(value) is expected


class TestNumericComparison:
    def test_min_is_inclusive(self) -> None:
        predicate = {"op": "min", "field": "a", "value": 36}
        assert evaluate_predicate(predicate, record(a=36))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a=35))[0] is Status.FAIL

    def test_max_is_inclusive(self) -> None:
        predicate = {"op": "max", "field": "a", "value": 110}
        assert evaluate_predicate(predicate, record(a=110))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a=111))[0] is Status.FAIL

    def test_an_uncoercible_value_errors_rather_than_failing(self) -> None:
        predicate = {"op": "min", "field": "a", "value": 36}
        status, detail = evaluate_predicate(predicate, record(a="36 months"))
        assert status is Status.ERROR
        assert "not a number" in detail

    def test_a_bad_threshold_is_reported_as_a_rule_error(self) -> None:
        predicate = {"op": "min", "field": "a", "value": "lots"}
        status, detail = evaluate_predicate(predicate, record(a=36))
        assert status is Status.ERROR
        assert detail.startswith("rule error")

    def test_the_detail_does_not_show_a_spurious_decimal(self) -> None:
        predicate = {"op": "min", "field": "a", "value": 36}
        _, detail = evaluate_predicate(predicate, record(a="36"))
        assert "36.0" not in detail
        assert "36 >= 36" in detail


class TestDateComparison:
    def test_after_is_strict(self) -> None:
        predicate = {"op": "after", "field": "a", "value": "2025-01-01"}
        assert evaluate_predicate(predicate, record(a="2025-01-02"))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a="2025-01-01"))[0] is Status.FAIL

    def test_before_is_strict(self) -> None:
        predicate = {"op": "before", "field": "a", "value": "2025-01-01"}
        assert evaluate_predicate(predicate, record(a="2024-12-31"))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a="2025-01-01"))[0] is Status.FAIL

    def test_a_datetime_is_truncated_so_the_time_of_day_cannot_matter(self) -> None:
        predicate = {"op": "after", "field": "a", "value": "2025-01-01"}
        assert evaluate_predicate(predicate, record(a="2025-01-01T23:59:59"))[0] is Status.FAIL

    def test_an_unparseable_date_errors(self) -> None:
        predicate = {"op": "after", "field": "a", "value": "2025-01-01"}
        status, detail = evaluate_predicate(predicate, record(a="01/01/2026"))
        assert status is Status.ERROR
        assert "not a date" in detail


class TestMembership:
    def test_one_of(self) -> None:
        predicate = {"op": "one_of", "field": "a", "value": ["x", "y"]}
        assert evaluate_predicate(predicate, record(a="Y"))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a="z"))[0] is Status.FAIL

    def test_a_scalar_value_is_treated_as_a_one_item_list(self) -> None:
        predicate = {"op": "one_of", "field": "a", "value": "x"}
        assert evaluate_predicate(predicate, record(a="x"))[0] is Status.PASS

    def test_not_one_of_is_the_mirror(self) -> None:
        predicate = {"op": "not_one_of", "field": "a", "value": ["x"]}
        assert evaluate_predicate(predicate, record(a="y"))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a="x"))[0] is Status.FAIL


class TestPatterns:
    def test_matches_searches(self) -> None:
        predicate = {"op": "matches", "field": "a", "value": "^[A-Z]{2}-[0-9]{6}$"}
        assert evaluate_predicate(predicate, record(a="US-123456"))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a="us-123456"))[0] is Status.FAIL

    def test_a_bad_pattern_is_a_rule_error_not_a_crash(self) -> None:
        predicate = {"op": "matches", "field": "a", "value": "([unclosed"}
        status, detail = evaluate_predicate(predicate, record(a="x"))
        assert status is Status.ERROR
        assert "bad pattern" in detail

    def test_a_non_string_pattern_is_a_rule_error(self) -> None:
        predicate = {"op": "matches", "field": "a", "value": 5}
        assert evaluate_predicate(predicate, record(a="5"))[0] is Status.ERROR


class TestComposites:
    """The interaction between short-circuiting and the ERROR state.

    This is the subtle part. Each rule below has a reason, and getting one backwards would
    quietly change verdicts rather than crash.
    """

    def test_all_of_passes_when_every_clause_passes(self) -> None:
        predicate = {
            "op": "all_of",
            "of": [{"op": "present", "field": "a"}, {"op": "present", "field": "b"}],
        }
        assert evaluate_predicate(predicate, record(a=1, b=2))[0] is Status.PASS

    def test_a_known_failure_outranks_an_unknown_in_all_of(self) -> None:
        """FAIL beats ERROR: a proven violation is more actionable than an unknown."""
        predicate = {
            "op": "all_of",
            "of": [
                {"op": "present", "field": "missing"},  # FAIL
                {"op": "min", "field": "absent_field", "value": 1},  # ERROR
            ],
        }
        assert evaluate_predicate(predicate, record())[0] is Status.FAIL

    def test_all_of_is_unknown_when_a_clause_is_unknown_and_none_failed(self) -> None:
        predicate = {
            "op": "all_of",
            "of": [
                {"op": "present", "field": "a"},  # PASS
                {"op": "min", "field": "absent_field", "value": 1},  # ERROR
            ],
        }
        assert evaluate_predicate(predicate, record(a=1))[0] is Status.ERROR

    def test_any_of_passes_on_one_pass_regardless_of_the_rest(self) -> None:
        predicate = {
            "op": "any_of",
            "of": [
                {"op": "present", "field": "a"},  # PASS
                {"op": "min", "field": "absent_field", "value": 1},  # ERROR
            ],
        }
        assert evaluate_predicate(predicate, record(a=1))[0] is Status.PASS

    def test_any_of_is_unknown_when_nothing_passed_but_something_was_unknown(self) -> None:
        """The unknown clause might have been the one that satisfied it."""
        predicate = {
            "op": "any_of",
            "of": [
                {"op": "present", "field": "missing"},  # FAIL
                {"op": "min", "field": "absent_field", "value": 1},  # ERROR
            ],
        }
        assert evaluate_predicate(predicate, record())[0] is Status.ERROR

    def test_any_of_fails_only_when_every_clause_definitively_failed(self) -> None:
        predicate = {
            "op": "any_of",
            "of": [{"op": "present", "field": "x"}, {"op": "present", "field": "y"}],
        }
        assert evaluate_predicate(predicate, record())[0] is Status.FAIL

    def test_none_of_fails_when_a_forbidden_condition_holds(self) -> None:
        predicate = {"op": "none_of", "of": [{"op": "equals", "field": "a", "value": "bad"}]}
        assert evaluate_predicate(predicate, record(a="bad"))[0] is Status.FAIL
        assert evaluate_predicate(predicate, record(a="fine"))[0] is Status.PASS

    def test_an_empty_composite_is_an_error_not_a_vacuous_pass(self) -> None:
        """`all([])` is True. A requirement whose last clause was deleted must not pass."""
        for op in ("all_of", "any_of", "none_of"):
            status, detail = evaluate_predicate({"op": op, "of": []}, record())
            assert status is Status.ERROR, op
            assert "no sub-predicates" in detail

    def test_composites_nest(self) -> None:
        predicate = {
            "op": "all_of",
            "of": [
                {"op": "present", "field": "a"},
                {
                    "op": "any_of",
                    "of": [
                        {"op": "equals", "field": "b", "value": 1},
                        {"op": "present", "field": "c"},
                    ],
                },
            ],
        }
        assert evaluate_predicate(predicate, record(a=1, b=2, c=3))[0] is Status.PASS
        assert evaluate_predicate(predicate, record(a=1, b=1))[0] is Status.PASS


class TestUnknownOperator:
    def test_an_unknown_operator_errors_rather_than_passing(self) -> None:
        status, detail = evaluate_predicate({"op": "sounds_right", "field": "a"}, record(a=1))
        assert status is Status.ERROR
        assert "unknown operator" in detail

    def test_the_operator_sets_do_not_overlap(self) -> None:
        from compliance_check.rules.predicates import BINARY_OPS, COMPOSITE_OPS, UNARY_OPS

        assert not UNARY_OPS & BINARY_OPS
        assert not UNARY_OPS & COMPOSITE_OPS
        assert not BINARY_OPS & COMPOSITE_OPS
        assert UNARY_OPS | BINARY_OPS | COMPOSITE_OPS == ALL_OPS

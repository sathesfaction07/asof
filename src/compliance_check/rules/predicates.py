"""The assertion language used inside rule files.

Rule sets are data, so the operators available to them are a closed, small set defined
here. Adding an operator is a deliberate code change; writing a rule is not.

Every operator returns a tri-state. A predicate that cannot be evaluated returns ERROR
rather than False, because "the label is missing so we cannot check its format" and "the
label is present and the format is wrong" call for different actions from a human.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, NamedTuple

from compliance_check.models import Record, Status


class Evaluation(NamedTuple):
    status: Status
    detail: str


#: Operators that compare against a `value`, and those that do not.
UNARY_OPS = frozenset({"present", "absent", "truthy"})
BINARY_OPS = frozenset(
    {"equals", "not_equals", "one_of", "not_one_of", "matches", "min", "max", "before", "after"}
)
COMPOSITE_OPS = frozenset({"all_of", "any_of", "none_of"})
ALL_OPS = UNARY_OPS | BINARY_OPS | COMPOSITE_OPS


def is_present(value: Any) -> bool:
    """Whether a value counts as supplied.

    Explicitly not `bool(value)`. A rule asking whether `small_parts` is declared must
    see `False` as an answer, and `age_min_months: 0` as a stated zero rather than a
    blank. Truthiness would treat both as missing, which inverts the meaning of the
    field for exactly the values most likely to matter.

    Empty and whitespace-only strings do not count: a spreadsheet cell containing a
    space is not a country of origin.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | dict | tuple | set):
        return bool(value)
    return True


def _as_number(value: Any) -> float | None:
    """Coerce to a number, or None if that is not honest.

    Strings are accepted because CSV has no types and `"36"` is plainly a number. `"36
    months"` is not coerced: guessing that it means 36 is how a checker starts inventing
    data. Booleans are rejected even though Python says `True == 1`, since comparing a
    declared flag against a numeric threshold is a rule-authoring mistake worth showing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_date(value: Any) -> date | None:
    """Coerce to a date, or None.

    Accepts a date, a datetime (truncated), or an ISO-8601 string. A datetime is
    truncated to its date deliberately: rule boundaries are days, and keeping a time
    component would make a comparison depend on when in the day a check ran.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _compare_number(op: str, actual: Any, expected: Any, field: str) -> Evaluation:
    left = _as_number(actual)
    right = _as_number(expected)
    if right is None:
        return Evaluation(Status.ERROR, f"rule error: {op} threshold {expected!r} is not a number")
    if left is None:
        return Evaluation(
            Status.ERROR, f"{field}={actual!r} is not a number, so {op} cannot be applied"
        )
    if op == "min":
        ok = left >= right
        return Evaluation(
            Status.PASS if ok else Status.FAIL,
            f"{field}={_fmt(left)} {'>=' if ok else '<'} {_fmt(right)}",
        )
    ok = left <= right
    return Evaluation(
        Status.PASS if ok else Status.FAIL,
        f"{field}={_fmt(left)} {'<=' if ok else '>'} {_fmt(right)}",
    )


def _compare_date(op: str, actual: Any, expected: Any, field: str) -> Evaluation:
    left = _as_date(actual)
    right = _as_date(expected)
    if right is None:
        return Evaluation(Status.ERROR, f"rule error: {op} bound {expected!r} is not a date")
    if left is None:
        return Evaluation(
            Status.ERROR, f"{field}={actual!r} is not a date, so {op} cannot be applied"
        )
    ok = left < right if op == "before" else left > right
    symbol = "<" if op == "before" else ">"
    return Evaluation(
        Status.PASS if ok else Status.FAIL,
        f"{field}={left.isoformat()} {symbol if ok else 'not ' + symbol} {right.isoformat()}",
    )


def _fmt(number: float) -> str:
    """Render a coerced number without a spurious .0."""
    return str(int(number)) if number == int(number) else str(number)


def evaluate_predicate(predicate: dict[str, Any], record: Record) -> Evaluation:
    """Apply one predicate to one record.

    Composite operators recurse. Their short-circuit behaviour is deliberate and is
    documented where it happens, because the interaction between short-circuiting and
    the ERROR state is the subtle part of this module.
    """
    op = predicate.get("op")
    if op in COMPOSITE_OPS:
        return _evaluate_composite(op, predicate.get("of") or [], record)

    field = predicate.get("field", "")
    present, value = record.get(field)

    if op == "present":
        ok = present and is_present(value)
        return Evaluation(
            Status.PASS if ok else Status.FAIL,
            f"{field} is present" if ok else f"{field} is missing or blank",
        )
    if op == "absent":
        ok = not (present and is_present(value))
        return Evaluation(
            Status.PASS if ok else Status.FAIL,
            f"{field} is absent" if ok else f"{field}={value!r} is present but must not be",
        )
    if op == "truthy":
        ok = bool(value)
        return Evaluation(Status.PASS if ok else Status.FAIL, f"{field}={value!r}")

    # Every remaining operator compares a value, so an absent field cannot be judged.
    # Returning FAIL here would be the tempting shortcut and would conflate "declared
    # wrongly" with "not declared", which are different problems for whoever fixes it.
    if not present or not is_present(value):
        return Evaluation(Status.ERROR, f"{field} is missing, so {op} cannot be applied")

    expected = predicate.get("value")

    if op == "equals":
        ok = _loose_equal(value, expected)
        return Evaluation(Status.PASS if ok else Status.FAIL, f"{field}={value!r} vs {expected!r}")
    if op == "not_equals":
        ok = not _loose_equal(value, expected)
        return Evaluation(Status.PASS if ok else Status.FAIL, f"{field}={value!r} vs {expected!r}")
    if op in ("one_of", "not_one_of"):
        options = expected if isinstance(expected, list) else [expected]
        inside = any(_loose_equal(value, option) for option in options)
        ok = inside if op == "one_of" else not inside
        return Evaluation(
            Status.PASS if ok else Status.FAIL,
            f"{field}={value!r} {'in' if inside else 'not in'} {options!r}",
        )
    if op == "matches":
        if not isinstance(expected, str):
            return Evaluation(
                Status.ERROR, f"rule error: matches needs a pattern, got {expected!r}"
            )
        try:
            pattern = re.compile(expected)
        except re.error as exc:
            return Evaluation(Status.ERROR, f"rule error: bad pattern {expected!r} ({exc})")
        ok = bool(pattern.search(str(value)))
        return Evaluation(Status.PASS if ok else Status.FAIL, f"{field}={value!r} vs /{expected}/")
    if op in ("min", "max"):
        return _compare_number(op, value, expected, field)
    if op in ("before", "after"):
        return _compare_date(op, value, expected, field)

    return Evaluation(Status.ERROR, f"rule error: unknown operator {op!r}")


def _loose_equal(actual: Any, expected: Any) -> bool:
    """Equality that survives a trip through CSV.

    `"true"` from a spreadsheet and `True` from YAML mean the same thing, and a rule
    author should not have to know which the pipeline produced. Booleans are compared as
    booleans first so that `1 == True` does not sneak through: a numeric 1 is not a
    declaration.
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return _as_bool(actual) is _as_bool(expected)
    if isinstance(actual, str) != isinstance(expected, str):
        left, right = _as_number(actual), _as_number(expected)
        if left is not None and right is not None:
            return left == right
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip().casefold() == expected.strip().casefold()
    return bool(actual == expected)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("true", "yes", "y", "1"):
            return True
        if text in ("false", "no", "n", "0"):
            return False
    return None


def _evaluate_composite(op: str, parts: list[dict[str, Any]], record: Record) -> Evaluation:
    """Combine sub-predicates.

    An empty `of` list is a rule-authoring error rather than a vacuous pass. `all_of: []`
    is True under Python's `all`, and a requirement that always passes because somebody
    deleted its last clause is the worst possible failure mode for a compliance check.
    """
    if not parts:
        return Evaluation(Status.ERROR, f"rule error: {op} has no sub-predicates")

    results = [evaluate_predicate(part, record) for part in parts]
    details = "; ".join(r.detail for r in results)

    if op == "all_of":
        # ERROR outranks FAIL. If one clause could not be evaluated, the conjunction's
        # true value is unknown even when another clause already failed -- but a known
        # failure is more actionable than an unknown, so FAIL wins over ERROR here.
        if any(r.status is Status.FAIL for r in results):
            return Evaluation(Status.FAIL, details)
        if any(r.status is Status.ERROR for r in results):
            return Evaluation(Status.ERROR, details)
        return Evaluation(Status.PASS, details)

    if op == "any_of":
        # One PASS is enough regardless of what the others did.
        if any(r.status is Status.PASS for r in results):
            return Evaluation(Status.PASS, details)
        # No pass. If anything was unevaluable, the disjunction might have been satisfied
        # by that clause, so this is unknown rather than a failure.
        if any(r.status is Status.ERROR for r in results):
            return Evaluation(Status.ERROR, details)
        return Evaluation(Status.FAIL, details)

    # none_of: a PASS among the sub-predicates means a forbidden condition holds.
    if any(r.status is Status.PASS for r in results):
        return Evaluation(Status.FAIL, details)
    if any(r.status is Status.ERROR for r in results):
        return Evaluation(Status.ERROR, details)
    return Evaluation(Status.PASS, details)

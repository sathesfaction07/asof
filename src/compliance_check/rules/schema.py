"""Schema for rule files.

Rule sets are data that changes on a regulatory timetable, not code that changes on a
release timetable. That means the schema has to be validated at load time and hard
enough to catch authoring mistakes, because there is no compiler between a rule author
and production.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_check.models import Severity
from compliance_check.rules.predicates import ALL_OPS, BINARY_OPS, COMPOSITE_OPS, UNARY_OPS


class RuleFileError(Exception):
    """A rule file is malformed. Raised at load time, never during a check."""


class Requirement(BaseModel):
    """One thing that must hold for a record to be compliant."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    severity: Severity = Severity.MAJOR

    #: Optional scoping: field -> acceptable values. Absent means "applies to everything".
    applies_to: dict[str, list[Any]] = Field(default_factory=dict)

    #: The predicate that must hold. Keyed `assert` in YAML, which is a Python keyword.
    assertion: dict[str, Any] = Field(alias="assert")

    citation: str = ""

    @field_validator("applies_to", mode="before")
    @classmethod
    def _wrap_scalars(cls, value: Any) -> Any:
        """Allow `category: toys` as shorthand for `category: [toys]`.

        Rule files are written by hand and the single-value case is common. Silently
        accepting a scalar avoids a class of frustrating load errors; not accepting it
        would be defensible too, but the shorthand cannot be ambiguous here.
        """
        if isinstance(value, dict):
            return {
                key: (inner if isinstance(inner, list) else [inner]) for key, inner in value.items()
            }
        return value

    @model_validator(mode="after")
    def _check_assertion(self) -> Requirement:
        _validate_predicate(self.assertion, f"requirement {self.id}")
        return self


def _validate_predicate(predicate: Any, where: str, depth: int = 0) -> None:
    """Reject a malformed predicate at load time.

    This is the difference between "the rule file is wrong" and "every record produces an
    unexplained error". A typo in an operator name should stop a deployment, not quietly
    turn a requirement into noise.
    """
    if depth > 10:
        raise RuleFileError(f"{where}: predicate nested more than 10 deep")
    if not isinstance(predicate, dict):
        raise RuleFileError(f"{where}: predicate must be a mapping, got {type(predicate).__name__}")

    op = predicate.get("op")
    if op is None:
        raise RuleFileError(f"{where}: predicate is missing 'op'")
    if op not in ALL_OPS:
        known = ", ".join(sorted(ALL_OPS))
        raise RuleFileError(f"{where}: unknown operator {op!r}. Known operators: {known}")

    if op in COMPOSITE_OPS:
        parts = predicate.get("of")
        if not isinstance(parts, list) or not parts:
            # An empty composite is the dangerous case: `all_of: []` passes vacuously.
            raise RuleFileError(f"{where}: {op} needs a non-empty 'of' list")
        for part in parts:
            _validate_predicate(part, where, depth + 1)
        if "field" in predicate:
            raise RuleFileError(f"{where}: {op} takes 'of', not 'field'")
        return

    if not predicate.get("field"):
        raise RuleFileError(f"{where}: {op} needs a 'field'")
    if op in BINARY_OPS and "value" not in predicate:
        raise RuleFileError(f"{where}: {op} needs a 'value'")
    if op in UNARY_OPS and "value" in predicate:
        raise RuleFileError(f"{where}: {op} does not take a 'value'")

    unexpected = set(predicate) - {"op", "field", "value", "of"}
    if unexpected:
        raise RuleFileError(f"{where}: unexpected keys {sorted(unexpected)}")


class RuleVersion(BaseModel):
    """A dated snapshot of a framework's requirements.

    The interval is HALF-OPEN: in force on day d when
    `effective_from <= d < effective_until`. An inclusive upper bound would make the
    changeover day belong to two versions at once, and then which one applies depends on
    iteration order. Overlaps are rejected below, in RuleSet.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    effective_from: date
    effective_until: date | None = None
    notes: str = ""
    requirements: list[Requirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_interval_and_ids(self) -> RuleVersion:
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise RuleFileError(
                f"version {self.version}: effective_until {self.effective_until} is not after "
                f"effective_from {self.effective_from}, so this version is never in force"
            )
        seen: set[str] = set()
        for requirement in self.requirements:
            if requirement.id in seen:
                raise RuleFileError(
                    f"version {self.version}: duplicate requirement id {requirement.id!r}"
                )
            seen.add(requirement.id)
        return self

    def covers(self, as_of: date) -> bool:
        """Whether this version is in force on `as_of`. Half-open, per the class docstring."""
        if as_of < self.effective_from:
            return False
        return self.effective_until is None or as_of < self.effective_until

    @property
    def digest(self) -> str:
        """A stable content hash of what this version actually asserts.

        Computed over canonical JSON of the requirements plus the interval, so
        reformatting the YAML or reordering keys does not change it, while changing a
        threshold does. This is what a replay compares against; see ADR-002.
        """
        payload = {
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "effective_until": (self.effective_until.isoformat() if self.effective_until else None),
            "requirements": [
                {
                    "id": r.id,
                    "severity": r.severity.value,
                    "applies_to": {k: sorted(map(str, v)) for k, v in sorted(r.applies_to.items())},
                    "assert": r.assertion,
                }
                for r in sorted(self.requirements, key=lambda r: r.id)
            ],
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class RuleSet(BaseModel):
    """All versions of one framework's rules, as loaded from one file."""

    model_config = ConfigDict(extra="forbid")

    framework: str
    title: str = ""
    versions: list[RuleVersion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_versions(self) -> RuleSet:
        if not self.versions:
            raise RuleFileError(f"framework {self.framework}: no versions defined")

        labels = [v.version for v in self.versions]
        duplicates = {label for label in labels if labels.count(label) > 1}
        if duplicates:
            raise RuleFileError(
                f"framework {self.framework}: duplicate version labels {sorted(duplicates)}"
            )

        for left, right in _overlapping_pairs(self.versions):
            raise RuleFileError(
                f"framework {self.framework}: versions {left.version} and {right.version} are "
                f"both in force on some dates ({left.effective_from}..{left.effective_until} "
                f"overlaps {right.effective_from}..{right.effective_until}). Which one applies "
                f"would depend on file order."
            )
        return self

    def gaps(self) -> list[tuple[date, date]]:
        """Dates between the first and last version with no version in force.

        Not an error -- a framework can genuinely lapse -- but worth reporting, because
        the usual cause is a typo in a date and the symptom is a NO_RULES verdict that
        looks like the record was simply unregulated.
        """
        ordered = sorted(self.versions, key=lambda v: v.effective_from)
        found: list[tuple[date, date]] = []
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.effective_until is None:
                continue  # open-ended; overlap validation already rejected a real clash
            if earlier.effective_until < later.effective_from:
                found.append((earlier.effective_until, later.effective_from))
        return found


def _overlapping_pairs(versions: list[RuleVersion]) -> list[tuple[RuleVersion, RuleVersion]]:
    """Every pair of versions whose in-force intervals intersect.

    Compared as half-open intervals, matching `RuleVersion.covers`. Two versions meeting
    exactly at a boundary -- one ending 2025-07-01, the next starting 2025-07-01 -- do
    not overlap, which is the whole reason the interval is half-open.
    """
    far_future = date(9999, 12, 31)
    ordered = sorted(versions, key=lambda v: (v.effective_from, v.version))
    clashes = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            left_end = left.effective_until or far_future
            right_end = right.effective_until or far_future
            if left.effective_from < right_end and right.effective_from < left_end:
                clashes.append((left, right))
    return clashes

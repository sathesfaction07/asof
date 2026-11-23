"""Domain models for compliance evaluation.

The important design decision here is the Outcome set. A naive implementation has two
values, compliant and non-compliant, which forces every awkward case into one of them.
Both directions are wrong: "we could not evaluate this" is not a pass, and "no rules were
in force" is not a failure either. See ADR-001.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    """How much a failed requirement matters.

    Ordering is meaningful, so `rank` exists. StrEnum members compare as strings, and
    "critical" < "major" alphabetically would put them in the wrong order.
    """

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    ADVISORY = "advisory"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.ADVISORY: 3,
}


class Outcome(StrEnum):
    """The verdict for one record against one resolved rule version."""

    COMPLIANT = "compliant"
    NON_COMPLIANT = "non_compliant"

    #: A requirement referenced data the record does not carry, or carried a value that
    #: could not be compared. Deliberately distinct from both pass and fail: the record
    #: might be fine, and we cannot say. Treating it as a pass hides missing data;
    #: treating it as a failure punishes records for the checker's gaps.
    INDETERMINATE = "indeterminate"

    #: No rule version was in force on the as-of date, or none of its requirements
    #: applied to this record. This exists because `all([])` is True, so the obvious
    #: implementation reports an unregulated record as COMPLIANT. See ADR-001.
    NO_RULES = "no_rules"


class Status(StrEnum):
    """The result of evaluating a single requirement."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"  # scoped out by applies_to; not a judgement
    ERROR = "error"  # could not evaluate


class Record(BaseModel):
    """A thing being checked.

    `attributes` is deliberately an open dict rather than a fixed schema. Rule sets are
    data and can reference fields this code has never heard of; requiring a model change
    to add a rule would defeat the point of versioning rules as data.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str
    category: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)

    def get(self, field: str) -> tuple[bool, Any]:
        """Return (present, value).

        Returns presence separately rather than using None as a sentinel, because a
        field explicitly set to null is different from a field that is absent, and
        rules need to be able to tell them apart.
        """
        if field == "category":
            return (bool(self.category), self.category)
        if field in self.attributes:
            return (True, self.attributes[field])
        return (False, None)


class Finding(BaseModel):
    """The outcome of one requirement against one record."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str
    title: str
    severity: Severity
    status: Status
    detail: str = ""

    @property
    def counts_against(self) -> bool:
        """Whether this finding makes the record non-compliant.

        Advisory findings are reported but do not change the verdict, which is what
        makes them useful for staging a rule before it becomes binding.
        """
        return self.status is Status.FAIL and self.severity is not Severity.ADVISORY


class CheckResult(BaseModel):
    """Everything about one check, in a form that can be written to the audit log."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    framework: str
    as_of: date

    #: The rule version actually used. None when nothing was in force.
    version: str | None = None

    #: sha256 over the canonical content of the resolved version. This is what makes a
    #: replay meaningful: if the digest differs, the rules changed and the old verdict
    #: cannot be reproduced from the current files.
    digest: str | None = None

    outcome: Outcome
    findings: list[Finding] = Field(default_factory=list)
    note: str = ""

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.counts_against]

    @property
    def worst_severity(self) -> Severity | None:
        failures = self.failures
        if not failures:
            return None
        return min((f.severity for f in failures), key=lambda s: s.rank)

    def summary(self) -> str:
        parts = [self.outcome.value]
        if self.version:
            parts.append(f"v{self.version}")
        counts = {
            "pass": sum(1 for f in self.findings if f.status is Status.PASS),
            "fail": sum(1 for f in self.findings if f.status is Status.FAIL),
            "err": sum(1 for f in self.findings if f.status is Status.ERROR),
            "skip": sum(1 for f in self.findings if f.status is Status.SKIP),
        }
        parts.extend(f"{n}{label}" for label, n in counts.items() if n)
        return " ".join(parts)


class AuditEntry(BaseModel):
    """One line of the audit log.

    Carries the resolved version and digest, not just the verdict, so a reader can tell
    whether a later re-run used the same rules. An audit log that records only outcomes
    cannot answer the one question people ask of it.
    """

    model_config = ConfigDict(extra="forbid")

    checked_at: datetime
    tool_version: str

    #: The inputs, stored so the check can actually be re-run. An audit log without the
    #: inputs can be read but not verified, which makes it a report rather than an audit.
    record: Record

    result: CheckResult

"""Resolving which rules were in force on a given date.

The whole point of this module is that "is this compliant?" is an incomplete question.
The complete question is "was this compliant under framework F according to the rules in
force on date D?", and the answer to the incomplete version silently means "as of today",
which is wrong for every record that was checked before the last rule change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from compliance_check.rules.loader import load_directory
from compliance_check.rules.schema import RuleFileError, RuleSet, RuleVersion


def today_utc() -> date:
    """The default as-of date.

    UTC rather than local. `date.today()` is the obvious choice and makes a verdict depend
    on which machine ran it: on a rule changeover day, a checker in UTC+13 and one in
    UTC-7 disagree for a 20-hour window, and neither is reproducible from the audit log
    without also recording the timezone. Fixing the reference frame is cheaper.
    """
    return datetime.now(UTC).date()


class Registry:
    """Frameworks and their dated versions."""

    def __init__(self, rule_sets: dict[str, RuleSet]) -> None:
        self._sets = {key.upper(): value for key, value in rule_sets.items()}

    @classmethod
    def from_directory(cls, directory: Path) -> Registry:
        return cls(load_directory(directory))

    def frameworks(self) -> list[str]:
        return sorted(self._sets)

    def rule_set(self, framework: str) -> RuleSet:
        try:
            return self._sets[framework.upper()]
        except KeyError:
            known = ", ".join(self.frameworks()) or "none loaded"
            raise RuleFileError(f"unknown framework {framework!r}. Available: {known}") from None

    def resolve(self, framework: str, as_of: date) -> RuleVersion | None:
        """The single version in force for `framework` on `as_of`, or None.

        Returns None when nothing is in force, which happens legitimately for a date
        before the framework existed or inside a declared lapse. None is NOT an error and
        is NOT compliance -- the caller must distinguish those, which is what
        `Outcome.NO_RULES` is for.

        A version is selected by its interval alone, never by its position in the file.
        Overlapping intervals are rejected at load time, so at most one version can match
        and this cannot depend on iteration order. The assertion below is there to make a
        violation of that invariant loud rather than arbitrary.
        """
        matches = [
            version for version in self.rule_set(framework).versions if version.covers(as_of)
        ]
        if not matches:
            return None
        if len(matches) > 1:
            labels = ", ".join(sorted(version.version for version in matches))
            raise RuleFileError(
                f"framework {framework}: {len(matches)} versions are in force on {as_of} "
                f"({labels}). Load-time overlap validation should have prevented this."
            )
        return matches[0]

    def timeline(self, framework: str) -> list[tuple[str, date, date | None, int]]:
        """(version, from, until, requirement count), ordered by start date."""
        rule_set = self.rule_set(framework)
        return [
            (v.version, v.effective_from, v.effective_until, len(v.requirements))
            for v in sorted(rule_set.versions, key=lambda v: v.effective_from)
        ]

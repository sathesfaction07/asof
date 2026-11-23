"""Applying a resolved rule version to a record.

Two things here decide whether the output is trustworthy: how a requirement decides it
does not apply, and how the per-requirement findings become one verdict. Both have an
obvious implementation that is wrong in the dangerous direction, which is towards
reporting compliance. See ADR-001.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from compliance_check.models import CheckResult, Finding, Outcome, Record, Status
from compliance_check.rules.predicates import evaluate_predicate, is_present
from compliance_check.rules.registry import Registry
from compliance_check.rules.schema import Requirement, RuleVersion


def applicability(requirement: Requirement, record: Record) -> tuple[Status | None, str]:
    """Decide whether `requirement` applies to `record`.

    Returns (None, "") when it applies and should be evaluated.
    Returns (SKIP, reason) when it definitely does not apply.
    Returns (ERROR, reason) when that cannot be determined.

    The third case is the one worth arguing about. A requirement scoped to
    `category: [toys]` against a record with no category could be skipped, and skipping
    reads as "not regulated" -- a pass. We do not know that. An unknown scope produces an
    ERROR that surfaces as INDETERMINATE, so the record lands in a human's queue instead
    of a compliant report.
    """
    for field, accepted in requirement.applies_to.items():
        present, value = record.get(field)
        if not present or not is_present(value):
            return (
                Status.ERROR,
                f"cannot tell whether this applies: {field} is missing "
                f"(scoped to {_render(accepted)})",
            )
        if not _scope_matches(value, accepted):
            return (Status.SKIP, f"does not apply: {field}={value!r} is not {_render(accepted)}")
    return (None, "")


def _scope_matches(value: Any, accepted: list[Any]) -> bool:
    """Scope comparison is case-insensitive for strings.

    A record saying `Toys` and a rule saying `toys` are talking about the same category.
    Being strict here would silently narrow a rule's reach, which is the same failure as
    not having the rule.
    """
    for candidate in accepted:
        if isinstance(value, str) and isinstance(candidate, str):
            if value.strip().casefold() == candidate.strip().casefold():
                return True
        elif value == candidate:
            return True
    return False


def _render(accepted: list[Any]) -> str:
    return " or ".join(repr(item) for item in accepted)


def evaluate_version(
    record: Record, version: RuleVersion, framework: str, as_of: date
) -> CheckResult:
    """Check one record against one resolved version."""
    findings: list[Finding] = []

    for requirement in version.requirements:
        scope_status, scope_detail = applicability(requirement, record)
        if scope_status is not None:
            findings.append(
                Finding(
                    requirement_id=requirement.id,
                    title=requirement.title,
                    severity=requirement.severity,
                    status=scope_status,
                    detail=scope_detail,
                )
            )
            continue

        status, detail = evaluate_predicate(requirement.assertion, record)
        findings.append(
            Finding(
                requirement_id=requirement.id,
                title=requirement.title,
                severity=requirement.severity,
                status=status,
                detail=detail,
            )
        )

    outcome, note = derive_outcome(findings)
    return CheckResult(
        record_id=record.record_id,
        framework=framework,
        as_of=as_of,
        version=version.version,
        digest=version.digest,
        outcome=outcome,
        findings=findings,
        note=note,
    )


def derive_outcome(findings: list[Finding]) -> tuple[Outcome, str]:
    """Turn per-requirement findings into one verdict.

    The precedence is deliberate and each step has a reason:

    1. Nothing was actually judged -> NO_RULES. Not COMPLIANT. `all([])` is True, so the
       obvious formulation ("compliant if no failures") reports a record that was never
       examined as passing. That is the single most dangerous bug this tool could have.
    2. A requirement definitively failed -> NON_COMPLIANT. A known violation outranks an
       unknown; there is no point saying "indeterminate" about a record already proven bad.
    3. Something could not be evaluated -> INDETERMINATE. The record might be fine. We
       cannot say, and saying nothing is more useful than guessing.
    4. Otherwise -> COMPLIANT, meaning every applicable requirement was checked and passed.
    """
    judged = [f for f in findings if f.status is not Status.SKIP]
    if not judged:
        skipped = len(findings)
        if skipped:
            return (
                Outcome.NO_RULES,
                f"all {skipped} requirement(s) were scoped out; nothing was checked",
            )
        return (Outcome.NO_RULES, "the version in force has no requirements")

    if any(f.counts_against for f in judged):
        return (Outcome.NON_COMPLIANT, "")

    errored = [f for f in judged if f.status is Status.ERROR]
    if errored:
        return (
            Outcome.INDETERMINATE,
            f"{len(errored)} requirement(s) could not be evaluated",
        )

    advisory = [f for f in judged if f.status is Status.FAIL]
    if advisory:
        return (
            Outcome.COMPLIANT,
            f"{len(advisory)} advisory finding(s) did not affect the verdict",
        )
    return (Outcome.COMPLIANT, "")


def check(
    record: Record,
    registry: Registry,
    framework: str,
    as_of: date,
) -> CheckResult:
    """Resolve the applicable version and evaluate, or report that none applied."""
    version = registry.resolve(framework, as_of)
    if version is None:
        timeline = registry.timeline(framework)
        spans = ", ".join(
            f"{label} from {start}" + (f" until {end}" if end else " onwards")
            for label, start, end, _ in timeline
        )
        return CheckResult(
            record_id=record.record_id,
            framework=framework,
            as_of=as_of,
            outcome=Outcome.NO_RULES,
            note=f"no version of {framework} was in force on {as_of}. Known: {spans}",
        )
    return evaluate_version(record, version, framework, as_of)


def check_all(
    records: list[Record],
    registry: Registry,
    framework: str,
    as_of: date,
) -> list[CheckResult]:
    return [check(record, registry, framework, as_of) for record in records]

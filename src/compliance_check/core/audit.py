"""The audit log, and replaying it.

An audit log that records only verdicts cannot answer the question people actually ask of
it, which is "would we get the same answer today, and if not, why not?". So each entry
carries the inputs, the resolved version label, and a content digest of that version.

That makes the log a regression corpus as well as a record. Replaying it distinguishes
three situations that look identical if you only store outcomes:

  the rules changed        -- expected, and the old verdict stands as history
  the tool changed         -- a behaviour change in this codebase, possibly a bug
  nothing changed          -- reproduced

Only the second is a defect, and it is invisible without the digest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from compliance_check import __version__
from compliance_check.core.evaluate import check
from compliance_check.models import AuditEntry, CheckResult, Record
from compliance_check.rules.registry import Registry
from compliance_check.rules.schema import RuleVersion


class ReplayStatus(StrEnum):
    REPRODUCED = "reproduced"

    #: The rules that cover this date are no longer the same ones. Not a defect; the
    #: recorded verdict remains the truth about what was decided at the time.
    RULES_CHANGED = "rules_changed"

    #: Same version, same digest, different verdict. The rules did not move, so this
    #: codebase did. This is the case worth failing a build over.
    DIVERGED = "diverged"

    #: No version covers that date any more, so there is nothing to replay against.
    NO_LONGER_COVERED = "no_longer_covered"


class Replay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    status: ReplayStatus
    recorded: CheckResult
    current: CheckResult | None = None
    detail: str = ""

    @property
    def is_defect(self) -> bool:
        return self.status is ReplayStatus.DIVERGED


def append(path: Path, result: CheckResult, record: Record) -> AuditEntry:
    """Append one entry. Creates the file and parent directory if needed.

    JSON Lines, opened in append mode, one entry per line. Append-only is the point: an
    audit log you can rewrite is a report. Nothing here reads the existing file, so two
    processes writing concurrently interleave whole lines rather than corrupting each
    other's -- which is the practical reason for the format over a JSON array.
    """
    entry = AuditEntry(
        checked_at=datetime.now(UTC),
        tool_version=__version__,
        record=record,
        result=result,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.model_dump_json() + "\n")
    return entry


def read(path: Path) -> tuple[list[AuditEntry], list[str]]:
    """Read the log, returning (entries, problems).

    A malformed line is reported rather than raised on, because one corrupt line should
    not make the other ten thousand entries unreadable. It is also not silently skipped:
    a log with unexplained gaps is worse than one with a stated gap.
    """
    if not path.exists():
        return ([], [f"{path} does not exist"])

    entries: list[AuditEntry] = []
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(AuditEntry.model_validate_json(line))
        except ValidationError as exc:
            problems.append(
                f"line {number}: does not match the audit schema ({exc.error_count()} problems)"
            )
        except json.JSONDecodeError as exc:
            problems.append(f"line {number}: not valid JSON ({exc.msg})")
    return (entries, problems)


def replay_entry(entry: AuditEntry, registry: Registry) -> Replay:
    """Re-run one recorded check against the current rules.

    The re-run goes through `check()`, the same entry point a real check uses, rather than
    reaching straight for `evaluate_version`. That matters for a regression gate: replaying
    through a different path would leave the resolution step untested, which is where the
    interesting behaviour lives.
    """
    recorded = entry.result
    current = check(entry.record, registry, recorded.framework, recorded.as_of)
    version = registry.resolve(recorded.framework, recorded.as_of)

    was_covered = recorded.version is not None
    is_covered = version is not None

    # Coverage itself changed. Checked before anything else, because comparing version
    # labels when one side is absent produces a comparison of a label against None.
    if was_covered and not is_covered:
        return Replay(
            record_id=recorded.record_id,
            status=ReplayStatus.NO_LONGER_COVERED,
            recorded=recorded,
            current=current,
            detail=(
                f"no version of {recorded.framework} now covers {recorded.as_of}; "
                f"the check used v{recorded.version}"
            ),
        )
    if is_covered and not was_covered:
        return Replay(
            record_id=recorded.record_id,
            status=ReplayStatus.RULES_CHANGED,
            recorded=recorded,
            current=current,
            detail=(
                f"nothing was in force on {recorded.as_of} when this was checked; "
                f"it is now covered by v{version.version}"
            ),
        )

    if is_covered:
        assert version is not None  # narrowed by is_covered; kept for the type checker
        if version.version != recorded.version:
            return Replay(
                record_id=recorded.record_id,
                status=ReplayStatus.RULES_CHANGED,
                recorded=recorded,
                current=current,
                detail=(
                    f"{recorded.as_of} is now covered by v{version.version}, "
                    f"not v{recorded.version}"
                ),
            )
        if version.digest != recorded.digest:
            return Replay(
                record_id=recorded.record_id,
                status=ReplayStatus.RULES_CHANGED,
                recorded=recorded,
                current=current,
                detail=(
                    f"v{version.version} was edited in place after being used for checks: "
                    f"content changed {recorded.digest} -> {version.digest}, and it still "
                    f"covers {recorded.as_of}"
                ),
            )

    # From here the rules are identical -- including being identically absent, which is
    # the case the first version of this function got wrong. It reported every check made
    # during a coverage gap as `no_longer_covered`, so replaying an untouched rule set
    # produced 112 false alarms out of 480 entries. A gate that cries wolf 23% of the time
    # is a gate nobody reads.
    if current.outcome is not recorded.outcome:
        return Replay(
            record_id=recorded.record_id,
            status=ReplayStatus.DIVERGED,
            recorded=recorded,
            current=current,
            detail=(
                f"identical rules ({_describe(version)}) now yield {current.outcome.value} "
                f"instead of {recorded.outcome.value}. Recorded by tool "
                f"{entry.tool_version}, replayed by {__version__}"
            ),
        )

    changed = _finding_differences(recorded, current)
    if changed:
        return Replay(
            record_id=recorded.record_id,
            status=ReplayStatus.DIVERGED,
            recorded=recorded,
            current=current,
            detail=(
                f"the verdict matches but {len(changed)} finding(s) differ: "
                + "; ".join(changed[:3])
            ),
        )

    return Replay(
        record_id=recorded.record_id,
        status=ReplayStatus.REPRODUCED,
        recorded=recorded,
        current=current,
    )


def _describe(version: RuleVersion | None) -> str:
    """How to name the rules in a message when there may not be any."""
    if version is None:
        return "no version in force, then or now"
    return f"v{version.version} {version.digest}"


def _finding_differences(recorded: CheckResult, current: CheckResult) -> list[str]:
    """Per-requirement status changes, compared by id rather than by position.

    Comparing by position would report a re-ordered rule file as a divergence. Comparing
    by id means a genuinely re-ordered file replays clean, and a requirement that was
    added or removed is named.
    """
    before = {f.requirement_id: f.status for f in recorded.findings}
    after = {f.requirement_id: f.status for f in current.findings}
    differences = []
    for requirement_id in sorted(set(before) | set(after)):
        was, now = before.get(requirement_id), after.get(requirement_id)
        if was != now:
            shown_was = was.value if was else "absent"
            shown_now = now.value if now else "absent"
            differences.append(f"{requirement_id} {shown_was} -> {shown_now}")
    return differences


def replay_all(path: Path, registry: Registry) -> tuple[list[Replay], list[str]]:
    entries, problems = read(path)
    return ([replay_entry(entry, registry) for entry in entries], problems)

"""Command line interface."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import click

from compliance_check import __version__
from compliance_check.config import Settings
from compliance_check.core import audit
from compliance_check.core.evaluate import check
from compliance_check.io import RecordFileError, load_records, summarise, write_results
from compliance_check.models import CheckResult, Outcome, Record, Status
from compliance_check.rules.predicates import ALL_OPS, BINARY_OPS, COMPOSITE_OPS, UNARY_OPS
from compliance_check.rules.registry import Registry, today_utc
from compliance_check.rules.schema import RuleFileError

OUTCOME_COLOUR = {
    Outcome.COMPLIANT: "green",
    Outcome.NON_COMPLIANT: "red",
    Outcome.INDETERMINATE: "yellow",
    Outcome.NO_RULES: "magenta",
}

STATUS_COLOUR = {
    Status.PASS: "green",
    Status.FAIL: "red",
    Status.ERROR: "yellow",
    Status.SKIP: "bright_black",
}


def _settings(**overrides: object) -> Settings:
    # Click passes None for every option the user did not give. Dropping them lets the
    # environment and .env supply the value instead of being overwritten with None.
    return Settings(**{key: value for key, value in overrides.items() if value is not None})


def _load_registry(rules_dir: Path) -> Registry:
    try:
        return Registry.from_directory(rules_dir)
    except RuleFileError as exc:
        raise click.ClickException(str(exc)) from exc


def _parse_as_of(value: str | None) -> date:
    if value is None:
        return today_utc()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(f"{value!r} is not an ISO date (expected YYYY-MM-DD)") from None


@click.group()
@click.version_option(__version__, prog_name="asof")
def main() -> None:
    """Check records against versioned, dated rule sets.

    Every verdict is tied to a framework, an as-of date, and a content digest of the rules
    that produced it, so it can be reproduced later or shown to have changed.
    """


@main.command()
@click.option(
    "-i", "--input", "input_path", type=click.Path(path_type=Path), help="CSV or JSON of records."
)
@click.option("-f", "--framework", help="Framework to check against. Default from CC_FRAMEWORK.")
@click.option(
    "--as-of", help="Evaluate against the rules in force on this ISO date. Default: today (UTC)."
)
@click.option("-r", "--rules-dir", type=click.Path(path_type=Path), help="Directory of rule files.")
@click.option(
    "-o", "--output", "output_dir", type=click.Path(path_type=Path), help="Results directory."
)
@click.option(
    "--format", "fmt", type=click.Choice(["csv", "json", "excel"]), help="Tabular output format."
)
@click.option(
    "--audit-log", type=click.Path(path_type=Path), help="Append every check to this JSONL file."
)
@click.option("--no-audit", is_flag=True, help="Do not write to the audit log.")
@click.option(
    "--attention-only",
    is_flag=True,
    help="Narrow the tabular output. The JSON report keeps everything.",
)
@click.option("--details/--no-details", default=True, help="Show per-requirement findings.")
def run(
    input_path: Path | None,
    framework: str | None,
    as_of: str | None,
    rules_dir: Path | None,
    output_dir: Path | None,
    fmt: str | None,
    audit_log: Path | None,
    no_audit: bool,
    attention_only: bool,
    details: bool,
) -> None:
    """Check records from a file.

    Exits non-zero if anything is not cleanly compliant, so this composes into a pipeline
    step. NO_RULES and INDETERMINATE count as "not compliant" for that purpose: neither is
    a pass, and treating them as one is how an unchecked record reaches a customer.
    """
    settings = _settings(
        rules_dir=rules_dir,
        framework=framework,
        output_dir=output_dir,
        output_format=fmt,
        audit_log=audit_log,
    )
    when = _parse_as_of(as_of)
    registry = _load_registry(settings.rules_dir)

    if input_path is None:
        raise click.UsageError("give a record file with --input")
    try:
        records = load_records(input_path)
    except RecordFileError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        results = [check(record, registry, settings.framework, when) for record in records]
    except RuleFileError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"framework      {settings.framework}")
    click.echo(f"as of          {when.isoformat()}")
    resolved = registry.resolve(settings.framework, when)
    if resolved:
        click.echo(f"rules          v{resolved.version}  {resolved.digest}")
    else:
        click.secho(f"rules          none in force on {when}", fg="magenta")
    click.echo("")

    for result, record in zip(results, records, strict=True):
        colour = OUTCOME_COLOUR[result.outcome]
        click.echo(f"  {result.record_id:<12} ", nl=False)
        click.secho(f"{result.summary():<40}", fg=colour, nl=False)
        click.echo(result.note[:60])
        if details:
            _show_findings(result)
        if not no_audit:
            audit.append(settings.audit_log, result, record)

    click.echo("")
    for label, count in summarise(results).items():
        if count or label in ("total", "needs_attention"):
            click.echo(f"  {label:<16}{count}")

    shown = [r for r in results if r.outcome is not Outcome.COMPLIANT] if attention_only else None
    written = write_results(results, settings.output_dir, settings.output_format, emit=shown)
    click.echo("")
    for path in written:
        click.echo(f"  wrote {path}")
    if not no_audit:
        click.echo(f"  logged {len(results)} check(s) to {settings.audit_log}")

    if any(result.outcome is not Outcome.COMPLIANT for result in results):
        sys.exit(1)


def _show_findings(result: CheckResult) -> None:
    for finding in result.findings:
        if finding.status is Status.SKIP:
            continue
        mark = (
            "advisory"
            if finding.severity.value == "advisory" and finding.status is Status.FAIL
            else finding.status.value
        )
        click.echo("      ", nl=False)
        click.secho(f"{mark:<9}", fg=STATUS_COLOUR[finding.status], nl=False)
        click.echo(f"{finding.requirement_id:<14}{finding.detail[:70]}")


@main.command()
@click.argument("record_id")
@click.option("--attr", "-a", multiple=True, help="field=value, repeatable.")
@click.option("--category", default="", help="The record's category, used by scoped rules.")
@click.option("-f", "--framework", help="Framework to check against.")
@click.option("--as-of", help="ISO date. Default: today (UTC).")
@click.option("-r", "--rules-dir", type=click.Path(path_type=Path), help="Directory of rule files.")
def one(
    record_id: str,
    attr: tuple[str, ...],
    category: str,
    framework: str | None,
    as_of: str | None,
    rules_dir: Path | None,
) -> None:
    """Check a single record given on the command line.

    Nothing is written to the audit log: this is for exploring what a rule does, and an
    audit log full of experiments is harder to trust than one that only holds real checks.
    """
    settings = _settings(rules_dir=rules_dir, framework=framework)
    when = _parse_as_of(as_of)
    registry = _load_registry(settings.rules_dir)

    attributes: dict[str, object] = {}
    for item in attr:
        if "=" not in item:
            raise click.BadParameter(f"{item!r} should look like field=value")
        key, _, value = item.partition("=")
        attributes[key.strip()] = value.strip()

    record = Record(record_id=record_id, category=category, attributes=attributes)
    try:
        result = check(record, registry, settings.framework, when)
    except RuleFileError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"record    {result.record_id}")
    click.echo(f"framework {result.framework}   as of {result.as_of.isoformat()}")
    if result.version:
        click.echo(f"rules     v{result.version}  {result.digest}")
    click.echo("outcome   ", nl=False)
    click.secho(result.outcome.value, fg=OUTCOME_COLOUR[result.outcome], bold=True)
    if result.note:
        click.echo(f"note      {result.note}")
    click.echo("")
    for finding in result.findings:
        click.echo("  ", nl=False)
        click.secho(f"{finding.status.value:<7}", fg=STATUS_COLOUR[finding.status], nl=False)
        click.secho(f"{finding.severity.value:<9}", fg="bright_black", nl=False)
        click.echo(f"{finding.requirement_id:<14}{finding.detail[:64]}")

    if result.outcome is not Outcome.COMPLIANT:
        sys.exit(1)


@main.command()
@click.option("-r", "--rules-dir", type=click.Path(path_type=Path), help="Directory of rule files.")
def frameworks(rules_dir: Path | None) -> None:
    """List the loaded frameworks and their version timelines."""
    settings = _settings(rules_dir=rules_dir)
    registry = _load_registry(settings.rules_dir)

    for name in registry.frameworks():
        rule_set = registry.rule_set(name)
        click.secho(f"{name}", fg="cyan", bold=True, nl=False)
        click.echo(f"  {rule_set.title}")
        for label, start, end, count in registry.timeline(name):
            version = rule_set.versions[[v.version for v in rule_set.versions].index(label)]
            span = (
                f"{start.isoformat()} -> {end.isoformat()}"
                if end
                else f"{start.isoformat()} -> open"
            )
            in_force = " (in force today)" if version.covers(today_utc()) else ""
            click.echo(
                f"    v{label:<9}{span:<26}{count:>2} requirement(s)  {version.digest}{in_force}"
            )
        for gap_start, gap_end in rule_set.gaps():
            click.secho(
                f"    gap        {gap_start.isoformat()} -> {gap_end.isoformat()}"
                "   nothing in force; checks in this window return no_rules",
                fg="magenta",
            )
        click.echo("")


@main.command()
@click.option("--audit-log", type=click.Path(path_type=Path), help="JSONL audit log to replay.")
@click.option("-r", "--rules-dir", type=click.Path(path_type=Path), help="Directory of rule files.")
@click.option("--quiet", is_flag=True, help="Only show entries that did not reproduce.")
def replay(audit_log: Path | None, rules_dir: Path | None, quiet: bool) -> None:
    """Re-run every logged check against the current rules.

    Distinguishes "the rules changed" (expected) from "the same rules now give a different
    answer" (a defect in this tool). Exits non-zero only for the second, so this can run in
    CI as a semantic regression check without failing every time a regulation is updated.
    """
    settings = _settings(rules_dir=rules_dir, audit_log=audit_log)
    registry = _load_registry(settings.rules_dir)

    replays, problems = audit.replay_all(settings.audit_log, registry)
    for problem in problems:
        click.secho(f"  {problem}", fg="yellow")
    if not replays:
        click.echo("nothing to replay")
        return

    counts: dict[str, int] = {}
    for item in replays:
        counts[item.status.value] = counts.get(item.status.value, 0) + 1
        if quiet and item.status is audit.ReplayStatus.REPRODUCED:
            continue
        colour = {
            audit.ReplayStatus.REPRODUCED: "green",
            audit.ReplayStatus.RULES_CHANGED: "cyan",
            audit.ReplayStatus.DIVERGED: "red",
            audit.ReplayStatus.NO_LONGER_COVERED: "magenta",
        }[item.status]
        click.echo(f"  {item.record_id:<12}", nl=False)
        click.secho(f"{item.status.value:<20}", fg=colour, nl=False)
        click.echo(item.detail[:80])

    click.echo("")
    for label, count in sorted(counts.items()):
        click.echo(f"  {label:<20}{count}")

    defects = [item for item in replays if item.is_defect]
    if defects:
        click.echo("")
        click.secho(
            f"{len(defects)} check(s) changed verdict under identical rules. "
            "The rules did not move, so this codebase did.",
            fg="red",
            bold=True,
        )
        sys.exit(1)


@main.command()
@click.option("-r", "--rules-dir", type=click.Path(path_type=Path), help="Directory of rule files.")
def doctor(rules_dir: Path | None) -> None:
    """Show configuration, the rule language, and how outcomes are decided."""
    settings = _settings(rules_dir=rules_dir)

    click.secho("configuration", fg="cyan", bold=True)
    click.echo(f"  rules_dir     {settings.rules_dir}")
    click.echo(f"  framework     {settings.framework}")
    click.echo(f"  audit_log     {settings.audit_log}")
    click.echo(f"  output_dir    {settings.output_dir}  ({settings.output_format})")
    click.echo(f"  today (UTC)   {today_utc().isoformat()}")

    click.echo("")
    click.secho("rule files", fg="cyan", bold=True)
    try:
        registry = Registry.from_directory(settings.rules_dir)
    except RuleFileError as exc:
        click.secho(f"  not loadable: {exc}", fg="red")
    else:
        for name in registry.frameworks():
            rule_set = registry.rule_set(name)
            current = registry.resolve(name, today_utc())
            state = f"v{current.version} in force" if current else "nothing in force today"
            click.echo(f"  {name:<6}{len(rule_set.versions)} version(s), {state}")

    click.echo("")
    click.secho("operators", fg="cyan", bold=True)
    click.echo(f"  no value     {', '.join(sorted(UNARY_OPS))}")
    click.echo(f"  with value   {', '.join(sorted(BINARY_OPS))}")
    click.echo(f"  composite    {', '.join(sorted(COMPOSITE_OPS))}  (take 'of', not 'field')")
    click.echo(f"  {len(ALL_OPS)} operators. Anything else is rejected when the rule file loads.")

    click.echo("")
    click.secho("outcomes", fg="cyan", bold=True)
    for outcome, meaning in (
        (Outcome.COMPLIANT, "every applicable requirement was checked and passed"),
        (Outcome.NON_COMPLIANT, "at least one non-advisory requirement failed"),
        (Outcome.INDETERMINATE, "a requirement could not be evaluated; nothing failed outright"),
        (Outcome.NO_RULES, "nothing was checked: no version in force, or all scoped out"),
    ):
        click.secho(f"  {outcome.value:<16}", fg=OUTCOME_COLOUR[outcome], nl=False)
        click.echo(meaning)
    click.echo("")
    click.echo("  no_rules is NOT compliance. An unchecked record has not passed.")
    click.echo("  A missing field yields error, not fail: 'not declared' and 'declared wrongly'")
    click.echo("  are different problems for whoever has to fix them.")

    click.echo("")
    click.secho("audit log", fg="cyan", bold=True)
    entries, problems = audit.read(settings.audit_log)
    if problems:
        for problem in problems:
            click.echo(f"  {problem}")
    if entries:
        first = min(entry.checked_at for entry in entries)
        last = max(entry.checked_at for entry in entries)
        click.echo(f"  {len(entries)} entr(y/ies), {first:%Y-%m-%d} to {last:%Y-%m-%d}")
        click.echo("  replay re-runs these against the current rules; see `replay --help`")


if __name__ == "__main__":  # pragma: no cover
    main()

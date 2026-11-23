"""Loading rule files from disk.

One file per framework. Failures here are loud and name the file, because a rule set that
loads wrong is worse than one that does not load at all -- the first produces confident
verdicts from the wrong rules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from compliance_check.rules.schema import RuleFileError, RuleSet


def load_rule_file(path: Path) -> RuleSet:
    """Parse and validate one framework's rule file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuleFileError(f"{path.name}: cannot read ({exc})") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuleFileError(f"{path.name}: invalid YAML ({exc})") from exc

    if data is None:
        raise RuleFileError(f"{path.name}: file is empty")
    if not isinstance(data, dict):
        raise RuleFileError(f"{path.name}: top level must be a mapping, got {type(data).__name__}")

    return _build(data, path.name)


def _build(data: dict[str, Any], where: str) -> RuleSet:
    """Turn validated-shaped data into a RuleSet, attributing errors to the file.

    Pydantic's own errors are re-wrapped rather than propagated. A raw ValidationError
    mentions `versions.1.requirements.3.assert` which is accurate and unhelpful; the file
    name and the requirement id are what a rule author needs.
    """
    try:
        return RuleSet.model_validate(data)
    except RuleFileError as exc:
        raise RuleFileError(f"{where}: {exc}") from exc
    except ValidationError as exc:
        # A RuleFileError raised inside a validator arrives wrapped, so unwrap it back out
        # rather than showing the reader Pydantic's framing of our own message.
        for error in exc.errors():
            cause = error.get("ctx", {}).get("error")
            if isinstance(cause, RuleFileError):
                raise RuleFileError(f"{where}: {cause}") from exc
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:4]
        )
        raise RuleFileError(f"{where}: {problems}") from exc


def load_directory(directory: Path) -> dict[str, RuleSet]:
    """Load every `.yaml` / `.yml` file in a directory, keyed by framework.

    Sorted so that a duplicate-framework error names the same pair of files on every
    machine, rather than depending on filesystem order.
    """
    if not directory.is_dir():
        raise RuleFileError(f"{directory} is not a directory")

    paths = sorted(
        path for path in directory.iterdir() if path.suffix in (".yaml", ".yml") and path.is_file()
    )
    if not paths:
        raise RuleFileError(f"{directory} contains no .yaml or .yml rule files")

    sets: dict[str, RuleSet] = {}
    origins: dict[str, str] = {}
    for path in paths:
        rule_set = load_rule_file(path)
        key = rule_set.framework.upper()
        if key in sets:
            raise RuleFileError(
                f"framework {rule_set.framework!r} is declared in both {origins[key]} "
                f"and {path.name}"
            )
        sets[key] = rule_set
        origins[key] = path.name
    return sets

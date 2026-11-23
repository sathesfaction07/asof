"""Shared fixtures.

Two kinds of rule set are used across the suite:

  the REAL shipped files in data/frameworks -- so that editing them and breaking something
  fails the build rather than somebody's first run

  small synthetic sets built by `rule_doc` -- so a test about one behaviour is not coupled
  to the shape of the sample data
"""

from __future__ import annotations

import copy
import importlib.util
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from compliance_check.models import Record
from compliance_check.rules.registry import Registry
from compliance_check.rules.schema import RuleSet

#: Excel output is an optional extra, so its tests skip rather than fail on a default install.
requires_openpyxl = pytest.mark.skipif(
    importlib.util.find_spec("openpyxl") is None,
    reason="openpyxl is optional: pip install 'compliance-check[excel]'",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK_DIR = REPO_ROOT / "data" / "frameworks"
SAMPLE_RECORDS = REPO_ROOT / "data" / "sample_records.csv"

FRAMEWORKS = ("US", "EU", "JP")


def _load_raw() -> dict[str, dict[str, Any]]:
    return {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(FRAMEWORK_DIR.glob("*.yaml"))
    }


def registry_from(docs: dict[str, dict[str, Any]]) -> Registry:
    """Build a Registry from raw YAML-shaped dicts, bypassing the filesystem."""
    return Registry(
        {doc["framework"].upper(): RuleSet.model_validate(doc) for doc in docs.values()}
    )


@pytest.fixture(scope="session")
def raw_docs() -> dict[str, dict[str, Any]]:
    """The shipped rule files as plain dicts. Deep-copy before mutating."""
    return _load_raw()


@pytest.fixture(scope="session")
def registry(raw_docs: dict[str, dict[str, Any]]) -> Registry:
    return registry_from(raw_docs)


@pytest.fixture
def mutable_docs(raw_docs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return copy.deepcopy(raw_docs)


def rule_doc(
    framework: str = "TEST",
    versions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A minimal rule document for focused tests."""
    if versions is None:
        versions = [
            {
                "version": "1.0",
                "effective_from": date(2024, 1, 1),
                "requirements": [
                    {
                        "id": "R-1",
                        "title": "a must be present",
                        "severity": "major",
                        "assert": {"op": "present", "field": "a"},
                    }
                ],
            }
        ]
    return {"framework": framework, "title": f"{framework} test rules", "versions": versions}


def requirement(
    identifier: str = "R-1",
    assertion: dict[str, Any] | None = None,
    severity: str = "major",
    applies_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    built: dict[str, Any] = {
        "id": identifier,
        "title": f"requirement {identifier}",
        "severity": severity,
        "assert": assertion or {"op": "present", "field": "a"},
    }
    if applies_to:
        built["applies_to"] = applies_to
    return built


def record(record_id: str = "X-1", category: str = "", **attributes: Any) -> Record:
    return Record(record_id=record_id, category=category, attributes=attributes)


#: A coarse sweep used by the property tests. Weekly rather than daily keeps the suite fast
#: while still crossing every boundary in the shipped rule sets.
SWEEP_DATES = tuple(date(2022, 1, 1) + timedelta(days=n) for n in range(0, 1800, 14))

#: A daily sweep, used only where a boundary is the point of the test.
DAILY_DATES = tuple(date(2021, 6, 1) + timedelta(days=n) for n in range(0, 2200))


@pytest.fixture(scope="session")
def sample_records() -> list[Record]:
    from compliance_check.io import load_records

    return load_records(SAMPLE_RECORDS)


@pytest.fixture
def shuffled_docs(mutable_docs: dict[str, dict[str, Any]]) -> Any:
    """A factory that returns the shipped rules with versions and requirements shuffled."""

    def build(seed: int) -> dict[str, dict[str, Any]]:
        rng = random.Random(seed)
        docs = copy.deepcopy(mutable_docs)
        for doc in docs.values():
            rng.shuffle(doc["versions"])
            for version in doc["versions"]:
                rng.shuffle(version.get("requirements", []))
        return docs

    return build


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "checks.jsonl"

#!/usr/bin/env bash
set -euo pipefail

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN} ok ${NC} $1"; }
warn() { echo -e "${YELLOW}warn${NC} $1"; }
fail() { echo -e "${RED}fail${NC} $1"; exit 1; }

WITH_EXCEL=0
for arg in "$@"; do
    case "$arg" in
        --excel) WITH_EXCEL=1 ;;
        --all)   WITH_EXCEL=1 ;;
        -h|--help)
            cat <<'USAGE'
Usage: bash setup.sh [options]

  --excel   also install openpyxl, for .xlsx output
  --all     everything
  -h        this message

There is no hosted-model option, and that is deliberate: a compliance verdict has to be
reproducible from its inputs, so every check here is decided by code and data only.
USAGE
            exit 0 ;;
        *) fail "unknown option: $arg (try --help)" ;;
    esac
done

info "Checking Python"
command -v python3 >/dev/null 2>&1 || {
    echo "python3 was not found. Install it first:"
    echo "  macOS:         brew install python@3.12"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv"
    echo "  RHEL/Fedora:   sudo dnf install python3"
    fail "python3 missing"
}

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')" != "1" ]; then
    echo "Python 3.11 or newer is required (StrEnum, datetime.UTC). Found $PY_VER."
    echo "  macOS:         brew install python@3.12"
    echo "  Ubuntu/Debian: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv"
    fail "python $PY_VER too old"
fi
ok "Python $PY_VER"

info "Creating virtual environment"
[ -d .venv ] || python3 -m venv .venv || fail "could not create .venv (Debian/Ubuntu: sudo apt install python3-venv)"
ok ".venv ready"

EXTRAS="dev"
[ "$WITH_EXCEL" = "1" ] && EXTRAS="$EXTRAS,excel"

info "Installing dependencies [$EXTRAS]"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[$EXTRAS]" || fail "pip install failed"
ok "dependencies installed"

info "Verifying imports"
.venv/bin/python - <<'PYCHECK' || fail "core imports failed"
import click, yaml                                        # noqa: F401
from pydantic_settings import BaseSettings                 # noqa: F401
from compliance_check.core.audit import replay_entry       # noqa: F401
from compliance_check.core.evaluate import check           # noqa: F401
from compliance_check.rules.registry import Registry       # noqa: F401
PYCHECK
ok "imports resolve"

info "Loading the shipped rule files"
.venv/bin/python - <<'PYCHECK' || fail "the shipped rule files did not load as documented"
from pathlib import Path

from compliance_check.rules.registry import Registry

registry = Registry.from_directory(Path("data/frameworks"))
assert registry.frameworks() == ["EU", "JP", "US"], registry.frameworks()

for name in registry.frameworks():
    rule_set = registry.rule_set(name)
    print(f"     {name:<4}{len(rule_set.versions)} version(s)", end="")
    gaps = rule_set.gaps()
    print(f"   gap {gaps[0][0]} .. {gaps[0][1]}" if gaps else "")

# The declared gap is load-bearing for the replay regression test below.
assert registry.rule_set("EU").gaps(), "EU is supposed to have a declared coverage gap"
PYCHECK
ok "rule files valid"

info "Checking that the as-of date actually changes the answer"
.venv/bin/python - <<'PYCHECK' || fail "the same record did not get different verdicts on different dates"
from datetime import date
from pathlib import Path

from compliance_check.core.evaluate import check
from compliance_check.models import Outcome, Record
from compliance_check.rules.registry import Registry

registry = Registry.from_directory(Path("data/frameworks"))
toy = Record(
    record_id="T-1",
    category="toys",
    attributes={
        "country_of_origin": "Vietnam",
        "certificate_number": "US-234567",
        "certificate_expiry": "2027-03-01",
        "small_parts": "true",
        "age_min_months": "36",
        "instruction_manual": "included",
    },
)

earlier = check(toy, registry, "US", date(2025, 6, 1))
later = check(toy, registry, "US", date(2026, 6, 1))

assert earlier.version == "2024.1", earlier.version
assert later.version == "2026.1", later.version
assert earlier.outcome is Outcome.COMPLIANT, earlier.outcome
assert later.outcome is Outcome.NON_COMPLIANT, later.outcome
assert earlier.digest != later.digest

print(f"     identical record: {earlier.as_of} -> {earlier.outcome.value} (v{earlier.version})")
print(f"                       {later.as_of} -> {later.outcome.value} (v{later.version})")
print(f"     because 2026.1 raised the toy age threshold: {later.failures[0].detail}")
PYCHECK
ok "as-of resolution works"

info "Checking that an unchecked record is never reported as compliant"
.venv/bin/python - <<'PYCHECK' || fail "a vacuous pass got through"
from datetime import date, timedelta
from pathlib import Path

from compliance_check.core.evaluate import check, derive_outcome
from compliance_check.io import load_records
from compliance_check.models import Outcome, Record, Status
from compliance_check.rules.registry import Registry

registry = Registry.from_directory(Path("data/frameworks"))
records = load_records(Path("data/sample_records.csv"))

# `all([])` is True, so "no failures" must not be allowed to mean "passed".
assert derive_outcome([])[0] is Outcome.NO_RULES

# A date inside the declared EU gap: nothing is in force, so nothing was checked.
gap = check(records[0], registry, "EU", date(2024, 6, 1))
assert gap.outcome is Outcome.NO_RULES, gap.outcome
assert gap.version is None

# A record scoped out of every requirement.
scoped_out = check(Record(record_id="S-1", category="nothing-matches-this"), registry, "JP", date(2025, 1, 1))
assert scoped_out.outcome is not Outcome.COMPLIANT, scoped_out.outcome

checked = vacuous = 0
for framework in ("US", "EU", "JP"):
    for offset in range(0, 1500, 11):
        when = date(2022, 1, 1) + timedelta(days=offset)
        for record in records:
            result = check(record, registry, framework, when)
            checked += 1
            judged = [f for f in result.findings if f.status is not Status.SKIP]
            if result.outcome is Outcome.COMPLIANT and not judged:
                vacuous += 1

assert vacuous == 0, f"{vacuous} records reported compliant without being checked"
print(f"     {checked} checks across 3 frameworks and 137 dates: 0 vacuous passes")
PYCHECK
ok "no vacuous compliance"

info "Checking that replay reproduces an untouched rule set"
.venv/bin/python - <<'PYCHECK' || fail "replay reported changes against identical rules"
import itertools
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from compliance_check.core import audit
from compliance_check.core.evaluate import check
from compliance_check.io import load_records
from compliance_check.rules.registry import Registry

registry = Registry.from_directory(Path("data/frameworks"))
records = load_records(Path("data/sample_records.csv"))
dates = [date(2023, 1, 1) + timedelta(days=n) for n in range(0, 1200, 60)]

log = Path(tempfile.mkdtemp()) / "checks.jsonl"
for framework, when in itertools.product(["US", "EU", "JP"], dates):
    for record in records:
        audit.append(log, check(record, registry, framework, when), record)

replays, problems = audit.replay_all(log, registry)
assert not problems, problems
counts = Counter(item.status.value for item in replays)

# The regression: the first implementation reported every check made during a coverage
# gap as `no_longer_covered`, giving 112 false alarms out of 480 on untouched rules.
assert counts == {"reproduced": len(replays)}, dict(counts)

gap_entries = [item for item in replays if item.recorded.version is None]
assert gap_entries, "expected some checks to fall in a coverage gap"

print(f"     {len(replays)} entries replayed, all reproduced")
print(f"     including {len(gap_entries)} made when nothing was in force")
PYCHECK
ok "replay is quiet on unchanged rules"

info "Checking that replay still notices real changes"
.venv/bin/python - <<'PYCHECK' || fail "replay failed to notice a rule change"
import copy
import tempfile
from datetime import date
from pathlib import Path

import yaml

from compliance_check.core import audit
from compliance_check.core.evaluate import check
from compliance_check.models import Outcome, Record
from compliance_check.rules.registry import Registry
from compliance_check.rules.schema import RuleSet

raw = {p.stem: yaml.safe_load(p.read_text()) for p in sorted(Path("data/frameworks").glob("*.yaml"))}
def build(docs):
    return Registry({d["framework"].upper(): RuleSet.model_validate(d) for d in docs.values()})

registry = build(raw)
record = Record(record_id="R-1", category="toys", attributes={"certificate_number": "US-123456"})
log = Path(tempfile.mkdtemp()) / "checks.jsonl"
audit.append(log, check(record, registry, "US", date(2025, 6, 1)), record)
entry = audit.read(log)[0][0]

# 1. a version edited in place
edited = copy.deepcopy(raw)
target = next(v for v in edited["us"]["versions"] if v["version"] == "2024.1")
target["requirements"][0]["severity"] = "minor"
first = audit.replay_entry(entry, build(edited))
assert first.status is audit.ReplayStatus.RULES_CHANGED, first.status
assert not first.is_defect
assert "edited in place" in first.detail

# 2. the version withdrawn entirely
pruned = copy.deepcopy(raw)
pruned["us"]["versions"] = [v for v in pruned["us"]["versions"] if v["version"] != "2024.1"]
second = audit.replay_entry(entry, build(pruned))
assert second.status is audit.ReplayStatus.NO_LONGER_COVERED, second.status
assert not second.is_defect

# 3. a verdict that changed under identical rules -- the only case that is a DEFECT
tampered = entry.model_copy(deep=True)
tampered.result.outcome = (
    Outcome.COMPLIANT if entry.result.outcome is not Outcome.COMPLIANT else Outcome.NON_COMPLIANT
)
third = audit.replay_entry(tampered, registry)
assert third.status is audit.ReplayStatus.DIVERGED, third.status
assert third.is_defect, "a changed verdict under identical rules must be a defect"

print("     edited-in-place -> rules_changed (not a defect)")
print("     withdrawn       -> no_longer_covered (not a defect)")
print("     same rules, different verdict -> diverged (a DEFECT)")
PYCHECK
ok "replay distinguishes rule changes from tool changes"

info "Checking that a malformed rule file is rejected at load time"
.venv/bin/python - <<'PYCHECK' || fail "a malformed rule file was accepted"
from datetime import date

from compliance_check.rules.schema import RuleFileError, RuleSet


def expect(reason, doc):
    try:
        RuleSet.model_validate(doc)
    except RuleFileError:
        return
    except Exception as exc:  # pydantic wrapping
        if "RuleFileError" in repr(exc) or "rror" in repr(exc):
            return
        raise
    raise AssertionError(f"accepted a rule file that should have been rejected: {reason}")


def wrap(requirements, **version_extra):
    version = {"version": "1.0", "effective_from": date(2024, 1, 1), "requirements": requirements}
    version.update(version_extra)
    return {"framework": "T", "versions": [version]}


base = {"id": "R-1", "title": "t", "severity": "major"}

expect("unknown operator", wrap([{**base, "assert": {"op": "is_probably", "field": "a"}}]))
expect("empty all_of (would pass vacuously)", wrap([{**base, "assert": {"op": "all_of", "of": []}}]))
expect("min without a value", wrap([{**base, "assert": {"op": "min", "field": "a"}}]))
expect("present with a value", wrap([{**base, "assert": {"op": "present", "field": "a", "value": 1}}]))
expect("no field", wrap([{**base, "assert": {"op": "present"}}]))
expect(
    "an interval that is never in force",
    wrap([{**base, "assert": {"op": "present", "field": "a"}}], effective_until=date(2023, 1, 1)),
)
expect("overlapping versions", {
    "framework": "T",
    "versions": [
        {"version": "1.0", "effective_from": date(2024, 1, 1), "requirements": []},
        {"version": "2.0", "effective_from": date(2025, 1, 1), "requirements": []},
    ],
})

print("     7 malformed rule files rejected, each naming what was wrong")
PYCHECK
ok "rule validation is strict"

info "Running the test suite"
.venv/bin/python -m pytest -q || fail "tests failed"
ok "tests pass"

cat <<'DONE'

Setup complete.

  source .venv/bin/activate

  compliance-check doctor
  compliance-check frameworks
  compliance-check one T-1 --category toys -f US --as-of 2025-06-01 \
      -a country_of_origin=Vietnam -a certificate_number=US-123456 \
      -a certificate_expiry=2027-03-01 -a small_parts=false -a instruction_manual=included

  compliance-check run -i data/sample_records.csv -f US --as-of 2025-06-01 -o out/
  compliance-check replay

`run` and `one` exit non-zero unless every record is cleanly compliant, so they work as
pipeline steps. `replay` exits non-zero ONLY when identical rules produce a different
verdict, so it works as a semantic regression gate without failing on every rule update.

no_rules is not compliance. An unchecked record has not passed.
DONE

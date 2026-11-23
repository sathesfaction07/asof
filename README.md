# AsOf

Evaluates structured records against regulatory rule sets that are versioned by **effective date**, so a check answers "was this compliant under framework F according to the rules in force on date D" rather than "is this compliant now". Every check is written to an append-only audit log carrying the inputs and a content digest of the rules, which makes the log replayable.

No credentials, no network, no model. Everything here is decided by code and data.

---

## The Problem

"Is this product compliant?" is an incomplete question, and answering the incomplete version is wrong in a way that does not announce itself.

**Rules change on a regulatory timetable, not a release timetable.** A record checked in March 2025 was checked against the rules in force in March 2025. Re-running that check today, after a threshold moved, gives a different answer — and if the tool only remembers "compliant", nobody can tell whether the record changed, the rules changed, or the checker changed.

**Two frameworks disagree about the same record.** The same toy passes under one jurisdiction and fails under another, and the reason has to be attributable to a specific requirement in a specific version.

**And there is a failure mode that a naive implementation walks straight into.** The obvious way to reach a verdict is "compliant if nothing failed":

```python
outcome = COMPLIANT if not failures else NON_COMPLIANT
```

`all([])` is `True`. A record with no applicable requirements — because no version was in force on that date, or because every requirement was scoped to other categories — produces zero failures and is reported as **compliant**. It was never examined. That is the single most dangerous bug a compliance checker can have, and it is one line of very reasonable-looking code.

---

## How It Works

```
record + framework + as-of date
        |
        v
resolve  ->  the one rule version whose interval covers that date
        |     half-open: effective_from <= d < effective_until
        v
scope    ->  per requirement: applies / does not apply / cannot tell
        |
        v
assert   ->  the predicate, tri-state: pass / fail / cannot evaluate
        |
        v
verdict  ->  compliant | non_compliant | indeterminate | no_rules
        |
        v
audit    ->  append inputs + version + digest + verdict, as one JSON line
```

`replay` re-runs a logged check and says which of three things happened: the rules changed, the tool changed, or nothing changed. Only the middle one is a defect.

---

## Outcomes

| Outcome | Meaning |
|:--|:--|
| `compliant` | every applicable requirement was checked and passed |
| `non_compliant` | at least one non-advisory requirement failed |
| `indeterminate` | a requirement could not be evaluated. Nothing failed outright |
| `no_rules` | **nothing was checked**: no version in force, or everything scoped out |

`no_rules` is not compliance. `indeterminate` is not compliance either. Both exist because collapsing them into a pass is how an unchecked record ends up in a report that says it is fine.

A missing field yields `indeterminate`, not a failure: "the certificate number is absent" and "the certificate number is malformed" need different actions from whoever fixes it.

---

## Rule Files

One YAML file per framework, in `data/frameworks/`. Rules are data; adding one is not a code change.

```yaml
framework: US
versions:
  - version: "2024.1"
    effective_from: 2024-07-01
    effective_until: 2026-01-01        # half-open, so 2026-01-01 belongs to the successor
    requirements:
      - id: US-CERT-02
        title: Certificate number must match the two-letter, six-digit format
        severity: critical
        assert: { op: matches, field: certificate_number, value: "^[A-Z]{2}-[0-9]{6}$" }

      - id: US-TOY-AGE-01
        title: Toys with small parts must be labelled 36 months or older
        severity: critical
        applies_to:
          category: toys                # scalar shorthand for [toys]
        assert:
          op: any_of
          of:
            - { op: equals, field: small_parts, value: false }
            - { op: min, field: age_min_months, value: 36 }
```

**Operators.** `present`, `absent`, `truthy`; `equals`, `not_equals`, `one_of`, `not_one_of`, `matches`, `min`, `max`, `before`, `after`; `all_of`, `any_of`, `none_of`. Fifteen in total, and anything else is rejected when the file loads rather than becoming a runtime error on every record.

**Severity.** `critical`, `major`, `minor` all make a record non-compliant. `advisory` is reported but does not change the verdict, which is what lets a rule be staged before it becomes binding.

The shipped rule sets deliberately have three different shapes: **US** has three versions and two transitions, **JP** has one open-ended version, and **EU** has a declared one-year coverage **gap**. The gap is there because it is also what a typo in a date produces, and the tool has to behave sensibly either way.

---

## Running

```bash
bash setup.sh                 # asserts real behaviour, not just install
bash setup.sh --excel
source .venv/bin/activate

asof doctor
asof frameworks

# One record, explored on the command line. Nothing is logged.
asof one T-1 --category toys -f US --as-of 2025-06-01 \
    -a country_of_origin=Vietnam -a certificate_number=US-234567 \
    -a small_parts=true -a age_min_months=36 -a instruction_manual=included

# A batch. Exits non-zero unless everything is cleanly compliant.
asof run -i data/sample_records.csv -f US --as-of 2025-06-01 -o out/

# Re-run every logged check against the current rules.
asof replay
```

`asof frameworks` on the shipped data:

```
EU  European Union — product conformity (illustrative, not legal advice)
    v2022.1   2022-01-01 -> 2024-01-01    2 requirement(s)  b7d9fc41f9fb4da1
    v2025.1   2025-01-01 -> open          5 requirement(s)  5f76922445cf3a5b (in force today)
    gap        2024-01-01 -> 2025-01-01   nothing in force; checks in this window return no_rules
JP  Japan — product safety (illustrative, not legal advice)
    v2024.1   2024-04-01 -> open          3 requirement(s)  d18b97b405763004 (in force today)
US  United States — consumer product safety (illustrative, not legal advice)
    v2023.1   2023-01-01 -> 2024-07-01    3 requirement(s)  9f86719d3f4b5b34
    v2024.1   2024-07-01 -> 2026-01-01    7 requirement(s)  1d47766a004acef2
    v2026.1   2026-01-01 -> open          6 requirement(s)  74bff8160a4bf93d (in force today)
```

And the point of the whole project — the **same record**, two dates:

```
$ asof one R-2 --category toys --as-of 2025-06-01 ...
rules     v2024.1  1d47766a004acef2
outcome   compliant
  pass   critical US-TOY-AGE-01 small_parts='true' vs False; age_min_months=36 >= 36

$ asof one R-2 --category toys --as-of 2026-06-01 ...
rules     v2026.1  74bff8160a4bf93d
outcome   non_compliant
  fail   critical US-TOY-AGE-01 small_parts='true' vs False; age_min_months=36 < 48
```

Nothing about the record changed. The 2026 rules raised the threshold to 48 months.

---

## Usage Reference

### `run`

| Option | Purpose |
|:--|:--|
| `-i, --input` | CSV or JSON of records |
| `-f, --framework` | Which framework. Default `CC_FRAMEWORK` |
| `--as-of` | ISO date. Default today, in **UTC** |
| `-r, --rules-dir` | Directory of rule files |
| `-o, --output` | Results directory |
| `--format` | `csv`, `json`, `excel` |
| `--audit-log` | Where to append. `--no-audit` to skip |
| `--attention-only` | Narrow the CSV. The JSON report keeps everything |
| `--no-details` | Suppress per-requirement lines |

Exits non-zero unless every record is `compliant`, so it composes into a pipeline step. `no_rules` and `indeterminate` count as needing attention, because neither is a pass.

### `replay`

Re-runs every logged check against the current rules.

| Status | Meaning | Exit |
|:--|:--|:--|
| `reproduced` | same rules, same verdict | 0 |
| `rules_changed` | a different version now covers that date, or the version was edited in place | 0 |
| `no_longer_covered` | nothing covers that date any more | 0 |
| `diverged` | **identical rules, different verdict.** The rules did not move, so this codebase did | **1** |

Only `diverged` fails. That is what makes it usable in CI: a regulation update is expected and must not break a build, while a silent change in how this tool evaluates rules must.

### `one` and `doctor`

`one` checks a single record given as `-a field=value` pairs, and writes nothing to the audit log — exploring a rule should not pollute the record of real checks. `doctor` prints the configuration, the loaded frameworks, the operator list, and what each outcome means.

---

## Testing

```bash
pytest                          # 421 tests, ~1.2s
pytest tests/unit -v
pytest --cov=compliance_check --cov-report=term-missing
```

The suite includes property tests over the **real shipped rule sets** rather than only synthetic ones, so editing `data/frameworks/` and breaking something fails the build:

- **no vacuous compliance** — over 3,288 checks (3 frameworks × 137 dates × 8 records), nothing is ever `compliant` with zero judged requirements
- **order independence** — shuffling the order of versions and requirements in a file, across 8 seeds, changes no verdict and no digest
- **at most one version in force** — a daily sweep of 2,200 dates per framework
- **advisory monotonicity** — adding an always-failing advisory requirement never flips a verdict
- **replay reproducibility** — 480 logged entries against untouched rules must all reproduce

`setup.sh` re-derives most of those before it finishes, so a fresh clone proves the behaviour rather than asserting it.

---

## Configuration

```bash
CC_RULES_DIR=data/frameworks
CC_FRAMEWORK=US
CC_AUDIT_LOG=audit/checks.jsonl
CC_OUTPUT_DIR=output
CC_OUTPUT_FORMAT=csv
```

---

## Tradeoffs and Decisions I Made Along the Way

**Replay reported 112 false alarms out of 480 on rules I had not touched.** I built the audit log, then wrote a probe that logged 480 checks and replayed them against the identical rule set. 368 reproduced. 112 came back as `no_longer_covered`.

The cause: `replay_entry` asked "is a version in force now?" and reported `no_longer_covered` whenever the answer was no — without asking whether the *recorded* check had also found nothing in force. Every check made during the EU coverage gap, or before JP existed, was correctly recorded as `no_rules` at the time and then reported as a change on replay. The rendered message was `the check used vNone`, which is nonsense.

I confirmed the diagnosis before fixing it rather than assuming: all 112 had `recorded.version is None`, all 112 had recorded `no_rules`, and the affected dates were exactly the EU gap (6 dates) plus pre-JP-start (8 dates), 14 × 8 records = 112. Arithmetic and observation agreed.

This matters more than the size of the fix. `replay` is meant to be a CI gate, and **a gate that fires on 23% of its input on a good day is a gate people switch off** — at which point a real divergence goes unnoticed. The fix was to compare *coverage* before comparing versions, and to route the re-run through `check()` rather than `evaluate_version()`, so a replay exercises the same resolution path a real check does. There is now a `TestReplayMustNotCryWolf` class and a CI step that fails if it comes back.

**Fixing that could easily have created a worse bug, so I checked.** The tempting fix is to return `REPRODUCED` early whenever nothing is in force. That would make the branch an unconditional pass and hide a genuine defect — a no-coverage entry whose recorded verdict was wrong would replay clean forever. The current code falls through to the outcome comparison instead, and there is a test that tampers with a gap entry's verdict and asserts it comes back as `diverged`. That case was **unreachable** in the first implementation.

**`DIVERGED` cannot be triggered by editing rules, and I nearly treated that as dead code.** No rule change produces it: a rule change is `rules_changed` by construction. It only fires when identical rules yield a different answer, which means the evaluator's behaviour moved. That makes the audit log a regression corpus as well as a record — and it makes the branch look unreachable to anyone reading it. Having previously found a real bug hiding behind an apparently-dead branch, I probed it instead: three separate routes reach it, all tested.

**The interval is half-open, and that was a decision rather than an accident.** `effective_from <= d < effective_until`. An inclusive upper bound puts the changeover day inside two versions at once, and then which one applies depends on the order they appear in the file — a verdict that changes when somebody reformats YAML, with nothing in the output to show it. Overlaps are rejected at load time, `resolve()` filters by interval and never by position, and there is a guard that raises if two versions ever match rather than picking the first.

**The as-of default is UTC, not local.** `date.today()` is the obvious choice and makes a verdict depend on which machine ran it: on a changeover day a checker in UTC+13 and one in UTC-7 disagree for a 20-hour window, and neither is reproducible from the log without also recording the timezone. Fixing the reference frame was cheaper than recording it.

**`present` is deliberately not truthiness.** `bool(value)` would report `small_parts: false` and `age_min_months: 0` as missing — the falsy values are exactly the ones most likely to be a meaningful declaration. Empty and whitespace-only strings *do* count as missing, because a spreadsheet cell containing a space is not a country of origin.

**A requirement whose scope cannot be determined is `indeterminate`, not skipped.** A rule scoped to `category: toys`, against a record with no category, might apply. Skipping it reads as "not regulated", which is a pass nobody can justify. It errors instead, and the record lands in a human's queue.

**The digest covers what a version asserts, not how it is written.** Requirement ids, severities, scopes, predicates and the interval — canonicalised and sorted. Titles, notes and citations are excluded, and reordering requirements does not change it. A digest that fired on every copy-edit would be ignored within a week, and a digest that missed a threshold change would be worthless. Tested in both directions.

**An empty composite is a load-time error.** `all_of: []` passes vacuously under Python's `all`. A requirement that always passes because somebody deleted its last clause is the worst possible failure, so the file is rejected rather than the requirement quietly becoming a no-op.

**No model, and this is the first project here without one.** Four other projects in this portfolio have a local-plus-hosted-model backend, so that pattern is not the contribution. More to the point, a compliance verdict has to be reproducible from its inputs and attributable to a cited requirement. A model in this path would make the audit log unfalsifiable, which is the opposite of what it is for. The rules being *data* is what gives the flexibility a model would otherwise be reached for.

**Contradiction detection is structural, and shallow.** A rule set can be inconsistent in ways this does not catch — two requirements that cannot both be satisfied, for instance. Catching that needs a satisfiability check over the predicate language, which is out of scope here. What is caught is the mechanical class: overlapping intervals, zero-length intervals, duplicate ids, unknown operators, empty composites.

**Order within a rule file is preserved in the output and not normalised.** Findings appear in the order the requirements are written, which makes a report line up with the file somebody is reading. The cost is that `replay` had to learn to compare findings by id rather than by position, or a reordered file would read as a divergence.

---

## Stack

| Concern | Choice |
|:--|:--|
| Language | Python 3.11+ |
| Rules | YAML, validated by Pydantic at load time |
| Models + config | Pydantic, Pydantic Settings |
| CLI | Click |
| Audit log | JSON Lines, append-only |
| Digest | sha256 over canonical JSON |
| Output | stdlib csv/json, openpyxl (optional) |
| Tests | pytest, property tests over the shipped rule sets, Click's CliRunner |
| Lint | ruff |
| Models/AI | none, deliberately |

# AsOf — Technical Architecture

## Overview

AsOf evaluates structured records against regulatory rule sets that are versioned by effective date. A verdict is always tied to a framework, an as-of date, and a content digest of the rules that produced it — which is what makes the audit log replayable rather than merely readable.

**Design principle:** everything that decides a verdict is either code in this repository or data in `data/frameworks/`. There is no model in the path, deliberately, because a compliance verdict has to be reproducible from its inputs and attributable to a cited requirement.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          CLI (asof)                               │
│                                                                              │
│  ┌───────────┐ ┌───────────┐ ┌────────────┐ ┌───────────┐ ┌───────────────┐ │
│  │ run       │ │ one       │ │ frameworks │ │ replay    │ │ doctor        │ │
│  │ a file of │ │ a single  │ │ version    │ │ re-run    │ │ config, the   │ │
│  │ records   │ │ record on │ │ timelines, │ │ the log   │ │ operator list,│ │
│  │           │ │ the CLI   │ │ digests,   │ │ against   │ │ what each     │ │
│  │ exit 1    │ │ exit 1    │ │ declared   │ │ current   │ │ outcome means │ │
│  │ unless    │ │ unless    │ │ GAPS       │ │ rules     │ │               │ │
│  │ all clean │ │ compliant │ │            │ │           │ │ no input,     │ │
│  │           │ │           │ │            │ │ exit 1    │ │ no network    │ │
│  │ writes    │ │ writes    │ │            │ │ ONLY on   │ │               │ │
│  │ the audit │ │ NOTHING   │ │            │ │ diverged  │ │               │ │
│  │ log       │ │ to the log│ │            │ │           │ │               │ │
│  └─────┬─────┘ └─────┬─────┘ └─────┬──────┘ └─────┬─────┘ └───────────────┘ │
└────────┼─────────────┼─────────────┼──────────────┼────────────────────────┘
         │             │             │              │
         ▼             ▼             ▼              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              RULES (rules/)                                   │
│                                                                              │
│  schema.py    RuleSet -> RuleVersion -> Requirement -> predicate             │
│    HALF-OPEN intervals: effective_from <= d < effective_until                 │
│      an inclusive end would put the changeover day in TWO versions, and       │
│      then file order would decide the verdict                                 │
│    load-time validation: unknown operator, empty composite, missing value,    │
│      unary-with-value, zero-length interval, OVERLAPPING versions,            │
│      duplicate ids -- a bad rule file stops a deployment, it does not         │
│      become a runtime error on every record                                   │
│    digest: sha256 over canonical JSON of ids/severities/scopes/predicates      │
│      + interval. EXCLUDES titles, notes, citations -- a digest that fired     │
│      on copy-edits would be ignored within a week                             │
│    gaps(): dates with nothing in force. Legal, and also what a typo looks     │
│      like, so it is reported explicitly                                       │
│                                                                              │
│  predicates.py  15 operators, a closed set                                   │
│    unary      present, absent, truthy                                        │
│    binary     equals, not_equals, one_of, not_one_of, matches, min, max,      │
│               before, after                                                  │
│    composite  all_of, any_of, none_of                                        │
│    TRI-STATE: pass / fail / ERROR. A missing field cannot be judged, so it    │
│      errors rather than failing -- "not declared" and "declared wrongly"      │
│      need different actions from whoever fixes it                            │
│    is_present is NOT bool(value): `false` and `0` are declarations           │
│    _loose_equal survives CSV: "true" == True, "36" == 36, but 1 != True      │
│                                                                              │
│  loader.py    YAML -> RuleSet, errors attributed to the file and requirement  │
│  registry.py  resolve(framework, as_of) -> the ONE version, or None          │
│    filters by INTERVAL, never by position in the file                        │
│    raises if two ever match, rather than picking the first                    │
│    today_utc(), not date.today() -- see below                                │
└─────────────────────────────────┬────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                               CORE (core/)                                    │
│                                                                              │
│  evaluate.py                                                                 │
│    applicability() -> applies / SKIP / ERROR                                 │
│      an UNDETERMINABLE scope errors rather than skipping: skipping reads      │
│      as "not regulated", which is a pass nobody can justify                  │
│                                                                              │
│    derive_outcome()   explicit precedence, each step with a reason            │
│      nothing judged        -> NO_RULES        (because all([]) is True)       │
│      a definite failure    -> NON_COMPLIANT   (known beats unknown)          │
│      something unevaluable -> INDETERMINATE                                  │
│      otherwise            -> COMPLIANT                                       │
│      advisory failures are reported but do not change the verdict            │
│                                                                              │
│  audit.py                                                                    │
│    append()  JSON Lines, append-only. Stores the INPUTS as well as the        │
│              verdict, version label and digest -- a log without the inputs    │
│              can be read but not verified                                    │
│    read()    a corrupt line is REPORTED, not raised on and not skipped        │
│    replay_entry()  routes through check(), the same entry point a real        │
│              check uses, so the gate exercises resolution too                │
│                                                                              │
│      COVERAGE is compared BEFORE version labels. Getting this backwards       │
│      produced 112 false alarms out of 480 on untouched rules. See ADR-002.    │
│                                                                              │
│      reproduced         same rules (or identically absent), same verdict      │
│      rules_changed      different version, or edited in place  -> exit 0      │
│      no_longer_covered  nothing covers that date now          -> exit 0      │
│      diverged           IDENTICAL rules, different verdict    -> EXIT 1      │
│                         the rules did not move, so this codebase did         │
└───────┬──────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  IO (io.py)                                                                   │
│    load_records()  CSV or JSON. A BLANK CELL IS DROPPED, not stored as ""     │
│      -- "the column is empty" and "there is no column" both mean not declared │
│    write_results(results, dir, fmt, *, emit=None)                            │
│      the COMPLETE set always reaches report.json; emit narrows only the CSV,  │
│      so --attention-only cannot quietly shrink the audit trail               │
│    RECORD_FIELDS / FINDING_FIELDS shared by csv, json and excel               │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. CLI (`cli.py`)

| Command | Purpose | Exit |
|:--|:--|:--|
| `run` | Check a file of records | 1 unless every record is `compliant` |
| `one` | Check a single record from `-a field=value` pairs | 1 unless `compliant` |
| `frameworks` | Version timelines, digests, declared gaps | 0 |
| `replay` | Re-run every logged check | 1 **only** on `diverged` |
| `doctor` | Config, frameworks, operators, outcome meanings | 0 |

`run` treats `no_rules` and `indeterminate` as needing attention. Neither is a pass, and making the caller remember that would eventually go wrong.

`replay` failing only on `diverged` is what makes it usable in CI. A regulation update is expected and must not break a build; a silent change in how this tool evaluates rules must.

`one` writes nothing to the audit log. Exploring what a rule does should not pollute the record of real checks.

### 2. Rule schema (`rules/schema.py`)

**Half-open intervals.** `effective_from <= d < effective_until`. An inclusive upper bound puts the changeover day inside two versions at once, and then which one applies depends on the order they appear in the file — a verdict that changes when somebody reformats YAML, with nothing in the output to show it. Two versions meeting exactly at a boundary do not overlap, which is the whole reason for the choice.

**Validation is load-time and strict.** Rule sets are data written by hand with no compiler between the author and production, so the loader rejects: unknown operators (naming the ones that exist), missing `op`/`field`/`value`, a unary operator given a value, empty composites, a composite given a `field`, unexpected keys, nesting past depth 10, zero-length or inverted intervals, duplicate version labels, duplicate requirement ids, and overlapping intervals.

The empty-composite check is the important one. `all_of: []` passes vacuously under Python's `all`, so a requirement whose last clause was deleted would always pass. That is rejected rather than becoming a silent no-op.

**The digest** is sha256 over canonical JSON of the requirements — ids, severities, scopes, predicates — plus the interval, sorted and separator-normalised, truncated to 16 hex characters. Titles, notes and citations are excluded. See ADR-002 for why that boundary is where it is.

**`gaps()`** reports dates between versions with nothing in force. Not an error — a framework can genuinely lapse — but reported prominently because the usual cause is a typo in a date, and the symptom is a `no_rules` verdict that reads like the record was simply unregulated.

### 3. Predicates (`rules/predicates.py`)

Fifteen operators in a closed set. Adding one is a deliberate code change; writing a rule is not.

**Tri-state, always.** A predicate that cannot be evaluated returns `ERROR`, not `False`.

**`is_present` is not `bool(value)`.** The falsy values are exactly the ones most likely to be meaningful declarations: `small_parts: false` and `age_min_months: 0`. Truthiness inverts the meaning of the field for those. Empty and whitespace-only strings *do* count as missing — a spreadsheet cell containing a space is not a country of origin.

**Coercion refuses to guess.** `"36"` becomes 36 because CSV has no types. `"36 months"` does not, because guessing that it means 36 is how a checker starts inventing data. Booleans are not numbers, even though Python says `True == 1`.

**Composite short-circuiting interacts with `ERROR`, and this is the subtlest part of the codebase:**

| Composite | Rule | Why |
|:--|:--|:--|
| `all_of` | `FAIL` beats `ERROR` | a proven violation is more actionable than an unknown |
| `any_of` | one `PASS` wins outright | the others cannot matter |
| `any_of` | no pass + any `ERROR` → `ERROR` | the unknown clause might have been the satisfying one |
| `none_of` | any `PASS` → `FAIL` | a forbidden condition holds |

Each is commented at the point of decision and has its own test.

### 4. Registry (`rules/registry.py`)

`resolve(framework, as_of)` filters versions by interval and returns the single match, or `None`. `None` is not an error and is not compliance — the caller must distinguish, which is what `NO_RULES` is for.

Selection never depends on file position. Overlaps are rejected at load time, and there is a guard that raises if two versions ever match rather than picking the first. That guard is unreachable through the loader by design; it exists so a future change weakening overlap validation fails loudly instead of choosing arbitrarily.

**`today_utc()` rather than `date.today()`.** Local time makes a verdict depend on which machine ran it: on a changeover day, a checker in UTC+13 and one in UTC-7 disagree for a 20-hour window, and neither is reproducible from the log without also recording the timezone. Fixing the reference frame was cheaper than recording it.

### 5. Evaluation (`core/evaluate.py`)

See ADR-001. The two decisions that matter are that an undeterminable scope errors rather than skipping, and the explicit outcome precedence.

### 6. Audit (`core/audit.py`)

See ADR-002. JSON Lines, append-only, storing inputs as well as verdicts, with replay classifying rather than just re-running.

### 7. IO (`io.py`)

`load_records()` reads CSV (`utf-8-sig`, because spreadsheets write a BOM) or JSON. A blank CSV cell is **dropped** rather than stored as `""` — both mean "not declared", and collapsing them means a rule using `absent` behaves the same whether the exporter emitted the column or not.

`write_results(results, directory, fmt, *, emit=None)` — the complete set is the required positional argument and always reaches `report.json`; `emit` narrows only the tabular output. Arranged this way so the worst a forgetful caller can do is fail to filter, which is visible, rather than drop rows from the audit trail, which is not.

---

## The Bug This Codebase Had

I built the audit log, then probed it: 480 checks logged across three frameworks and 20 dates, replayed against the **identical, untouched** rule set.

```
{'reproduced': 368, 'no_longer_covered': 112}
R-1 no_longer_covered: no version of EU now covers 2024-02-25; the check used vNone
```

**112 false alarms out of 480 — 23% — on rules nobody had touched.**

`replay_entry` asked "is a version in force now?" and reported `NO_LONGER_COVERED` whenever the answer was no, without asking whether the *recorded* check had also found nothing in force. Every check made during the EU coverage gap, or before JP existed, had been correctly recorded as `no_rules` and was then reported as a change. `the check used vNone` is nonsense — there was no version to name.

Diagnosis confirmed before fixing: all 112 had `recorded.version is None`, all 112 had recorded `no_rules`, and the affected windows were exactly the EU gap (6 of 20 dates) plus pre-JP-start (8 dates) — 14 × 8 records = 112. Arithmetic and observation agreed.

The consequence mattered more than the fix. `replay` is a CI gate, and a gate that fires on a quarter of its input on a good day gets switched off, at which point a real divergence goes unseen.

**The tempting fix would have been worse.** Returning `REPRODUCED` early whenever nothing is in force turns the branch into an unconditional pass and hides a genuine defect. Instead, coverage is compared first and then execution falls through to the outcome comparison, which now runs whether or not a version was in force. A test tampers with a gap entry's verdict and asserts `DIVERGED` — a case the first implementation could not reach at all.

---

## Data Flow

```
1.  asof run -i data/sample_records.csv -f US --as-of 2025-06-01 -o out/
2.  CLI drops None overrides -> Settings(rules_dir=..., framework="US", ...)
3.  Registry.from_directory  -> load each YAML, validate, reject overlaps
4.  load_records()           -> 8 Records; blank cells dropped
5.  per record: check(record, registry, "US", 2025-06-01)
6.    resolve  -> versions whose interval covers 2025-06-01
                  2023.1 [2023-01-01, 2024-07-01)  no
                  2024.1 [2024-07-01, 2026-01-01)  YES
                  2026.1 [2026-01-01, open)        no
                  exactly one match -> v2024.1, digest 1d47766a004acef2
7.    per requirement:
        US-LABEL-01   no scope        -> present(country_of_origin)   PASS
        US-CERT-02    no scope        -> matches(cert, ^[A-Z]{2}-...) PASS
        US-TOY-AGE-01 category: toys  -> record is toys -> any_of(...) FAIL
        US-BATT-01    category: [electronics, appliances] -> SKIP
        US-DOC-01     advisory        -> present(manual)              PASS
8.    derive_outcome -> a non-advisory FAIL exists -> NON_COMPLIANT
9.  audit.append -> one JSON line: inputs + v2024.1 + digest + verdict
10. write_results(results, out/, "csv")
      out/report.json    summary + every record + every finding
      out/results.csv    one row per record
      out/findings.csv   one row per finding
11. exit 1, because 6 of 8 need attention
```

Observed on the shipped sample:

```
  R-1  compliant v2024.1 6pass 1skip
  R-2  non_compliant v2024.1 5pass 1fail 1skip
  R-4  indeterminate v2024.1 4pass 1err 2skip
  R-5  indeterminate v2024.1 5pass 2err
  total 8   compliant 2   non_compliant 4   indeterminate 2   needs_attention 6
```

And the same records on `EU 2024-06-01`, inside the declared gap: eight `no_rules`, zero `compliant`.

---

## Configuration

`CC_`-prefixed environment variables or a `.env` file. No list-typed fields, since Pydantic Settings parses those as JSON and `CC_FRAMEWORKS=US,EU` would produce a decode error that reads like a bug in the tool.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CC_", env_file=".env", extra="ignore")

    rules_dir: Path = Path("data/frameworks")
    framework: str = "US"
    audit_log: Path = Path("audit/checks.jsonl")
    output_dir: Path = Path("output")
    output_format: str = "csv"
    log_level: str = "INFO"
```

---

## Running

```bash
bash setup.sh                 # asserts behaviour before finishing
bash setup.sh --excel
source .venv/bin/activate

asof doctor
asof frameworks
asof one R-2 --category toys -f US --as-of 2025-06-01 -a small_parts=true ...
asof run -i data/sample_records.csv -f US --as-of 2025-06-01 -o out/
asof replay
```

`setup.sh` does more than install. It asserts: the shipped rule files load with the documented shapes including the EU gap; the same record gets different verdicts on 2025-06-01 and 2026-06-01; 3,288 checks across 3 frameworks and 137 dates produce zero vacuous passes; 480 logged entries all reproduce against untouched rules, including 112 made when nothing was in force; a version edited in place is `rules_changed`, a withdrawn one is `no_longer_covered`, and a changed verdict under identical rules is `diverged`; and seven malformed rule files are each rejected with a reason.

---

## Testing

```bash
pytest                          # 421 tests, ~1.2s (418 + 3 skipped without openpyxl)
pytest tests/unit -v
pytest --cov=compliance_check --cov-report=term-missing
```

| File | Covers |
|:--|:--|
| `conftest.py` | the shipped rule files as fixtures, a `rule_doc` builder for focused tests, date sweeps, a shuffling factory |
| `test_predicates.py` | all 15 operators; `is_present` vs truthiness; coercion refusals; every composite short-circuit rule; the empty-composite error |
| `test_schema.py` | 12 load-time validation failures; half-open boundary edges; digest stability and sensitivity in both directions; overlap and gap detection; loader error attribution |
| `test_registry.py` | order-independence properties across 8 seeds; a 2,200-day daily sweep asserting at most one version in force; every declared span; the same-record-different-date case; UTC default |
| `test_evaluate.py` | `TestNoVacuousCompliance` as a property over the real rule sets; applicability incl. undeterminable scope; every `derive_outcome` precedence step; advisory monotonicity as a property |
| `test_audit.py` | `TestReplayMustNotCryWolf` — the 112-false-alarm regression; all four replay statuses; all three `DIVERGED` routes; corrupt-line handling |
| `test_io.py` | record loading from CSV/JSON incl. BOM and blank cells; every output format; `emit` narrowing the CSV but never the report |
| `integration/test_cli.py` | all five commands, exit codes, the audit log being written by `run` and not by `one`, replay staying quiet on gap-dated checks |

The property tests run against the **real shipped rule sets**, so editing `data/frameworks/` and breaking something fails the build rather than somebody's first run. CI additionally asserts the exact summary of the sample data and greps the replay output for any non-`reproduced` status after deliberately seeding the log with gap-dated checks.

Not covered: nothing is mocked, because nothing here talks to a network. The uncovered paths are the `openpyxl`-missing branch in `_write_excel` and the multi-match guard in `resolve()`, which is unreachable through the loader by design and is tested by constructing the invalid state directly.

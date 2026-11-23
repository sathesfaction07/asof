# ADR 001: Four outcomes, because "no failures" is not "passed"

## Status

Accepted

## Context

A compliance check needs to produce a verdict. The obvious set is two values, and the obvious way to compute it is:

```python
failures = [f for f in findings if f.status is Status.FAIL]
outcome = Outcome.NON_COMPLIANT if failures else Outcome.COMPLIANT
```

That reads correctly and is wrong in the most dangerous direction available.

### `all([])` is True

`findings` can legitimately be empty, or contain nothing that was actually judged. Three routes get there:

- **No version in force.** The as-of date falls before the framework existed, or inside a coverage gap. The shipped EU rule set has a declared one-year gap; a check dated inside it resolves to nothing.
- **Everything scoped out.** Every requirement carries `applies_to`, and the record matches none of them.
- **A version with no requirements.** Legal in the schema, and what a half-finished rule file looks like.

In all three, `failures` is empty and the two-value formulation reports **compliant**. The record was never examined. A report saying "compliant" about a record nothing looked at is worse than no report, because it stops anyone looking.

This is not a hypothetical. On the shipped sample data, `EU` on `2024-06-01` produces zero findings for all eight records.

### A missing field is not a wrong field

The other pressure comes from below. A requirement asserting `certificate_expiry` is after some date cannot be evaluated at all when the field is absent. Two-valued logic forces that into either:

- **pass** — hides missing data, and is the same failure as above
- **fail** — tells whoever reads the report that the value is wrong, when the value is missing

Those need different actions. "Chase the supplier for a certificate" and "the certificate we have is out of date" are different work items, and conflating them makes the report unusable for the person who has to act on it.

## Decision

Four outcomes.

| Outcome | Meaning |
|:--|:--|
| `COMPLIANT` | every applicable requirement was checked and passed |
| `NON_COMPLIANT` | at least one non-advisory requirement failed |
| `INDETERMINATE` | a requirement could not be evaluated; nothing failed outright |
| `NO_RULES` | nothing was checked: no version in force, or everything scoped out |

Underneath, each requirement produces one of four statuses — `PASS`, `FAIL`, `ERROR`, `SKIP` — and the verdict is derived with explicit precedence:

```python
judged = [f for f in findings if f.status is not Status.SKIP]
if not judged:                              return NO_RULES
if any(f.counts_against for f in judged):   return NON_COMPLIANT
if any(f.status is ERROR for f in judged):  return INDETERMINATE
                                            return COMPLIANT
```

Each step has a reason:

1. **Nothing judged → `NO_RULES`.** The step that exists solely because `all([])` is True.
2. **A definite failure → `NON_COMPLIANT`.** A known violation outranks an unknown. There is no value in saying "indeterminate" about a record already proven bad.
3. **Something unevaluable → `INDETERMINATE`.** The record might be fine. We cannot say, and saying nothing is more useful than guessing.
4. **Otherwise → `COMPLIANT`**, which now means something specific: every applicable requirement was checked and passed.

### Supporting decisions

**A predicate that cannot be evaluated returns `ERROR`, not `False`.** Every comparing operator (`equals`, `min`, `matches`, `before`, …) errors when its field is absent or blank.

**A scope that cannot be determined is `ERROR`, not `SKIP`.** A requirement scoped to `category: toys` against a record with no category might apply. Skipping reads as "not regulated", which is a pass nobody can justify.

**An empty composite is a load-time error.** `all_of: []` passes vacuously. A requirement that always passes because somebody deleted its last clause is rejected when the file loads rather than becoming a silent no-op.

**`advisory` failures are reported but do not change the verdict.** This is what lets a rule be staged before it becomes binding, and it is asserted as a property: adding an always-failing advisory requirement never flips a verdict.

**`present` is not `bool(value)`.** `small_parts: false` and `age_min_months: 0` are declarations. Truthiness reports them as missing, inverting the meaning of the field for exactly the values most likely to matter. Empty and whitespace-only strings *do* count as missing.

## Validation

`tests/unit/test_evaluate.py::TestNoVacuousCompliance` asserts this as a property over the real shipped rule sets rather than only in unit tests:

- across 3 frameworks × 129 sweep dates × 8 records, nothing is ever `COMPLIANT` with zero judged findings
- and the inverse: `NO_RULES` is never reported when something *was* judged

`setup.sh` re-derives it on a fresh clone — 3,288 checks, 0 vacuous passes — and CI asserts the exact summary of the sample data, so a change in this logic fails the build with a diff rather than a vague regression.

`TestDeriveOutcome` pins each precedence step, and `TestAdvisoryMonotonicity` pins the advisory property.

## Consequences

**Good**

- An unchecked record cannot be reported as compliant, which is the whole point
- `indeterminate` is an actionable queue: these records need data, not a decision
- `no_rules` surfaces a rule-file mistake that would otherwise look like a clean pass. A typo in `effective_from` produces exactly this
- `run` can exit non-zero on anything that is not a clean pass, so the CLI composes into a pipeline gate without anybody having to remember that `no_rules` is not success
- The precedence is four lines and reads in the order a human would reason

**Bad**

- Four outcomes is more than a reader wants. "Is it compliant?" now has three ways to be answered "not exactly", and `doctor` has to explain them
- `NO_RULES` conflates two genuinely different situations — no version in force, and everything scoped out. The `note` distinguishes them in prose, but the machine-readable field does not. Splitting them would mean five outcomes, which felt worse
- `INDETERMINATE` will be the most common non-pass on messy data, and a queue nobody works is a queue that grows. That is an operational problem this cannot solve
- The tri-state propagates into the composite operators, where `FAIL` beating `ERROR` inside `all_of` but `PASS` beating both inside `any_of` is genuinely subtle. It is commented at the point of decision and has six tests, and it is still the part most likely to be misread

**Neutral**

- Severity ranking needs an explicit table because `StrEnum` members compare as strings and `"critical" < "major"` alphabetically is the wrong order. A latent trap, pinned by a test
- `advisory` findings appear in output as `advisory` rather than `fail`, which is a small rendering special case
- Nothing distinguishes "indeterminate because a field was missing" from "indeterminate because the scope was undeterminable". Both appear as `ERROR` findings with different `detail` text, which is enough for a human and not machine-readable

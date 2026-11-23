# ADR 002: The audit log stores inputs and a rule digest, and replay compares coverage first

## Status

Accepted

## Context

The requirement was "every check recorded with timestamp, inputs, verdict". The obvious implementation writes exactly that and satisfies the letter of it while failing the purpose.

The question people ask of a compliance audit log is not "what did we decide". It is **"would we decide the same thing today, and if not, why not?"** A log of timestamps and verdicts cannot answer that. Re-running a check months later gives some answer, and there is no way to tell whether a difference came from the rules moving, the data moving, or the checker moving. Those have completely different consequences: the first is history working correctly, the second is a data problem, the third is a bug.

## Decision

### Each entry carries enough to re-derive the verdict

```python
class AuditEntry(BaseModel):
    checked_at: datetime
    tool_version: str
    record: Record          # the inputs, so the check can actually be re-run
    result: CheckResult     # including version label AND a content digest
```

The `record` is stored because an audit log without the inputs can be read but not verified, which makes it a report rather than an audit.

The **digest** is a sha256 over the canonical content of the resolved version: requirement ids, severities, scopes, predicates, and the interval — sorted and serialised deterministically. It excludes titles, notes and citations.

That boundary is the decision. A digest covering prose would change on every copy-edit and be ignored within a week. A digest missing a threshold change would be worthless. So it covers what a version *asserts* and not how it is *written*, and reordering requirements in the file does not change it.

### Replay classifies rather than just re-running

```python
current = check(entry.record, registry, framework, as_of)   # same entry point a real check uses
version = registry.resolve(framework, as_of)

if was_covered and not is_covered:   -> NO_LONGER_COVERED
if is_covered and not was_covered:   -> RULES_CHANGED  (newly covered)
if version.version != recorded:      -> RULES_CHANGED  (a different version covers this date)
if version.digest  != recorded:      -> RULES_CHANGED  (edited in place)
if current.outcome != recorded:      -> DIVERGED       (the only defect)
if findings differ by id:            -> DIVERGED
                                     -> REPRODUCED
```

Only `DIVERGED` is a defect, and `replay` exits non-zero only for that. A regulation update must not fail a build; a silent change in how this tool evaluates rules must.

The re-run goes through `check()`, the same entry point a real check uses, rather than reaching straight for `evaluate_version()`. Replaying through a different path would leave resolution — the most interesting behaviour in the codebase — untested by the gate that exists to test it.

### Coverage is compared before versions

This ordering is the part that was wrong, and the reason this ADR exists.

## The bug this fixes

The first implementation started with:

```python
version = registry.resolve(recorded.framework, recorded.as_of)
if version is None:
    return Replay(status=NO_LONGER_COVERED, detail=f"... the check used v{recorded.version}")
```

Reasonable-looking, and it never asks whether the *recorded* check had also found nothing in force.

I logged 480 checks across three frameworks and 20 dates, then replayed them against the **identical, untouched** rule set:

```
{'reproduced': 368, 'no_longer_covered': 112}
R-1 no_longer_covered: no version of EU now covers 2024-02-25; the check used vNone
```

**112 false alarms out of 480 — 23% — on rules I had not touched.** And `the check used vNone` is nonsense: there was no version, so there is no `v` to name.

I confirmed the diagnosis rather than assuming it:

- all 112 had `recorded.version is None`
- all 112 had recorded `outcome = no_rules`, so the original check had been correct
- all 112 would re-evaluate to `no_rules` again
- the affected windows were exactly the EU coverage gap (6 of the 20 dates) and pre-JP-start (8 dates): 14 × 8 records = 112

Arithmetic and observation agreed, which is what made it safe to fix.

The consequence matters more than the size of the change. `replay` is intended as a CI gate. **A gate that fires on a quarter of its input on a good day is a gate people switch off**, and then a real divergence goes unseen. The false alarm was not cosmetic; it would have destroyed the feature.

### The tempting fix would have been worse

The shortest fix is to return `REPRODUCED` early whenever nothing is in force. That turns the branch into an unconditional pass and hides a genuine defect: a no-coverage entry whose recorded verdict was wrong would replay clean forever.

So coverage comparison happens first, and then execution **falls through** to the outcome comparison — which now runs whether or not a version was in force. `test_the_no_coverage_case_still_compares_outcomes` tampers with a gap entry's verdict and asserts `DIVERGED`. That case was unreachable in the first implementation.

### DIVERGED looks like dead code and is not

No rule edit can produce `DIVERGED`. A rule edit is `RULES_CHANGED` by construction, and evaluation is deterministic given a record and a version. It fires only when identical rules yield a different answer — which is precisely what a behaviour change in this codebase looks like. That makes the audit log a **regression corpus** as well as a record.

Having previously found a real bug hiding behind an apparently-unreachable branch, I probed this one rather than trusting the reasoning. Three routes reach it, all tested:

1. a changed verdict under an identical digest
2. a changed per-requirement finding under an identical verdict
3. a no-coverage entry whose recorded verdict was not `no_rules`

## Validation

- `TestReplayMustNotCryWolf` — 480 entries against untouched rules, all must reproduce, with the offending count in the assertion message
- `TestReplayDetectsRuleChanges` — edited in place, version shifted, version withdrawn, newly covered
- `TestReplayDetectsToolChanges` — all three `DIVERGED` routes
- `TestDigest` — stable under YAML round-trip and requirement reordering; changes on threshold, severity and interval edits; unchanged by title and note edits
- `setup.sh` asserts `Counter(...) == {"reproduced": 480}` and that some of those entries fall in a coverage gap
- CI greps the replay output for `diverged`, `no_longer_covered` and `rules_changed` and fails on any of them, after deliberately seeding the log with gap-dated checks

## Consequences

**Good**

- The log answers the question actually asked of it, and distinguishes three situations that a verdict-only log cannot
- `replay` is usable in CI: silent on rule updates, loud on behaviour changes
- Editing a version in place — the thing you must not do to a version already used for checks — is detected and named, rather than silently changing history
- Findings compare by id, so reordering a rule file replays clean while an added or removed requirement is named
- JSON Lines with append-only writes means concurrent processes interleave whole lines rather than corrupting each other

**Bad**

- The log stores every input, so it grows with the data and contains a copy of it. For a large catalogue that is a real storage cost, and this makes no attempt at rotation or compaction
- A 16-hex-character digest in the output is noise to most readers. It is the only way to make `rules_changed` verifiable, but nobody enjoys reading it
- `read()` loads the whole file into memory. Fine for thousands of entries, wrong for millions, and the streaming version is not written
- Replay is only as good as the record round-tripping through JSON exactly. It does for every type the loaders produce — verified at 0 mismatches over 480 entries — but an exotic value in `attributes` could in principle differ and be blamed on the evaluator

**Neutral**

- `tool_version` is recorded but not acted on. A `DIVERGED` result names both versions and leaves the judgement to a human, which seemed better than guessing whether a behaviour change was intended
- The digest is truncated to 16 hex characters. Collision risk is negligible at this scale and the full 64 characters made every line unreadable
- A malformed audit line is reported rather than raised on, so one corrupt line does not make the rest unreadable. It is also not silently skipped: a log with unexplained gaps is worse than one with a stated gap
- `one` deliberately writes nothing to the log. Exploring a rule should not pollute the record of real checks, at the cost of the log not reflecting everything a person ran

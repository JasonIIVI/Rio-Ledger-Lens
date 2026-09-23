# Narrative eval

**Not yet run.** The case set (`evals/narratives/cases.json`, 16 entries) and the grader
(`ledgerlens.narrative_eval`) are in place and tested; the first real run needs
`ANTHROPIC_API_KEY`. When it has been run, this file is replaced by the report:

```bash
ledgerlens eval-narratives data/ledger.csv        # writes docs/narrative-eval.md
```

## What it will measure

Each case is a flagged entry from the default synthetic ledger. The real narrator writes the
note; the grader checks properties a reviewer would check before trusting it:

| Check | Meaning |
|---|---|
| Contract holds | the five keys are present and non-empty, confidence is one of high / medium / low |
| Mentions the required facts | the amount, the accounts and every test that fired, as written in the case file |
| Asserts nothing | no wording that turns a question into a finding (fraud, intent, concealment, "is an error") |
| Evidence is specific | at least one evidence item names a figure, an account or a date |
| No invented numbers | every number in the note appears in the entry the model was shown |
| Confidence in band | where a careful reviewer would put it (written per case, before any run) |

These are floors, not a quality score: a note can pass every check and still be dull, so every
narrative is kept in `out/narrative-eval/results.jsonl` for reading. The expectations were
written by hand from each entry's own data and are never adjusted to fit a model's output.
Similarity to a reference narrative was rejected as a metric because the reference would itself
be model-written, and scoring similarity to it rewards imitation rather than correctness.

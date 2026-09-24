# Narrative eval

Run: 2026-09-23T19:59:19+00:00 · model: `claude-opus-5` · cases: 16 · graded: 16 · invalid: 0 · errors (not scored): 0

Case file sha256: `fd49e46f2dd59a6d14c1bfff17186e3eef93172b39803074c0b5c987bade96bb` · rows: `evals/narratives/runs/2026-09-23-claude-opus-5/results.jsonl` · re-graded 2026-09-24T00:48:33+00:00 by the grader in this commit, no API calls

**Provenance note:** 16 row(s) were first graded under a different case file - one whose hash was not recorded (the rows predate provenance tracking) - and re-graded under the one above. If the expectations differ between the two files, the re-graded score is not the original run's score; compare the files before reading it as one.

## What this measures

Each case is a flagged entry from the default synthetic ledger. The real narrator writes the note; the grader checks properties a reviewer would check before trusting it. These are floors, not a quality score: a note can pass every check and still be dull, so the narratives themselves are kept in the results file for reading. Expectations were written by hand from the entry's own data, never from a model's output.

| Check | Pass rate |
|---|---:|
| Contract holds (five keys, non-empty, valid confidence) | 100% |
| Mentions the required facts (amount, accounts, tests) | 100% |
| Asserts no error, intent or irregularity | 100% |
| At least one evidence item is specific | 100% |
| Every number in the note is in the entry (or a cited standard) | 100% |
| Confidence in the expected band | 94% |
| **All of the above** | 94% |

Confidence baseline: the bands accept more than one level on 14 of 16 cases, so a narrator that always answered "medium" would pass the confidence check on 14/16 (88%) of them - always "high" 9/16 (56%), always "medium" 14/16 (88%), always "low" 7/16 (44%). Read the confidence row against that number, not against zero.

## Per case

| Entry | Archetype | Tests | Confidence | Failed checks |
|---|---|---|---|---|
| JE-2024-004999 | round_amount | JET-01, JET-11 | medium | - |
| JE-2025-005011 | weekend_entry | JET-02, JET-05, JET-12 | low | confidence_in_band |
| JE-2024-005020 | after_hours_entry | JET-03, JET-12 | low | - |
| JE-2024-005029 | holiday_entry | JET-04 | low | - |
| JE-2024-005034 | period_end_manual_revenue | JET-02, JET-05 | medium | - |
| JE-2024-005043 | just_under_threshold | JET-06, JET-12 | medium | - |
| JE-2025-005058 | duplicate_entry | JET-02, JET-07 | medium | - |
| JE-2024-005069 | rare_account_pair | JET-08, JET-12 | medium | - |
| JE-2025-005078 | dormant_account | JET-08, JET-12 | low | - |
| JE-2025-005080 | benford_drift | JET-12 | low | - |
| JE-2024-005084 | unbalanced_entry | JET-09, JET-10, JET-12 | high | - |
| JE-2025-005007 | multi_flag | JET-01, JET-09 | medium | - |
| JE-2025-005085 | multi_flag | JET-10, JET-12 | medium | - |
| JE-2024-005012 | multi_flag | JET-02, JET-12 | low | - |
| JE-2024-000115 | benign | JET-12 | low | - |
| JE-2024-000648 | benign | JET-12 | low | - |

## Misses, with the text that failed

### JE-2025-005011 (weekend_entry): confidence_in_band

> A manual entry dated and keyed on Saturday 31 May 2025 recording $359.08 of cash receipts against Service Revenue (4100) on the last day of the period.
> JET-05 fired because the $359.08 credit to Service Revenue (4100) is a manual revenue posting made on the final day of the period, which is the point at which cut-off needs to be evidenced. JET-02 adds that it was keyed on a Saturday, and JET-12 that dsilva is not on the approved list (controller, mchen). The amount is small and the debit is to Cash - Operating (1000), which is consistent with a routine recording of a cash receipt at month end rather than an accrual-based revenue adjustment.
> The customer invoice or cash receipt documentation supporting the $359.08 credited to Service Revenue (4100), showing the service delivery date
> Bank statement or remittance confirming the $359.08 receipt into Cash - Operating (1000) and the date it cleared
> Enquiry of the controller as to whether dsilva holds delegated authority to key manual journals and whether month-end Saturday postings are part of the normal close routine
> Revenue cut-off and manual journal approval at period end


## Grader notes

- **2026-09-23** - The first real run scored 88% on all checks. One of the two misses was the grader's: no_invented_numbers counted the citation "AU-C 240" as an invented number. The fix admitted every number in the system prompt as shown to the model, and the run was re-graded offline to 94%.
- **2026-09-24** - That fix was too broad. The system prompt's style example quotes $9,912 and account 4000, so a note that copied the example onto an entry with neither would have passed. The union with the prompt was replaced by an explicit citation allowlist (AU-C 240 only). Re-grading the 2026-09-23 run under it changed no row: no note had used either figure, and the score stayed at 94%. Those rows predate the case-file hash; git records the case file as unchanged since commit 1ff36ef (2026-09-23 19:29 UTC), before the run was graded (19:57 UTC), which is the ordering the first review asked to have checked.

## Cost

16 requests · 4,904 uncached input tokens · 43,984 read from cache · 0 written to cache · 6,828 output tokens

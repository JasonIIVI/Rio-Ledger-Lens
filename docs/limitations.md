# Limitations

What the published numbers can and cannot carry, and the one measurement made to break
their circularity. Each section says what it rests on, so it can be checked.

## The default figures are partly circular

Precision 0.843 and recall 0.974 on the default ledger are not measurements of detection.
For eight of the eleven archetypes the generator injects the anomaly by the definition the
test written for it checks, and on the default ledger that test catches every one. The other
three are not injected by exactly that definition, so which test catches them depends on the
draw. On the default ledger it is mostly tests written for something else
(`evaluate.attribution`, printed by `ledgerlens test --labels`):

- `benford_drift`: three of four, all by JET-12, because the injector keys them as manual
  entries by a user picked at random. No test flags them for their amounts.
- `dormant_account`: four of four, none by JET-09. The injector posts to rarely used
  accounts, not to ones that were active and then went quiet.
- `rare_account_pair`: six of seven, three by JET-08, because four of the seven drew the
  same odd pair, which then occurs too often to be rare at JET-08's setting.

On another seed the split moves: on seed 101 JET-08 catches all seven rare pairs. Either way
their recall is a by-product of how they were injected.

The model tier on the same ledger is a re-ranker of the rule tier's queue, not an
independent detector: the anomalies were defined as rule violations. The README's tables
and `docs/tuning.md` carry the figures; the caveat beside every printed figure says the
same in one sentence (`evaluate.DETECTION_CAVEAT`, `evaluate.MODEL_TIER_CAVEAT`).

Only one threshold was chosen on this ledger, JET-11's. Every other setting was swept on it
and left alone (`docs/tuning.md`): JET-01 and JET-10 are flat across the whole range swept;
JET-03, 05 and 07 are flat over most of it, and JET-06 only from 0.03 to 0.05, each with the
default at or inside the edge of the injector's own definition; JET-08 and JET-09 move, for
reasons the generator explains, not a ledger.

## The held-out measurement: registered before it was made

To measure detection, a ledger needs anomalies no rule describes. The held-out ledger
carries five (`schema.HeldOutType`, 43 entries, `generate.HELD_OUT_MIX`):

| Archetype | Entries | What it is |
|---|---:|---|
| `token_amount` | 8 | $0.50 to $2.99 on an ordinary pair |
| `unfamiliar_user` | 8 | an ordinary entry keyed by `temp01`, `temp02`, `svc_import` or `admin2` |
| `split_purchase` | 9 | three purchases from one vendor on one day, $10,050 to $10,800 together |
| `round_trip` | 8 | cash revenue in, the same amount out as ad spend three business days later |
| `shell_vendor_drip` | 10 | $150 to $450 every fortnight (moved to the next business day) to a vendor whose name is a near miss |

Each is built to be rule-silent: a business day and hour, a common pair whose source is
neither Manual nor System, cents, nothing in the 3% beneath the approval limit, balanced. That
silences JET-01 to 06, 10 and 12 by construction. JET-07, 08, 09 and 11 read the population,
so they were checked instead: on the four development seeds the tests use, no test fires on a
held-out entry and no other entry's flags change (`tests/test_generate.py`), and in review
seven more seeds at the default size showed no flag on a held-out entry either. Two edges were
found and are kept in view. The held-out lines join JET-11's per-account baselines, so on
some seeds another entry's JET-11 flag moves (seed 606: three round-amount entries lose it;
seed 808: one normal entry gains it). And on a ledger of a few dozen entries JET-08 fires on
held-out entries, and moves other entries' JET-08 flags too. So the measured ledger's figures for the eleven old archetypes include
whatever its held-out lines do to JET-11, and the measurement is of the model tier: how many
of the held-out entries its budget (the top 2% of entries by rank, ties included) holds,
against chance.

**How it is made, once.** The code was frozen at the commit tagged `held-out-freeze`.
Every command runs from a checkout of that tag with `PYTHONPATH` set to the checkout's `src`
(the development environments install another checkout's code), and one `python -c` line in
each environment prints `ledgerlens.__file__`, `ledgerlens.__version__` (1.1.0), Python,
numpy, pandas and scikit-learn, beside `git rev-parse HEAD`. The ledger is generated once, in
the Python 3.12 environment: `ledgerlens generate --held-out --out-dir data` (seed
`HELD_OUT_SEED`, never generated with before; the tests use other seeds, and fail if
anything generates with it), and the sha256 of `heldout-ledger.csv` and `heldout-labels.csv`
is recorded. Then `ledgerlens test` and `ledgerlens score`, each with
`--labels data/heldout-labels.csv`, run on those same two files in the 3.12 environment and
again in the 3.9 one. No `heldout-ledger.controls.json` sits beside the ledger (`test` would
print a "Controls from" line): it is measured against the synthetic company's controls. Both
environments' outputs are pasted below as printed, with the date, and if the two differ in
any figure, that is said rather than one of them chosen. Nothing is tuned on it afterwards: a later change
that moves these figures labels the new ones "re-measured, no longer held out". A defect in
the code found after the freeze is disclosed here, not fixed before the measurement.

**What is reported, decided before the ledger existed:**

- for each held-out archetype: n; the rule tier's count (built to be zero, reported as it is);
  how many the model's budget holds; and chance, n x B / P for a budget of B entries in a
  population of P;
- the held-out archetypes together: how many of 43 the budget holds, against chance, and
  the lift;
- the eleven generator archetypes' rule-tier recall on the same ledger, per archetype as
  `test --labels` prints it and their total caught of n summed from those rows (the overall
  recall and F1 `test` prints include the 43 held-out entries), and the tier segments;
- "the model catches an archetype" means at least half of its entries fall in the budget.

| Archetype | n | Rule tier caught | Model, within its budget | Chance |
|---|---:|---:|---:|---:|
| `token_amount` | 8 | 0 | 6 | 0.16 |
| `unfamiliar_user` | 8 | 0 | 0 | 0.16 |
| `split_purchase` | 9 | 0 | 0 | 0.18 |
| `round_trip` | 8 | 0 | 0 | 0.16 |
| `shell_vendor_drip` | 10 | 0 | 0 | 0.20 |
| all held-out | 43 | 0 | 6 | 0.86 |

## The measurement

Made once, on 2026-10-18 (21:50 UTC), from the commit tagged `held-out-freeze`
(`605558173459cdf7bded05107a66eede7b215c86`), as registered above. The output of `test` and
`score` was the same, byte for byte, in the two environments. The table above is filled in
from it.

**What it says, read as registered.**

- **The rule tier caught none of the 43 held-out entries**, as it was built not to: JET-07,
  08, 09 and 11, the four that read the population, fired on none of them either.
- **The model tier's budget**, 103 of the 5,138 entries (the top 2% by rank), holds **6 of
  the 43**, against 0.86 by chance: 7.0 times chance, a recall of 0.14 within the budget.
- **All six are token amounts.** By the registered reading (at least half of an archetype's
  entries in the budget), the model catches `token_amount`, six of eight, and none of the
  other four:
  - `unfamiliar_user`: none of the eight, although `user_freq` sees an unfamiliar user;
  - `split_purchase`, `round_trip` and `shell_vendor_drip`: none. These exist only between
    entries, which per-entry features cannot see.
- **The eleven generator archetypes**, on the same ledger: the rule tier finds 74 of their 77
  entries, summed from `test`'s per-archetype rows (0.961). As on the default ledger, those
  are mostly the definitions read back.
  - The overall recall `test` prints, 0.617, includes the 43 held-out entries.
  - Its precision, 0.832, is the rule tier's on this ledger.
- **The tier segments:**
  - rules only: 67 entries, 52 anomalies;
  - model only: 81 entries, 8 anomalies;
  - both: 22 entries, 22 anomalies;
  - neither: 4,968 entries, 38 anomalies.

So, by the registered reading, the model tier catches one of the five, `token_amount`, an
amount under three dollars on an ordinary pair, which `log_amount` sees by design, and none
of the other four. That is one ledger, one seed and 43 entries of patterns written for this
project, not real fraud. Any later change that moves these figures labels the new ones
"re-measured, no longer held out" (CLAUDE.md, rule 10).

**As printed.** The environments (one `python -c` line each, beside `git rev-parse HEAD`):

```text
605558173459cdf7bded05107a66eede7b215c86
ledgerlens 1.1.0 /Users/rio/Library/Mobile Documents/com~apple~CloudDocs/LedgerGen project/.claude/worktrees/ledgerlens-continue-cc0a92/src/ledgerlens/__init__.py
python 3.12.0 numpy 2.5.3 pandas 3.0.6 scikit-learn 1.9.1
ledgerlens 1.1.0 /Users/rio/Library/Mobile Documents/com~apple~CloudDocs/LedgerGen project/.claude/worktrees/ledgerlens-continue-cc0a92/src/ledgerlens/__init__.py
python 3.9.6 numpy 2.0.2 pandas 2.3.3 scikit-learn 1.6.1
```

`ledgerlens generate --held-out --out-dir data` (once, Python 3.12), then `shasum -a 256`:

```text
Wrote 10,276 lines / 5,138 entries to data/heldout-ledger.csv
Wrote 5,138 labels (120 anomalies, 2.34%, 43 of them held out) to data/heldout-labels.csv
36d021d2f20f65e2af2989ae965c18118e97b751df050715570e4731ea7ea524  data/heldout-ledger.csv
0acd09710470a5ec68928f769a2c23f1aeca9adeb75a42a8fbe5b213cd866822  data/heldout-labels.csv
```

`ledgerlens test data/heldout-ledger.csv --labels data/heldout-labels.csv` (Python 3.12; the
3.9 output is identical; until the tie fix made after the merge, Linux printed the tied rows
of two of its tables in another order, see the disclosures):

```text
Ran 12 test(s) over 5,138 entries
Raised 130 flag(s) on 89 entrie(s)

test_id                          test_name severity  flags
 JET-01                Round-dollar amount   Medium     11
 JET-02                    Weekend posting   Medium     15
 JET-03                After-hours posting   Medium      9
 JET-04                    Holiday posting   Medium      4
 JET-05          Period-end manual revenue     High     11
 JET-06      Just under approval threshold     High     12
 JET-07           Possible duplicate entry     High     10
 JET-08           Rare account combination   Medium      6
 JET-09           Dormant account activity   Medium      5
 JET-10                   Unbalanced entry     High      2
 JET-11                Large value outlier   Medium      4
 JET-12 Manual entry outside approver list      Low     41

Top 10 by risk score:
  JE-2025-005018  score  6.0  JET-01, JET-09, JET-11  $50,000.00
  JE-2025-005094  score  6.0  JET-08, JET-10, JET-12  $164.46
  JE-2024-005046  score  5.0  JET-02, JET-05  $5,130.38
  JE-2024-005049  score  5.0  JET-02, JET-05  $1,661.10
  JE-2024-005047  score  5.0  JET-02, JET-05  $1,175.95
  JE-2025-005062  score  5.0  JET-02, JET-07  $926.23
  JE-2024-005072  score  5.0  JET-02, JET-07  $859.95
  JE-2025-005081  score  5.0  JET-05, JET-09  $276.91
  JE-2024-005009  score  4.0  JET-01, JET-11  $100,000.00
  JE-2024-005013  score  4.0  JET-01, JET-11  $75,000.00

--- evaluation against ground truth ---
Read these sceptically. For eight of eleven archetypes the generator injects the anomaly using
the same definition the test looks for, so recall on those is close to tautological. The other
three, benford_drift, dormant_account and rare_account_pair, are not injected by exactly that
definition, so which test catches them depends on the draw: on the default ledger, mostly tests
written for something else. Their recall says how they happened to be injected, not how well
anything detects them.
Population           5,138 entries
Injected anomalies   120
Flagged              89 (1.73% of population)
  true positives     74
  false positives    15
  false negatives    46
Precision            0.832
Recall               0.617
F1                   0.708

Recall by archetype:
             anomaly_type  n  caught  missed  recall
               round_trip  8       0       8  0.0000
        shell_vendor_drip 10       0      10  0.0000
           split_purchase  9       0       9  0.0000
             token_amount  8       0       8  0.0000
          unfamiliar_user  8       0       8  0.0000
            benford_drift  4       2       2  0.5000
        rare_account_pair  7       6       1  0.8571
        after_hours_entry  9       9       0  1.0000
          dormant_account  4       4       0  1.0000
          duplicate_entry 10      10       0  1.0000
            holiday_entry  4       4       0  1.0000
     just_under_threshold  8       8       0  1.0000
period_end_manual_revenue  8       8       0  1.0000
             round_amount 11      11       0  1.0000
         unbalanced_entry  2       2       0  1.0000
            weekend_entry 10      10       0  1.0000

Caught by the test written for each archetype, or by others:
             anomaly_type intended_test  n  caught  by_intended_test  by_other_tests  only_by_other_tests  missed
             token_amount          none  8       0                 0               0                    0       8
          unfamiliar_user          none  8       0                 0               0                    0       8
           split_purchase          none  9       0                 0               0                    0       9
               round_trip          none  8       0                 0               0                    0       8
        shell_vendor_drip          none 10       0                 0               0                    0      10
             round_amount        JET-01 11      11                11               5                    0       0
            weekend_entry        JET-02 10      10                10               7                    0       0
        after_hours_entry        JET-03  9       9                 9               4                    0       0
            holiday_entry        JET-04  4       4                 4               3                    0       0
period_end_manual_revenue        JET-05  8       8                 8               3                    0       0
     just_under_threshold        JET-06  8       8                 8               6                    0       0
          duplicate_entry        JET-07 10      10                10               2                    0       0
        rare_account_pair        JET-08  7       6                 3               5                    3       1
          dormant_account        JET-09  4       4                 0               4                    4       0
            benford_drift          none  4       2                 0               2                    2       2
         unbalanced_entry        JET-10  2       2                 2               1                    0       0

Precision by test:
test_id                          test_name  entries_flagged  true_positives  precision
 JET-01                Round-dollar amount               11              11     1.0000
 JET-02                    Weekend posting               15              15     1.0000
 JET-03                After-hours posting                9               9     1.0000
 JET-04                    Holiday posting                4               4     1.0000
 JET-05          Period-end manual revenue               11              11     1.0000
 JET-07           Possible duplicate entry               10              10     1.0000
 JET-08           Rare account combination                6               6     1.0000
 JET-09           Dormant account activity                5               5     1.0000
 JET-10                   Unbalanced entry                2               2     1.0000
 JET-11                Large value outlier                4               4     1.0000
 JET-12 Manual entry outside approver list               41              30     0.7317
 JET-06      Just under approval threshold               12               8     0.6667
```

`ledgerlens score data/heldout-ledger.csv --labels data/heldout-labels.csv` (Python 3.12; the
3.9 output is identical):

```text
Isolation Forest fitted on 5,138 entries
  features supplied  16
  features used      14
  contamination      0.020 (sklearn's offset; the 0-1 score does not depend on it)
  dropped (constant) n_lines, posting_lag_days

Tier agreement across 5,138 entries:
agreement
neither       4968
model only      81
rules only      67
both            22

Unusual to the model but matching no rule (top 10):
  JE-2025-002981  model 0.915  $20,953.77  Monthly depreciation
  JE-2024-000347  model 0.882  $33.45  Monthly depreciation
  JE-2025-003321  model 0.833  $7,510.45  Monthly depreciation
  JE-2025-005102  model 0.828  $1.82  Payment received - customer Coral Trading
  JE-2024-001903  model 0.826  $2,819.87  Monthly depreciation
  JE-2024-005103  model 0.821  $1.71  Service revenue - customer Harborview Inc
  JE-2025-005099  model 0.820  $1.55  Invoice 8379 - customer Vista Foods
  JE-2024-002134  model 0.795  $35.58  Payment received - customer Sunbelt Markets
  JE-2024-001261  model 0.793  $81.83  Monthly depreciation
  JE-2025-003944  model 0.787  $3,553.44  Monthly depreciation

--- tier comparison ---
Read these sceptically. For eight of eleven archetypes the generator injects the anomaly using
the same definition the test looks for, so recall on those is close to tautological. The other
three, benford_drift, dormant_account and rare_account_pair, are not injected by exactly that
definition, so which test catches them depends on the draw: on the default ledger, mostly tests
written for something else. Their recall says how they happened to be injected, not how well
anything detects them.
The same circularity applies to the model tier: these anomalies were defined as rule violations,
so almost every anomaly the model ranks highly, the rules had already caught. Read the lift as
re-ranking of the rule tier's queue, not as independent detection. Only the held-out archetypes,
which no rule describes, measure it as a detector: how many of them its budget holds, against
chance.
   segment  entries  true_anomalies  precision  share_of_all_anomalies
rules only       67              52     0.7761                  0.4333
model only       81               8     0.0988                  0.0667
      both       22              22     1.0000                  0.1833
   neither     4968              38     0.0076                  0.3167

--- model lift over random selection ---
 top_n  true_anomalies  precision  base_rate  lift_vs_random
    25              16      0.640     0.0234            27.4
    50              22      0.440     0.0234            18.8
   100              29      0.290     0.0234            12.4
   200              37      0.185     0.0234             7.9

--- which archetypes the model can perceive ---
             anomaly_type  n  mean_model_score  vs_normal
             round_amount 11            0.9496     0.6500
             token_amount  8            0.7481     0.4485
            benford_drift  4            0.7174     0.4179
          dormant_account  4            0.7019     0.4024
         unbalanced_entry  2            0.6709     0.3714
        rare_account_pair  7            0.6391     0.3395
     just_under_threshold  8            0.5340     0.2344
        after_hours_entry  9            0.5267     0.2272
          unfamiliar_user  8            0.4780     0.1785
               round_trip  8            0.4707     0.1711
period_end_manual_revenue  8            0.4498     0.1502
           split_purchase  9            0.3975     0.0979
            weekend_entry 10            0.3667     0.0671
            holiday_entry  4            0.2813    -0.0183
          duplicate_entry 10            0.2812    -0.0184
        shell_vendor_drip 10            0.1616    -0.1380

--- each archetype against the model's budget ---
             anomaly_type  n  rule_caught  model_in_budget  chance  model_recall
             token_amount  8            0                6    0.16        0.7500
          unfamiliar_user  8            0                0    0.16        0.0000
           split_purchase  9            0                0    0.18        0.0000
               round_trip  8            0                0    0.16        0.0000
        shell_vendor_drip 10            0                0    0.20        0.0000
             round_amount 11           11               11    0.22        1.0000
            weekend_entry 10           10                0    0.20        0.0000
        after_hours_entry  9            9                2    0.18        0.2222
            holiday_entry  4            4                0    0.08        0.0000
period_end_manual_revenue  8            8                1    0.16        0.1250
     just_under_threshold  8            8                0    0.16        0.0000
          duplicate_entry 10           10                0    0.20        0.0000
        rare_account_pair  7            6                2    0.14        0.2857
          dormant_account  4            4                3    0.08        0.7500
            benford_drift  4            2                3    0.08        0.7500
         unbalanced_entry  2            2                2    0.04        1.0000
Held-out archetypes, which no test describes: 6 of 43 in the budget (103 of 5,138 entries), against 0.86 by chance (7.0x).
```

**What it can and cannot say.** The five archetypes are patterns written for this project;
the model's result says how an Isolation Forest over these entry-level features perceives them,
not how it would do on real fraud. Some are visible to a feature by design (an unfamiliar
user to `user_freq`, a token amount to `log_amount`); some exist only between entries (a
split purchase, a round trip, a drip), which entry-level features cannot see, as with the
duplicate archetype on the default ledger.

## Disclosures

- **What was seen before the freeze.** The week-5 plan (2026-10-08) records that the
  exploration behind it looked at development-seed model results for two candidate
  archetypes, and that every rule-silent candidate was kept regardless of them; it does not
  record which two. While the recipes were written no test or probe computed a model score
  on a held-out recipe whose output was read: a test of what `score` prints for held-out
  labels relabels normal entries instead. One smoke run, on development seed 101, ran
  `generate --held-out`, `test`, `score`, `summary` and `report` on both Pythons and kept only
  their exit codes (all 0), so that a command that would fail on a held-out ledger was found
  while it could still be fixed; their output was discarded unread. The review read the code
  without scoring a held-out ledger. One recipe changed during development, for
  correctness: two drips could draw the same vendor, which reads as one drip of ten
  payments, so a drip now draws its vendor without replacement.
- **The seed.** `HELD_OUT_SEED` arrives in the same commit as the held-out code (`d328a90`),
  so "never generated with before" rests on the tests' guard and on what this document
  says was run, not on the history alone.
- **Tie order in two printed tables, found after the freeze, fixed after the merge.** `test`
  printed its "Recall by archetype" and "Precision by test" tables sorted on one key with
  pandas' default sort, which is not stable. So rows that tie printed in another order on CI's
  Linux runners than on the Mac that made the measurement: the five held-out archetypes at
  0.0000 and the nine at 1.0000, and on Python 3.9 JET-10 and JET-11 at 1.0000. Every row's
  values were the same, and `score`'s output, the model's figures included, was the same byte
  for byte. The sorts are in `src/`, so by rule 10 the defect was disclosed here and not fixed
  until the measurement was merged, and until then `tests/test_limitations.py` compared those
  two tables' rows regardless of their order. After the merge, before v1.1.0 was tagged, the
  two sorts were given a tie-break: by name and by test id, the order the Mac had printed. The
  output above is unchanged, its order no longer depends on the platform's sort, and the test
  compares all of it. No value moved.
- **Ties.** `model_lift` gives a tie at its cut to the lower entry id; `combine` puts every
  entry tied at the budget's cutoff inside it, so the budget can hold slightly more than 2%.
- **The narration prompt.** Its system prompt says the risk score counts "distinct tests";
  it is a severity-weighted sum over flags, and JET-09 and JET-11 judge lines, so one entry
  can count one test twice. The narrative eval's grader hash does not cover the system
  prompt, so changing it would date the published eval without saying so; it is left, and
  said here.
- **Review rows are filed by ledger, not by the controls they were made under.** After a
  controls change, an entry with a decision stays out of the queue whatever it is now
  flagged for, and `narrate` skips an entry with a note. Recording the controls on each row
  would be a schema change; the maintainer decided on 2026-10-18 to keep it documented (the
  README says to set the controls before review starts).
- **The model on older notes.** Before `narrate.response_model`, a note recorded the model the
  request named, not the one the API response reported, and an alias can resolve to a dated
  snapshot. Notes written since record the response's. No column says which a row holds (adding
  one is a review-schema change), so an older row is told only by its `generated_at`; the rows
  are append-only and are not rewritten. The narrative eval's rows still record the model
  requested, the one its runs directory is named for.

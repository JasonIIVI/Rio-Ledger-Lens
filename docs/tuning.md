# Tuning notes

One threshold in LedgerLens, JET-11's, was chosen by measuring against the labelled ledger.
Every other test's setting is swept below on the same ledger, and none was changed: most
are flat across the range swept, or sit at the edge of the generator's own definition,
because the generator injects most archetypes by the definition their test checks. This
ledger cannot choose them. The tables are computed by `tests/test_tuning.py` and looked
for here, so a figure that moves fails a test. Every table counts flags and entries
separately, precision per entry as `evaluate.precision_by_test` reports it, and, for a
test written for an archetype, how many of that archetype's entries it catches.

## JET-11 — large value outlier

The test scores each entry against its own account's distribution using a robust
(median/MAD) z-score.

**First attempt scored raw dollar amounts, and fired 119 times on a six-month ledger.**
That is a useless test: transaction values are lognormal, so a robust z-score on raw
dollars flags the entire natural right tail of the distribution. The fix was to score
`log10(amount)` instead, which is the scale the data actually lives on.

Sweeping the threshold on the two-year ledger (5,085 entries, 77 injected anomalies).
The test judges lines, so one entry can raise it twice: the table counts both, and
precision is per entry, as `evaluate.precision_by_test` reports it.

| z threshold | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 2.5 | 92 | 58 | 23 | 0.40 | — |
| 3 | 19 | 14 | 7 | 0.50 | — |
| **3.5** | 8 | 7 | 6 | 0.86 | — |
| 4 | 4 | 4 | 4 | 1.00 | — |
| 4.5 | 3 | 3 | 3 | 1.00 | — |
| 5 | 0 | 0 | 0 | — | — |

No archetype was written for JET-11, so the last column is empty.

(An earlier version of this table divided the anomalous entries by the flags, which
mixes the two counts: 0.25, 0.37 and 0.75 in the first three rows.)

**3.5 was chosen.** 4.0 and above look perfect on precision but are catching only the
most extreme handful; a test that fires four times across two years is not earning its
place in the workpaper. 3.5 keeps six of its seven entries real while surfacing
meaningfully more. A test that never fires is as useless as one that always does.

## The other settings, swept

Swept on the same ledger with `evaluate.sweep`, the default in bold. None was changed:
changing one would move every published figure, and on this ledger it would be tuning
against the generator's construction rather than against anything a ledger does.

### JET-01 — round-dollar amount (`min_amount`)

| min_amount | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 250 | 11 | 11 | 11 | 1.00 | 11 of 11 |
| 500 | 11 | 11 | 11 | 1.00 | 11 of 11 |
| **1000** | 11 | 11 | 11 | 1.00 | 11 of 11 |
| 5000 | 11 | 11 | 11 | 1.00 | 11 of 11 |
| 10000 | 11 | 11 | 11 | 1.00 | 11 of 11 |

Flat. The injector's round amounts are $10,000 and up here and no other amount is a whole
thousand, so any floor up to $10,000 gives the same eleven.

### JET-03 — after-hours posting (`start_hour, end_hour`)

| start_hour-end_hour | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 4-22 | 7 | 7 | 7 | 1.00 | 7 of 9 |
| 5-21 | 9 | 9 | 9 | 1.00 | 9 of 9 |
| **6-20** | 9 | 9 | 9 | 1.00 | 9 of 9 |
| 7-19 | 9 | 9 | 9 | 1.00 | 9 of 9 |
| 8-18 | 9 | 9 | 9 | 1.00 | 9 of 9 |

Flat from 5-21 to 8-18. Normal entries are keyed between 08:00 and 17:59 and the
injected ones between 22:00 and 04:59, so any window in between separates them; 4-22
misses the two keyed at 4 a.m.

### JET-05 — period-end manual revenue (`days`)

| days | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 3 | 3 | 1.00 | 2 of 8 |
| 2 | 5 | 5 | 5 | 1.00 | 4 of 8 |
| **3** | 9 | 9 | 9 | 1.00 | 8 of 8 |
| 4 | 9 | 9 | 9 | 1.00 | 8 of 8 |
| 5 | 9 | 9 | 9 | 1.00 | 8 of 8 |

The injector posts in a month's last three days, which is the default: fewer days miss
some, more add nothing. The ninth entry is another archetype's.

### JET-06 — just under the approval limit (`window`)

| window | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 0.01 | 4 | 4 | 4 | 1.00 | 4 of 8 |
| 0.02 | 6 | 6 | 6 | 1.00 | 6 of 8 |
| **0.03** | 8 | 8 | 8 | 1.00 | 8 of 8 |
| 0.05 | 8 | 8 | 8 | 1.00 | 8 of 8 |
| 0.1 | 13 | 13 | 8 | 0.62 | 8 of 8 |

The limit itself is the company's (a controls file, else $10,000) and is not swept. The
injector draws amounts up to $250 (2.5%) under the limit, so 3% is the narrowest window
swept that holds all eight; 10% adds five entries that are not anomalies.

### JET-07 — possible duplicate entry (`days`)

| days | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 1 | 6 | 6 | 6 | 1.00 | 6 of 10 |
| 3 | 10 | 10 | 10 | 1.00 | 10 of 10 |
| **7** | 10 | 10 | 10 | 1.00 | 10 of 10 |
| 14 | 10 | 10 | 10 | 1.00 | 10 of 10 |
| 30 | 10 | 10 | 10 | 1.00 | 10 of 10 |

The injector re-posts one to three days after the original, so any window from three
days up gives the same ten.

### JET-08 — rare account combination (`max_occurrences`)

| max_occurrences | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 1 | 5 | 5 | 5 | 1.00 | 3 of 7 |
| 2 | 5 | 5 | 5 | 1.00 | 3 of 7 |
| **3** | 5 | 5 | 5 | 1.00 | 3 of 7 |
| 5 | 13 | 13 | 13 | 1.00 | 7 of 7 |
| 10 | 13 | 13 | 13 | 1.00 | 7 of 7 |

The one that moves. At 3 it catches three of the seven rare pairs; at 5 all seven, with
no false positive, because four of the seven drew the same odd pair, which then occurs
four times and is no longer rare at 3. Choosing 5 would fit the generator's draw, not a
ledger, so the default stays.

### JET-09 — dormant account activity (`dormant_days`)

| dormant_days | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 30 | 9 | 9 | 9 | 1.00 | 0 of 4 |
| 60 | 6 | 6 | 6 | 1.00 | 0 of 4 |
| 90 | 4 | 4 | 4 | 1.00 | 0 of 4 |
| **120** | 4 | 4 | 4 | 1.00 | 0 of 4 |
| 180 | 2 | 2 | 2 | 1.00 | 0 of 4 |

It catches none of the dormant-account entries at any setting: they land on accounts
with no earlier activity, or soon after it, so no gap precedes them. What it flags,
precision 1.00 throughout, is other archetypes' entries on accounts no normal entry uses.
The setting cannot be judged on this ledger.

### JET-10 — unbalanced entry (`tolerance`)

| tolerance | flags raised | entries flagged | of them injected anomalies | precision (per entry) | its own archetype caught |
|---:|---:|---:|---:|---:|---:|
| 0.001 | 2 | 2 | 2 | 1.00 | 2 of 2 |
| **0.005** | 2 | 2 | 2 | 1.00 | 2 of 2 |
| 0.01 | 2 | 2 | 2 | 1.00 | 2 of 2 |
| 0.1 | 2 | 2 | 2 | 1.00 | 2 of 2 |
| 1 | 2 | 2 | 2 | 1.00 | 2 of 2 |

Flat. The injector unbalances an entry by $1 to $500, which every tolerance swept sees.

## JET-12 — manual entry outside approver list

Left deliberately noisy. It produces 13 of the 14 false positives in the default run,
and excluding it the rule layer produces exactly one false positive across the whole
population.

This is not a defect. JET-12 describes the control environment — how often people
outside the approved list key manual entries — rather than making a claim about any
individual entry. The generator posts 94% of manual entries from authorised users and
6% from others, which is roughly what a small company's ledger looks like in practice.

The right handling is to report it separately rather than to suppress it, which is what
`evaluate.precision_by_test` is for.

The approver list itself is not tuned and is not a threshold. It is a fact about the
company, as is JET-06's approval limit: both are read from a `<stem>.controls.json` file
beside the ledger, and default to the synthetic company's (`controller` and `mchen`;
$10,000). JET-06's 3% window below the limit is the test's own.

## Benford: MAD versus chi-square

Both are reported. Chi-square is what textbooks use; MAD (with Nigrini's conformity
bands) is what holds up at audit scale.

On the default ledger the two disagree — MAD says close conformity at 0.0035, while
chi-square at 15.59 exceeds its 15.507 critical value. The population is visibly
conformant. Chi-square's power grows with sample size, so on a large population it
rejects conformity over trivial deviations.

Reporting only one of them would be defensible. Reporting only chi-square would be
wrong.

## Sample size floor

The Benford module refuses to treat fewer than 300 observations as meaningful, and
`segmented_benford` skips any segment below that. Nigrini suggests 300–500 as the
practical floor for the first-digit test; below it the digit proportions are too noisy
to interpret.

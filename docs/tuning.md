# Tuning notes

Thresholds in LedgerLens were chosen by measuring against the labelled ledger, not by
picking round numbers. This file records the measurements so the choices can be argued
with.

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

| z threshold | flags raised | entries flagged | of them injected anomalies | precision (per entry) |
|---:|---:|---:|---:|---:|
| 2.5 | 92 | 58 | 23 | 0.40 |
| 3.0 | 19 | 14 | 7 | 0.50 |
| **3.5** | **8** | **7** | **6** | **0.86** |
| 4.0 | 4 | 4 | 4 | 1.00 |
| 4.5 | 3 | 3 | 3 | 1.00 |
| 5.0 | 0 | 0 | 0 | — |

(An earlier version of this table divided the anomalous entries by the flags, which
mixes the two counts: 0.25, 0.37 and 0.75 in the first three rows.)

**3.5 was chosen.** 4.0 and above look perfect on precision but are catching only the
most extreme handful; a test that fires four times across two years is not earning its
place in the workpaper. 3.5 keeps six of its seven entries real while surfacing
meaningfully more. A test that never fires is as useless as one that always does.

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

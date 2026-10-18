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
JET-03, 05, 06 and 07 are flat over most of it, the default at or inside the edge of the
injector's own definition; JET-08 and JET-09 move, for reasons the generator explains, not a
ledger.

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
held-out entries. So the measured ledger's figures for the eleven old archetypes include
whatever its held-out lines do to JET-11, and the measurement is of the model tier: how many
of the held-out entries its budget (the top 2% of entries by rank, ties included) holds,
against chance.

**How it is made, once.** The code was frozen at the commit tagged `held-out-freeze`.
From that commit, `ledgerlens generate --held-out --out-dir data` (seed `HELD_OUT_SEED`,
never generated with before; the tests use other seeds, and fail if anything generates
with it), then `ledgerlens test` and `ledgerlens score`, each with
`--labels data/heldout-labels.csv`. No `heldout-ledger.controls.json` sits beside it (`test`
would print a "Controls from" line): it is measured against the synthetic company's
controls. It runs in the Python 3.12 environment and again in the 3.9 one; both outputs are
pasted below as printed, with the freeze commit, the date and the versions (Python, numpy,
pandas, scikit-learn and ledgerlens, printed by one `python -c` line in each), and if the two
differ in any figure, that is said rather than one of them chosen. Nothing is tuned on it afterwards: a later change
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
| `token_amount` | 8 | | | |
| `unfamiliar_user` | 8 | | | |
| `split_purchase` | 9 | | | |
| `round_trip` | 8 | | | |
| `shell_vendor_drip` | 10 | | | |
| all held-out | 43 | | | |

*To be filled in from the measurement, as printed.*

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

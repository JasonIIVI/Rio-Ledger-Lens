# Methodology

## Population, not sample

Traditional substantive testing samples journal entries. Every entry in scope can be
tested instead when the tests are cheap enough, which changes the question from "is
this sample representative?" to "which of these 5,000 entries deserve an hour of
someone's time?"

That reframing is what the risk score is for. It does not decide anything; it orders
a queue.

## The two tiers

**Tier 1 — deterministic tests.** Twelve rules, each describing a pattern an auditor
can name and defend. Cheap, explainable, and reproducible. Their weakness is that they
only find what someone thought to describe.

**Tier 2 — unsupervised scoring** (week 2). An Isolation Forest over engineered
features, to surface entries that are unusual without matching any written rule. Its
weakness is the mirror image: it cannot explain itself, which is why its score is kept
separate rather than blended into the rule score.

The two tiers disagreeing is informative. An entry flagged by rules but not by the
model is probably a known pattern; an entry the model dislikes that no rule caught is
the interesting case.

**Tier 3 — narratives** (week 3). Claude writes the note a reviewer would write before
opening an entry: what it is, why it surfaced, what evidence resolves it, which control it
touches, and how likely it warrants investigation. The note is advisory text in a strict JSON
contract. The model never sees the labels, never changes a score, and never decides.

## The human loop

A flag on its own is noise; a flag that somebody looked at, judged, and signed their name to
is evidence. Decisions (accept / dismiss / escalate) are recorded by a named reviewer in an
append-only store: changing your mind adds a decision rather than editing one, because an audit
trail that can be silently edited is not an audit trail. The workpaper carries the note and the
latest decision per exception, and the MCP server exposes all of it read-only. Every decision
and note is filed under the ledger it belongs to (a canonical hash of a CSV, or a QuickBooks
realm id), so one review database can hold several ledgers without a decision ever attaching
to the wrong company's entry.

## Scoring

The composite score is a severity-weighted sum over the flags raised:

```
score = Σ weight(severity of the test that raised each flag)
weights: High = 3.0, Medium = 2.0, Low = 1.0
```

JET-09 and JET-11 judge lines, not entries, so a test that flags two lines of one entry
counts twice (one entry of the default ledger scores 4.0 for a single JET-11). The
`n_flags` column beside the score counts distinct tests.

Deliberately simple. A reviewer can reconstruct any score by hand from the flags shown
next to it. A more sophisticated weighting would buy marginal ranking accuracy at the
cost of the one property that makes a reviewer trust the queue.

## Ground truth and evaluation

The generator writes labels to a separate file. Nothing in the detection path reads
them. `evaluate` joins them back afterwards to compute:

- **precision** — when it speaks up, is it right?
- **recall** — how much does it miss?
- **recall by archetype** — *which kinds* of thing does it miss? (the aggregate hides this)
- **precision by test** — which individual tests are trustworthy?

For audit work recall generally matters more than precision: a missed misstatement is
worse than a wasted half hour. But precision decides whether anyone keeps using the
tool after week one.

## Known circularity

Eight of the eleven archetypes are injected using the same definition the corresponding
test looks for, so their recall is near-tautological. This is stated plainly in the
README rather than buried, because the alternative — quoting an unqualified 0.97 recall
— would be misleading.

The other three (Benford drift, dormant accounts, rare account pairs) are not definitional,
but they do not measure the rule layer either. Which test catches them depends on the draw
(on seed 101 JET-08 catches all seven rare pairs). On the default ledger the test written for
each catches half of them or fewer, and tests written for something else catch the rest: JET-12 catches the
Benford-drift entries keyed outside the approver list (the injector picks the keyer at
random), and no test reads their amounts; JET-09 catches none of the dormant-account
entries, which land on rarely used accounts rather than on ones that went quiet; JET-08
catches three of the seven rare pairs. `evaluate.attribution` counts this, and
`ledgerlens test --labels` prints it. Measuring detection needs anomalies no rule describes.

## The held-out ledger

Five archetypes no test describes (`schema.HeldOutType`) are injected only into an opt-in
ledger, `generate_ledger(held_out=True)`. They draw from their own random streams and are
numbered after every other entry, so the rest of that ledger is exactly the one the same seed
gives without them, and the default ledger does not change at all. Each is built to be
rule-silent: a business day and hour, a common pair whose source is neither Manual nor
System, cents, nothing in the 3% beneath the approval limit, balanced. That silences JET-01 to
06, 10 and 12 by construction; JET-07, 08, 09 and 11 read the population, so the tests check,
on four development seeds, that no rule fires on a held-out entry and that every other entry
keeps the flags it had (on other seeds the held-out lines can move another entry's JET-11
flag: `docs/limitations.md`).

So the rule tier is built to catch none of them, and its recall on them measures nothing. The model tier is
measured as a share of its budget: an archetype of n entries puts n x B / P of them in a
budget of B entries out of P by chance (`evaluate.budget_recall_by_archetype`,
`evaluate.held_out_budget`). The ledger is generated once, from the commit tagged
`held-out-freeze`, with `HELD_OUT_SEED`, which no code had used before; what is reported was
registered in `docs/limitations.md` before it existed.

## The QuickBooks connector

A pull is not a second pipeline. `pull-qbo` maps a period of a QuickBooks Online company into
the same column contract a CSV has and hands it to `ingest.prepare`, so every test, both tiers,
the review store and the workpaper read it exactly as they read the synthetic ledger.

Two sources are used because neither is enough alone. Manual journal entries come from the
`JournalEntry` entity, whose lines are complete; every other transaction (an invoice, a bill,
a payment) is rebuilt from the General Ledger report, which lists each posting under the
account it hits, so grouping its rows by transaction gives the entry back whole and balanced.
The report also supplies what the entity lacks: who created each entry.

The keying time matters to three tests (weekend, after-hours, holiday), so it is taken from
QuickBooks' own creation stamp rather than from the posting date. Where QuickBooks gives only
a date, or a stamp that cannot be read, the time is set to noon, which fires no after-hours
flag of its own, and the line is marked `entered_at_estimated` so the estimate is visible
rather than silent. In the recorded sandbox quarter no line needed it.

What a pull does not have is ground truth. Nothing about a sandbox company is labelled, the
thresholds are the ones set for the generator, the approver list and approval limit are the
synthetic company's unless a controls file beside the pull states its own (see below), and a
quarter of a small company is far too
few entries for the model tier or Benford analysis. Its flags show the tool running on a
second source; they measure nothing.

## A company's own controls

JET-12 asks whether a manual entry was keyed by someone on the approver list, and JET-06
whether an entry sits within 3% beneath the approval limit. The list and the limit are facts
about a company, read from a `<stem>.controls.json` file beside its ledger; with no file they
are the synthetic company's (`controller` and `mchen`; $10,000), which the generator's ledger
was built against. They are not thresholds, and nothing tunes them; the 3% window stays the
test's own. A file that is present but cannot be used is refused
rather than replaced by the defaults, because testing one company against another's approver
list raises a flag on every manual entry and explains none of them. Every surface reads the
same file and names it, and the model tier reads neither the list nor the limit. Review notes
and decisions are filed by ledger, not by controls, so the controls are best set before review
starts: an entry decided under one approver list keeps its decision under the next.

## References

- AU-C 240, *Consideration of Fraud in a Financial Statement Audit* — journal entry
  testing requirements
- Nigrini, M., *Benford's Law: Applications for Forensic Accounting, Auditing, and
  Fraud Detection* — MAD conformity bands
- *Using Benford's Law to reveal journal entry irregularities*, Journal of Accountancy,
  September 2022

## Measuring the narratives

Seventeen entries - sixteen chosen deterministically from the default ledger, and one written by
hand: an ordinary entry whose line description is replaced, before the prompt is built, with an
instruction addressed to the reviewer's AI, the ledger file untouched - with expectations written
by hand from each entry's own data before any run: facts the note must mention, wording it must
not use, and the confidence band a careful reviewer would choose. The grader checks properties,
not similarity to a reference narrative, because a reference would itself be model-written and
scoring similarity to it rewards imitation. The properties are floors: a note can satisfy all
of them and still be unhelpful. Results live in `docs/narrative-eval.md`.

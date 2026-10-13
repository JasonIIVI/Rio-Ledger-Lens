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

Nine of the eleven archetypes are injected using the same definition the corresponding
test looks for, so their recall is near-tautological. This is stated plainly in the
README rather than buried, because the alternative — quoting an unqualified 0.97 recall
— would be misleading.

The archetypes where detection is *not* definitional (Benford drift, rare account
pairs) score 0.75 and 0.86 respectively, and are the honest measure of the rule layer.

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

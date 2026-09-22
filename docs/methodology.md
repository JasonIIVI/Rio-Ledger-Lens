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

## Scoring

The composite score is a severity-weighted count of distinct tests that fired:

```
score = Σ weight(severity of each distinct test that fired)
weights: High = 3.0, Medium = 2.0, Low = 1.0
```

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

## References

- AU-C 240, *Consideration of Fraud in a Financial Statement Audit* — journal entry
  testing requirements
- Nigrini, M., *Benford's Law: Applications for Forensic Accounting, Auditing, and
  Fraud Detection* — MAD conformity bands
- *Using Benford's Law to reveal journal entry irregularities*, Journal of Accountancy,
  September 2022

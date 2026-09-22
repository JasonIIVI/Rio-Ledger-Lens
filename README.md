# LedgerLens

Audit analytics over general ledger data: journal-entry tests, Benford's Law digit
analysis, and risk-scored exceptions with plain-English reasons a reviewer can act on.

[![CI](https://github.com/JasonIIVI/Rio-Ledgens-Lens/actions/workflows/ci.yml/badge.svg)](https://github.com/JasonIIVI/Rio-Ledgens-Lens/actions/workflows/ci.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## What this is

Audit software at the large firms — KPMG Clara, PwC Halo, EY Canvas, Deloitte Argus —
tests the entire population of journal entries rather than a sample, and hands the
auditor a ranked list of exceptions to investigate. LedgerLens is a small, readable,
open implementation of that idea.

It ships with a **labelled synthetic ledger generator**, so every claim about detection
performance in this README can be reproduced with one command. That is the whole reason
the generator exists: without ground truth, "it flagged some things" is not a result.

```
ledger CSV ──▶ ingest/validate ──▶ 12 journal-entry tests ──▶ risk-scored exceptions
                     │                      │
                     └──▶ Benford analysis ─┘
```

## Quickstart

```bash
git clone https://github.com/JasonIIVI/Rio-Ledgens-Lens.git
cd Rio-Ledgens-Lens
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ledgerlens generate                                   # writes data/ledger.csv + labels.csv
ledgerlens test data/ledger.csv --labels data/labels.csv
ledgerlens benford data/ledger.csv --by account_code
```

## The tests

| ID | Test | Severity | What it asks |
|---|---|---|---|
| JET-01 | Round-dollar amount | Medium | Real invoices carry cents. Why is this one exactly $50,000? |
| JET-02 | Weekend posting | Medium | Why could this not wait for a business day? |
| JET-03 | After-hours posting | Medium | Does this match the user's normal working pattern? |
| JET-04 | Holiday posting | Medium | The office was closed. Who keyed this, and why? |
| JET-05 | Period-end manual revenue | **High** | Manual credit to revenue near period close — the AU-C 240 pattern. |
| JET-06 | Just under approval threshold | **High** | Structured beneath a control limit? |
| JET-07 | Possible duplicate entry | **High** | Same amount, accounts and description, days apart. |
| JET-08 | Rare account combination | Medium | This pairing occurs almost nowhere else in the population. |
| JET-09 | Dormant account activity | Medium | Quiet for months, then suddenly live. |
| JET-10 | Unbalanced entry | **High** | A conforming ERP cannot produce this. A control failed, or the extract is wrong. |
| JET-11 | Large value outlier | Medium | Far larger than this account normally sees (robust z on a log scale). |
| JET-12 | Manual entry outside approver list | Low | Segregation of duties: who is allowed to touch the ledger directly? |

Each flag carries a written `reason`, because a number with no explanation just moves
the work to the reviewer. Example output:

```
JE-2025-004431  score  5.0  JET-05, JET-01  $75,000.00
  Manual credit of $75,000.00 to Sales Revenue on 2025-09-30, within 3 days of period
  close. Obtain support and test cut-off. | Entry total is exactly $75,000 - a round
  thousand. Confirm the basis for the amount and whether it is an estimate or accrual.
```

## Results, and the caveat that matters more than the results

Against the default two-year synthetic ledger (5,085 entries, 77 injected anomalies at
1.5%):

| Metric | Value |
|---|---|
| Precision | **0.843** |
| Recall | **0.974** |
| F1 | **0.904** |
| Flag rate | 1.75% of population |

**Read those numbers sceptically, because they are partly circular.** For nine of the
eleven anomaly archetypes, the generator injects the anomaly using the same definition
the test looks for. Weekend entries are injected as weekend entries, and the weekend
test finds weekend entries. Recall near 1.0 on those archetypes is a tautology, not an
achievement.

The parts that are *not* circular, and are therefore the parts worth discussing:

- **Benford drift: 0.75 recall.** Nothing defines these entries as anomalous except
  the statistical shape of their amounts. One of four slipped through.
- **Rare account pairs: 0.86 recall.** The baseline is learned from the population,
  not hardcoded, so the test can genuinely miss.
- **JET-12 produces 13 of the 14 false positives.** Excluding it, the rule layer
  generates exactly **one** false positive across 5,085 entries. That is not a bug:
  JET-12 describes the *control environment* rather than any individual entry, and a
  test that fires on legitimate activity is still telling you something true. But a
  reviewer deserves to know which flags are which before spending an afternoon on the
  queue, which is why `precision_by_test` exists.
- **JET-11's threshold was tuned empirically**, not chosen for roundness — see
  [docs/tuning.md](docs/tuning.md).

The genuinely hard detection problem — finding anomalies nobody wrote a rule for —
is what the unsupervised layer in week 2 is for. This tier is deliberately the boring,
defensible one.

### A worked example of why the statistics need care

Running Benford over the full ledger:

```
MAD         0.00351  (close conformity)
chi-square  15.59    (critical 15.507 at 5%, 8 df) -> exceeds
```

The two statistics disagree on a population that is visibly conformant (observed 28.99%
of leading 1s against an expected 30.10%). Chi-square rejects conformity on almost any
large population because its power grows with sample size, which is precisely why
Nigrini's MAD bands are the standard in audit practice and why LedgerLens reports both.
Quoting only the chi-square here would produce a confident, wrong conclusion.

## Design decisions

- **Deterministic tier first.** Rules are cheap, explainable, and survive a reviewer
  asking "why". The ML layer is additive, and gets its own score rather than being
  blended into this one.
- **Labels live in a separate file** from the ledger. Detection code never sees them;
  only `evaluate` joins them back. It is otherwise far too easy to leak ground truth
  into a feature.
- **Amounts are lognormal.** That is what makes real ledgers Benford-conformant, so
  the digit test is measured against a legitimate baseline rather than a rigged one.
- **Severity is assigned per test, not per finding**, and the composite score is a
  severity-weighted count a reviewer can reconstruct by hand.
- **A flag is a question, not a finding.** Every `reason` string is worded that way
  on purpose.

## Roadmap

- [x] **Week 1** — synthetic generator, ingest/validation, 12 journal-entry tests,
      Benford analysis, evaluation harness, CLI, 61 tests, CI
- [ ] **Week 2** — Isolation Forest anomaly score, Streamlit review dashboard,
      Excel workpaper export
- [ ] **Week 3** — LLM-written exception narratives (structured JSON, with an eval
      set), reviewer queue with approve / dismiss / escalate, MCP server
- [ ] **Week 4** — QuickBooks Online connector (sandbox), scheduled re-run via
      GitHub Actions

## Limitations

- Synthetic data cannot capture adaptive behaviour: real fraud adjusts to the controls
  looking for it.
- Two-line journal entries only; multi-line allocations are generated but not yet
  modelled with realistic complexity.
- Thresholds are tuned against this generator. Against a real ledger they are a
  starting point, not a configuration.
- Nothing here constitutes an audit procedure or professional advice. It is a
  demonstration of technique.

## Development

```bash
pytest -q                    # 61 tests
pytest --cov=ledgerlens      # coverage (currently 97%)
ruff check src tests         # lint
```

## License

MIT — see [LICENSE](LICENSE).

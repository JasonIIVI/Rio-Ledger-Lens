# LedgerLens

Audit analytics over general ledger data: journal-entry tests, Benford's Law digit
analysis, and risk-scored exceptions with plain-English reasons a reviewer can act on.

[![CI](https://github.com/JasonIIVI/Rio-Ledger-Lens/actions/workflows/ci.yml/badge.svg)](https://github.com/JasonIIVI/Rio-Ledger-Lens/actions/workflows/ci.yml)
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
                        ┌──▶ 12 journal-entry tests ──┐
ledger CSV ──▶ ingest ──┼──▶ Benford analysis ────────┼──▶ exception queue ──▶ Claude note ──▶ reviewer ──▶ Excel workpaper
  or QuickBooks         └──▶ Isolation Forest ────────┘     (Streamlit)      (advisory JSON)  (append-only)
                                                                  └───▶ MCP server (read-only) ───▶ Claude Desktop
```

The two detection tiers are scored **separately and never blended**. They answer different
questions, and where they disagree is the most informative output the tool produces.

## Quickstart

```bash
git clone https://github.com/JasonIIVI/Rio-Ledger-Lens.git
cd Rio-Ledger-Lens
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ledgerlens generate                                   # writes data/ledger.csv + labels.csv
ledgerlens test data/ledger.csv --labels data/labels.csv      # rule tier
ledgerlens score data/ledger.csv --labels data/labels.csv     # both tiers, compared
ledgerlens benford data/ledger.csv --by account_code
ledgerlens report data/ledger.csv --out out/workpaper.xlsx    # Excel workpaper

pip install -e ".[app]" && streamlit run app.py               # review dashboard

cp .env.example .env                                          # add ANTHROPIC_API_KEY
pip install -e ".[llm]" && ledgerlens narrate data/ledger.csv # Claude-written reviewer notes
ledgerlens eval-narratives data/ledger.csv                    # grade them (docs/narrative-eval.md)
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

## The second tier, and what it is actually worth

Week 2 added an Isolation Forest over 16 engineered per-entry features. No feature encodes a
rule's answer - day-of-week is cyclical rather than a weekend flag - so the two tiers stay
genuinely independent. Two features (`n_lines`, `posting_lag_days`) are constant in the current
generator and are dropped automatically at fit time.

Putting both tiers side by side on the default ledger:

| Segment | Entries | Truly anomalous | Precision |
|---|---:|---:|---:|
| **both tiers agree** | 30 | 29 | **0.967** |
| rules only | 59 | 46 | 0.780 |
| model only | 72 | 1 | 0.014 |
| neither | 4,924 | 1 | 0.000 |

**The model is a strong re-ranker and a weak independent detector** - and that is worth saying
plainly rather than hiding behind a combined number. By rank it is far better than chance:

| Top N by model score | Anomalies found | Precision | Lift vs random |
|---:|---:|---:|---:|
| 25 | 16 | 0.64 | **42x** |
| 50 | 22 | 0.44 | 29x |
| 100 | 30 | 0.30 | 20x |

But almost everything it ranks highly, the rules had already caught. The "model only" segment is
essentially the base rate.

The reason is the same circularity described above: these anomalies were *defined* as rule
violations, so a model forbidden from encoding those rules has little left to find. Breaking
that is the explicit goal of the next milestone - injecting archetypes no rule describes, and
re-measuring both tiers against them.

What the model *can* perceive is visible per archetype:

| Archetype | Mean model score | vs normal baseline |
|---|---:|---:|
| round_amount | 0.915 | +0.63 |
| unbalanced_entry | 0.765 | +0.48 |
| benford_drift | 0.724 | +0.44 |
| weekend_entry | 0.364 | +0.08 |
| duplicate_entry | 0.330 | +0.04 |

Duplicates are near-invisible, correctly: a duplicate only exists by comparison with another
entry, and these are per-entry features. That is a design consequence, not a failure, and there
is a test asserting it stays true.

One honest false-positive pattern: the model repeatedly flags system-posted depreciation entries,
because `system` is a rare `created_by` value. Structurally unusual, operationally boring - the
kind of flag a reviewer dismisses in seconds, and the reason `precision_by_test` exists.

## The narrative layer and the human loop

Week 3 adds the part 2026 audit recruiters actually screen for: reading and questioning
AI-flagged exceptions. Claude writes the note a reviewer would otherwise write before opening
an entry, and a named reviewer records what they decided.

**What the model writes.** For each flagged entry it receives the entry's lines and every test
that fired, with the reason, and returns strict JSON:

```json
{"summary": "...", "why_flagged": "...", "evidence_to_request": ["..."],
 "suggested_control": "...", "confidence": "high | medium | low"}
```

The contract is enforced twice: the API is asked for that schema (structured outputs), and
`narrate.validate` checks the result again before it is stored. The system prompt carries the
audit meaning of all twelve tests, which is what makes the notes specific - and, as a side
effect, what makes the prompt long enough for the API to cache; `ledgerlens narrate` prints the
cache-read share so that claim is checked on every run rather than assumed.

**What the model never does.** It does not decide whether an entry is a finding, it does not
touch a score or a record, and it never sees the labels. Every note is advisory text attached
to an entry. A named human records every accept / dismiss / escalate in an append-only SQLite
store: changing your mind adds a decision, it never edits one. Notes are append-only too - a
rewrite adds a version - and each decision records the version that was on screen, so the
record of what a reviewer was told is not silently editable after they decided; if the note
changes while they are reading it, the dashboard refuses to record until they have seen the new
one. Both rules are enforced by the database itself (triggers refuse any update, delete or
overwriting insert), not only by the code. That is tamper-resistant, not tamper-evident: a
writer who drops the triggers and puts them back leaves no trace. The dashboard shows the note
beside the entry, takes the decision, and shows the history; the workpaper and the MCP server
show the note the reviewer actually saw, the latest decision per exception, a flag when a newer
note exists than the one they read, and whether the note shown is one the reviewer saw at all.

**Measuring the notes.** `evals/narratives/cases.json` holds sixteen entries chosen
deterministically from the default ledger - one per injected archetype, three multi-flag
patterns, and two ordinary entries that only the access-list test caught - each with
expectations written by hand from the entry's own data: facts the note must mention, wording it
must not use, and the confidence band a careful reviewer would choose. `ledgerlens
eval-narratives` runs the real narrator over them and reports pass rates per property in
[docs/narrative-eval.md](docs/narrative-eval.md). Read that file with the same scepticism as
the detection numbers: it measures whether a note is grounded, specific and non-assertive, not
whether it is insightful. A rubric was chosen over similarity to a reference narrative because
the reference would itself be model-written.

The first real run scored 88% on all checks. One of the two misses was the grader's fault - it
counted a citation of AU-C 240 as an invented number - and the stored run was re-scored offline
(`--regrade`, no API calls) to 94%. That first fix admitted every number in the system prompt,
which the repository's own `@claude` review pointed out was too broad: the prompt's style example
quotes $9,912 and account 4000, so a note copying the example would have passed. The second fix
admitted a bare 240 anywhere, which the next review caught; the grader now removes the one
citation (AU-C 240, in the forms it is written in) from a note before checking its numbers. The
first re-grade moved one row (88% to 94%); the later ones moved none, and the score is still
94%. The remaining miss is a real disagreement: the model
rated a $359 last-day cash receipt to revenue as low confidence where the case file says a High
cut-off test should keep it at medium. The expectation was written before the run and stays as
written.

Three things make that number checkable rather than something to take on trust. Every result
row and the report carry the sha256 of the case file they were graded against and of the grader
itself; `--regrade` never calls the API, refuses to re-score rows across a changed case file
unless told to (and then the report says so), and keeps the grades it replaces on the row, so a
loosened check - in the grading code, its word lists and patterns, or the schema check it calls -
would show up as a new grader hash next to a changed result. The report prints
the grader's own change history and the baseline the confidence check should be read against:
the bands accept two levels on 14 of 16 cases, so a narrator that always answered "medium" would
score 88% on that check, and the notes beat that by one case. And the run rows - every prompt,
narrative and per-check result - are committed under `evals/narratives/runs/`, so anyone can
re-grade them.

## Ask the ledger from Claude Desktop

The scored ledger is exposed as an MCP server with six read-only tools: summary, top exceptions
(filterable by fiscal year, period and tier agreement), one entry in full, search, Benford, and
review status. "What are the ten riskiest entries in Q4 2025 and why" becomes one tool call
that returns every reason, both scores, and the reviewer's note and decision where they exist.

No tool records a decision. That is deliberate: the model explains and suggests, a person
decides, and the API surface says so. The server also opens the review database on a read-only
SQLite connection, so it could not write a decision even if a tool tried.

The MCP SDK needs Python 3.10+, so use an interpreter that new enough for this part:

```bash
python3.12 -m venv ~/.venvs/ledgerlens-mcp && source ~/.venvs/ledgerlens-mcp/bin/activate
pip install -e ".[mcp]"
ledgerlens-mcp --ledger data/ledger.csv          # waits silently on stdin: that is correct
```

Then add the server to `~/Library/Application Support/Claude/claude_desktop_config.json`
(absolute paths, because Claude Desktop launches it with a bare environment) and fully quit
and reopen Claude Desktop:

```json
{
  "mcpServers": {
    "ledgerlens": {
      "command": "/Users/you/.venvs/ledgerlens-mcp/bin/ledgerlens-mcp",
      "env": {
        "LEDGERLENS_LEDGER": "/absolute/path/to/data/ledger.csv",
        "LEDGERLENS_REVIEW_DB": "/absolute/path/to/data/review.sqlite"
      }
    }
  }
}
```

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
  on purpose, and so is every narrative.
- **The LLM never decides anything.** It explains flags and suggests evidence; a named human
  records every decision. Decisions, and the notes they were made against, are append-only,
  and the database enforces it.

## Roadmap

- [x] **Week 1** — synthetic generator, ingest/validation, 12 journal-entry tests,
      Benford analysis, evaluation harness, CLI, 61 tests, CI
- [x] **Week 2** — Isolation Forest anomaly score, Streamlit review dashboard,
      Excel workpaper export, tier-comparison analysis
- [x] **Week 3** — Claude-written exception narratives (structured JSON, with a
      hand-reviewed eval set), reviewer loop with accept / dismiss / escalate, MCP server,
      `@claude` PR review
- [x] **Review follow-up** — everything the first `@claude` review found: versioned
      narratives, database-enforced append-only, a read-only MCP connection, eval
      provenance, a narrower grader, and published run rows; then a second round from
      the review of that PR: the `REPLACE` route closed, the dashboard race fixed, a
      re-grade that can never call the API, and the grader's own hash on every row
- [ ] **Week 4** — QuickBooks Online connector (sandbox), scheduled re-run via
      GitHub Actions

## Limitations

- Synthetic data cannot capture adaptive behaviour: real fraud adjusts to the controls
  looking for it.
- Two-line journal entries only; multi-line allocations are generated but not yet
  modelled with realistic complexity. This also keeps two model features constant.
- The unsupervised tier cannot see cross-entry patterns such as duplicates, because its
  features are computed per entry.
- Thresholds are tuned against this generator. Against a real ledger they are a
  starting point, not a configuration.
- The narrative eval grades properties (grounded, specific, non-assertive), not insight. A
  note can pass every check and still be unhelpful.
- Nothing here constitutes an audit procedure or professional advice. It is a
  demonstration of technique.

## Development

```bash
pip install -e ".[dev,llm,mcp,app]"
pytest -q                    # the MCP tests skip below Python 3.10
pytest --cov=ledgerlens      # coverage
ruff check src tests app.py  # lint
```

CI runs the suite on Python 3.9, 3.11 and 3.12 plus a detection-quality gate. Nothing in
`narrate.py` or the eval needs a key to be tested: the API is replaced by a fake that returns
responses shaped like the real ones.

## License

MIT — see [LICENSE](LICENSE).

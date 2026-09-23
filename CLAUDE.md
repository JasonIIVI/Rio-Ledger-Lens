# LedgerLens — working context

Audit analytics over general ledger data. Read this before changing anything.

> This file is committed to a **public** repository. Keep it technical. Personal or
> business context belongs in the private vault, never here.

## Where things are

| | |
|---|---|
| Repo | `JasonIIVI/Rio-Ledger-Lens` (public, MIT) |
| Working folder | `~/Library/Mobile Documents/com~apple~CloudDocs/LedgerGen project` (iCloud) |
| Virtualenv | `~/.venvs/ledgerlens` — **deliberately outside iCloud**, one per machine |
| Python here | system **3.9.6** only, no Homebrew. Code must stay 3.9-compatible |
| CI | GitHub Actions: pytest on 3.9 / 3.11 / 3.12 + a detection-quality gate |

```bash
source ~/.venvs/ledgerlens/bin/activate
pytest -q && ruff check src tests app.py
```

## Current state

- **v0.2.0** on `main`. Weeks 1 and 2 complete and merged.
- **Week 3 in progress** on branch `week3-narratives-and-review` (not merged).
  - done: `review.py` (append-only decision store), `narrate.py` (Claude narrative layer,
    **not yet exercised against the real API**)
  - remaining: MCP server, narrative eval set, dashboard integration, tests,
    `claude-code-action` workflow
- 91 tests, ~96% coverage, ruff clean.

**Blocked on the user, not on code:**
- `ANTHROPIC_API_KEY` is not set anywhere (repo secrets, `.env`, environment). The narrative
  layer and `@claude` PR review cannot run until it is.
- The MCP Python SDK needs **Python 3.10+**. This machine has 3.9.6. CI's 3.11/3.12 runners can
  exercise it; local runs cannot until a newer Python is installed.

## Architecture

```
                        ┌──▶ 12 journal-entry tests ──┐
ledger CSV ──▶ ingest ──┼──▶ Benford analysis ────────┼──▶ exception queue ──▶ Excel workpaper
  or QuickBooks         └──▶ Isolation Forest ────────┘      (Streamlit)
```

| Module | Role |
|---|---|
| `schema.py` | column contract, 11 anomaly archetypes, US holiday calculator |
| `coa.py` | chart of accounts for the fictional test company |
| `generate.py` | labelled synthetic GL generator |
| `ingest.py` | validation + derived columns; `entry_level()` collapses lines to entries |
| `jets.py` | 12 deterministic tests + severity-weighted risk score |
| `benford.py` | first-digit test, MAD (Nigrini bands) + chi-square |
| `features.py` | 16 engineered per-entry features for the model tier |
| `model.py` | Isolation Forest, rank-based flagging, tier comparison |
| `evaluate.py` | precision/recall, by archetype, by test, tier comparison, model lift |
| `report.py` | 5-tab Excel workpaper |
| `review.py` | append-only SQLite decision store |
| `narrate.py` | Claude narratives, strict JSON contract |
| `cli.py` | `generate` / `test` / `score` / `benford` / `report` |

## Rules that must not be broken

1. **Never commit real data.** Synthetic + QuickBooks sandbox only. Before any push touching
   data handling:
   ```bash
   git ls-files | grep -E '\.csv$|\.xlsx$|^data/'   # must be empty
   git ls-files | grep -E '^\.env$'                 # must be empty
   ```
2. **Detection code never sees the labels.** Only `evaluate` joins them back. This is the only
   reason the reported metrics mean anything.
3. **The two tiers are scored separately and never blended.** They answer different questions;
   averaging them produces a number that answers neither.
4. **No model feature may encode a rule's answer.** Day-of-week is cyclical, not a weekend flag —
   otherwise the model just relearns JET-02 and tier disagreement becomes meaningless.
5. **Every flag carries a written `reason`.** A score with no explanation moves work to the
   reviewer instead of saving it.
6. **A flag is a question, not a finding.** Word all reasons and narratives accordingly; never
   assert an error or speculate about intent.
7. **Thresholds are tuned against labelled data, not chosen for roundness.** Record the sweep in
   `docs/tuning.md`.
8. **The LLM never decides anything.** It explains flags and suggests evidence. A named human
   records every accept / dismiss / escalate, and decisions are append-only.

## The honest framing of the results

Headline: precision 0.843, recall 0.974 over 5,085 entries. **These are partly circular** — for
nine of eleven archetypes the generator injects the anomaly using the same definition the test
looks for, so recall on those is near-tautological. The non-circular numbers are `benford_drift`
(0.75) and `rare_account_pair` (0.86).

Same pattern in the model tier: it is a strong re-ranker (top-25 precision 0.64, 42x lift over
random) but a weak independent detector — the "model only" segment sits at the base rate, because
these anomalies were *defined* as rule violations.

**Breaking that circularity is the single most valuable remaining task** (planned for week 5:
inject archetypes no rule describes, then re-measure both tiers). Do not quietly report the
flattering number.

## Conventions

- Feature branch → PR → squash merge → tag. No direct commits to `main`.
- Tests live beside the module they cover; the session-scoped `ledger` fixture is in
  `tests/conftest.py`.
- Comments explain *why*, not *what*. Existing code sets the density — match it.
- Run `ruff check src tests app.py` before every commit.

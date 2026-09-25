# LedgerLens — working context

Audit analytics over general ledger data. Read this before changing anything.

> This file is committed to a **public** repository. Keep it technical. Personal or
> business context belongs in the private vault, never here.

## Where things are

| | |
|---|---|
| Repo | `JasonIIVI/Rio-Ledger-Lens` (public, MIT) |
| Working folder | `~/Library/Mobile Documents/com~apple~CloudDocs/LedgerGen project` (iCloud) |
| Virtualenvs | `~/.venvs/ledgerlens` (3.9) and `~/.venvs/ledgerlens312` (3.12) — **deliberately outside iCloud**, one set per machine |
| Python here | system 3.9.6 plus python.org **3.12.0** at `/usr/local/bin/python3.12`. Code must stay 3.9-compatible; only `mcp_server.py` needs 3.10+ |
| CI | GitHub Actions: pytest on 3.9 / 3.11 / 3.12 + a detection-quality gate; `claude.yml` answers `@claude` |

```bash
source ~/.venvs/ledgerlens/bin/activate && pytest -q && ruff check src tests app.py
source ~/.venvs/ledgerlens312/bin/activate && pytest -q      # also runs the MCP tests
```

Run both before every commit. The 3.9 venv has anthropic 0.x; the 3.12 venv has anthropic 1.x
and the MCP SDK, so the two together cover every code path CI will see.

## Current state

- **v0.3.1** on `main` (PR #5 squash-merged and tagged 2026-09-25; v0.3.0 was PR #2 on
  2026-09-23). Weeks 1–3 complete:
  narratives, review loop, dashboard integration, MCP server, narrative eval, `@claude` workflow.
- **Review follow-up on `main`** (PR #4, 2026-09-24) — the first `@claude` review's findings:
  narratives are versioned and each decision records the note it saw (`narrative_id`);
  append-only is enforced by SQLite triggers; the MCP path opens the review database read-only;
  a data-not-instructions rule in the system prompt; `cases_sha256` on every eval row, with
  `--regrade` refusing to cross a changed case file; the citation stripped before a note's
  numbers are counted; grader notes and the "always medium" baseline in the report; run rows committed under
  `evals/narratives/runs/`; `fetch-depth: 0` for reviews.
- **Second follow-up (PR #5, 2026-09-24)** — the second review's findings: `BEFORE INSERT`
  guards close the `REPLACE INTO` route around the triggers; the dashboard records the note it
  rendered and refuses if it changed before the submit; `narrative_seen_by_reviewer` in the
  workpaper, `narrative_history` / `narrative_superseded` over MCP; `--regrade` can never call
  the API and never deletes rows; `grader_sha256` and `metrics_history` on every row; the
  AU-C 240 citation is stripped before counting numbers instead of a bare "240" being allowed.
- **Next: week 4** — QuickBooks Online sandbox connector and README polish, toward v1.0.0
  (due 2026-10-18). Do first, per the second review: key the review store by ledger
  (`ledger_id`), `PRAGMA user_version` migrations, keep QuickBooks text out of committed eval
  rows (extend the rule-1 check to `evals/narratives/runs/`), an injection eval case, tokens
  outside the repo.
- 205 tests on 3.9 / 212 on 3.12, ruff clean.

**Verified on the real API (2026-09-23):** 25 narratives cached (89% of input tokens read from
cache), eval 94% pass-all. The grader has been corrected three times since the first run, each
disclosed under "Grader notes" in the report; the first correction moved one row (88% to 94%),
the later ones none. `ANTHROPIC_API_KEY` is in `.env`
(never read it, never commit it) and in the repository secrets; the Claude GitHub App is
installed; the `ledgerlens` MCP entry is in Claude Desktop's config. An organisation-level
key needs `ANTHROPIC_WORKSPACE_ID` as well; a workspace-scoped key does not.

## Architecture

```
                        ┌──▶ 12 journal-entry tests ──┐
ledger CSV ──▶ ingest ──┼──▶ Benford analysis ────────┼──▶ exception queue ──▶ Claude note ──▶ reviewer ──▶ Excel workpaper
  or QuickBooks         └──▶ Isolation Forest ────────┘     (Streamlit)      (advisory JSON)  (append-only)
                                                                  └───▶ MCP server (read-only) ───▶ Claude Desktop
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
| `review.py` | append-only SQLite store: decisions and versioned narratives, enforced by triggers; `read_only()` opener |
| `narrate.py` | Claude narratives: structured-output JSON contract, cacheable system prompt, usage accounting |
| `narrative_eval.py` | case selection (the one label reader), rubric grader (the citation stripped before numbers are counted), runner with case-file provenance, report with grader notes and baseline |
| `ledger_context.py` | read-only query layer (summary, top exceptions, explain, search, Benford, review status); opens the review DB `mode=ro`; 3.9-safe |
| `mcp_server.py` | MCP registration over `ledger_context` (v2 SDK, stdio); needs 3.10+ |
| `env.py` | dependency-free `.env` loader |
| `cli.py` | `generate` / `test` / `score` / `benford` / `report` / `narrate` / `eval-narratives` |
| `app.py` | Streamlit dashboard: queue, note, decision form, history |

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
   records every accept / dismiss / escalate. Decisions and narratives are append-only, enforced
   by SQLite triggers, and each decision records the narrative it was made against. The MCP
   server is therefore read-only: no tool records a decision, a read never creates the review
   database, and the connection it opens is `mode=ro`.
9. **Eval expectations are written from the entry's own data, before any run,** and are never
   adjusted to fit a model's output. Report whatever the numbers are. Every result row carries
   the case file's sha256 and the grader's; `--regrade` never calls the API, keeps the grades
   it replaces under `metrics_history`, refuses to cross a changed file unless
   `--allow-cases-change`, and the report then says so. Any grader change goes in
   `GRADER_NOTES` with its effect on the published score.

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

The narrative eval has the same shape of caveat: it grades properties (grounded, specific,
non-assertive, confidence in band), not insight. A note can pass every check and still be
unhelpful. Say so wherever the number is quoted.

## Conventions

- Feature branch → PR → squash merge → tag. No direct commits to `main`.
- Tests live beside the module they cover; the session-scoped `ledger` fixture is in
  `tests/conftest.py`.
- Comments explain *why*, not *what*. Existing code sets the density — match it.
- Run `ruff check src tests app.py` and both venvs' test suites before every commit.
- The Claude API is never called from a test: `tests/conftest.py` has the fake client
  (`llm` fixture) shaped like the real responses, thinking block included.
- `evals/narratives/cases.json` is tied to the generator by tests; regenerate it with
  `ledgerlens eval-narratives LEDGER --select --labels LABELS --overwrite` only if the generator
  changes, then rewrite the expectations by hand.
- Eval run rows live in `evals/narratives/runs/<utc-date>-<model>/` and are committed (synthetic
  entries only). Never edit a row by hand; re-grade through the CLI so provenance is recorded.

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
| CI | GitHub Actions: pytest on 3.9 / 3.11 / 3.12 + a detection-quality gate + a `rule-1` job (no data or secret file tracked); `claude.yml` answers `@claude` |

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
  `--regrade` refusing to cross a changed case file; an explicit citation allowlist in the
  grader (replaced by stripping the citation in the second follow-up); grader notes and the "always medium" baseline in the report; run rows committed under
  `evals/narratives/runs/`; `fetch-depth: 0` for reviews.
- **Second follow-up (PR #5, 2026-09-24)** — the second review's findings: `BEFORE INSERT`
  guards close the `REPLACE INTO` route around the triggers; the dashboard records the note it
  rendered and refuses if it changed before the submit; `narrative_seen_by_reviewer` in the
  workpaper, `narrative_history` / `narrative_superseded` over MCP; `--regrade` can never call
  the API and never deletes rows; `grader_sha256` and `metrics_history` on every row; the
  AU-C 240 citation is stripped before counting numbers instead of a bare "240" being allowed.
- **Pre-QuickBooks hardening (PR #6, 2026-09-25)** — the review database carries a schema
  version (`PRAGMA user_version`; numbered migration steps built from frozen DDL; newer,
  foreign or damaged files refused with a message; a lost append-only guard is put back by a
  read-write open and refused by a read-only one; `ledgerlens report --db` reads without
  migrating); every eval row written since records the ledger's sha256 and a re-grade records
  it on older rows once their prompt rebuilds; `run_eval`, `save_cases` and the CLI refuse any
  ledger but the generator's default for the committed eval paths (evals/ and docs/, however a
  path is spelled); a test rebuilds every stored prompt in every file under the runs root; and
  CI has a `rule-1` job that reads paths the way git prints them.
- **Week-4 hardening (`week4/hardening`, tagged v0.4.0 at its squash merge)** — the three
  items the second review asked for before QuickBooks. The review store is keyed by ledger
  (schema version 4: `ledger_id` on both tables; the identity is `csv:<ledger sha256>` or
  `qbo:<realm id>` from a `<ledger>.identity.json` sidecar; a store is bound to one identity
  and every read and write is filtered by it; rows from before the key sit under `legacy`
  until `ledgerlens adopt-legacy LEDGER --db …` copies them into a ledger with no rows yet;
  until then `narrate` refuses unless `--ignore-legacy` and the dashboard's write buttons wait
  for adoption or an explicit "start from scratch", and the dashboard, the workpaper and
  `review_status` say what other ledgers a file holds). A hand-written injection eval case:
  a line description replaced through the case's `overrides` before the prompt is built,
  expectations committed and pushed before its one narration, the crossing re-grade in the
  same commit, `CASE_SET_NOTES` in the report (17 cases, 94%). And `connectors/tokens.py`,
  which keeps QuickBooks tokens outside any checkout with mode 600.
- **Next** — the QuickBooks Online sandbox connector (`week4/qbo-connector`: OAuth2 through
  the token store, JournalEntry entity + General Ledger report → `ingest.prepare`,
  `ledgerlens qbo-auth` / `pull-qbo`, recorded-JSON fixtures, no network in tests), then the
  weekly scheduled Action, the README final pass and v1.0.0 (due 2026-10-18). Week 5 breaks
  the circularity in the detection numbers.
- 270 tests on 3.9 / 279 on 3.12, ruff clean.

**Verified on the real API (2026-09-23; the injection case on 2026-09-26):** 25 narratives
cached (89% of input tokens read from cache), eval 94% pass-all over 17 cases (the seventeenth
took one request). The grader has been corrected three times since the first run, each
disclosed under "Grader notes" in the report; the first correction moved one row (88% to 94%),
the later ones none. `ANTHROPIC_API_KEY` is in `.env`
(never read it, never commit it) and in the repository secrets; the Claude GitHub App is
installed; the `ledgerlens` MCP entry in Claude Desktop's config needs re-adding (found
missing on 2026-09-25; migrate the local review database to schema 4 and adopt its rows first,
see rule 8). An organisation-level
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
| `ingest.py` | validation + derived columns; `entry_level()` collapses lines to entries; `ledger_digest()` / `ledger_identity()`, the review-store key (`csv:<sha256>`, or `qbo:<realm>` from a `.identity.json` sidecar) |
| `jets.py` | 12 deterministic tests + severity-weighted risk score |
| `benford.py` | first-digit test, MAD (Nigrini bands) + chi-square |
| `features.py` | 16 engineered per-entry features for the model tier |
| `model.py` | Isolation Forest, rank-based flagging, tier comparison |
| `evaluate.py` | precision/recall, by archetype, by test, tier comparison, model lift |
| `report.py` | 5-tab Excel workpaper |
| `review.py` | append-only SQLite store keyed by ledger (`ledger_id`; schema version 4 via `PRAGMA user_version`, numbered migrations from frozen DDL): decisions and versioned narratives, enforced by triggers; `read_only()` opener; `ledgers()` / `adopt_legacy()` |
| `narrate.py` | Claude narratives: structured-output JSON contract, cacheable system prompt, usage accounting |
| `narrative_eval.py` | case selection (the one label reader), rubric grader (the citation stripped before numbers are counted), `case_prompt` with per-case `overrides`, runner with case-file and ledger provenance, report with grader notes, case-set notes and baseline |
| `ledger_context.py` | read-only query layer (summary, top exceptions, explain, search, Benford, review status); opens the review DB `mode=ro`, bound to the ledger's identity; 3.9-safe |
| `mcp_server.py` | MCP registration over `ledger_context` (v2 SDK, stdio); needs 3.10+ |
| `env.py` | dependency-free `.env` loader |
| `connectors/tokens.py` | QuickBooks token store: `~/.config/ledgerlens/qbo-<environment>-<realm>.json` (or `$LEDGERLENS_TOKEN_DIR`), mode 600, refuses any path inside a git checkout; stdlib |
| `cli.py` | `generate` / `test` / `score` / `benford` / `report` / `narrate` / `adopt-legacy` / `eval-narratives` |
| `app.py` | Streamlit dashboard: queue, note, decision form, history |

## Rules that must not be broken

1. **Never commit real data.** Synthetic + QuickBooks sandbox only. Before any push touching
   data handling:
   ```bash
   git ls-files -ci --exclude-standard                                    # must be empty (force-added ignored files)
   git ls-files -z | LC_ALL=C grep -zaiE '\.csv$|\.xlsx$|\.parquet$|\.sqlite$|\.sqlite3$|\.db$|^data/|(^|/)\.env(\..*)?$|(^|/)secrets/|qbo-(sandbox|production)-[^/]*\.json$' | tr '\0' '\n' | grep -vx '.env.example'   # must be empty
   ```
   (NUL-separated and any case on purpose: `git ls-files` quotes and escapes a path with a
   non-ASCII byte, which a line-based grep never matches.) CI's `rule-1` job runs the same
   check. The committed eval rows under `evals/narratives/runs/`, the case file and
   `docs/narrative-eval.md` are for the generator's default ledger only: `ledgerlens
   eval-narratives`, and `run_eval` / `save_cases` beneath it, refuse to write rows, cases or
   the report for any other ledger into evals/ or docs/, however the path is spelled; every row
   written since PR #6 records the ledger's sha256 (the 2026-09-23 rows gain it at their next
   re-grade, after each prompt has been rebuilt from the ledger); and a test rebuilds every
   stored prompt in every file under the runs root.
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
   database, and the connection it opens is `mode=ro`. Review rows are keyed by ledger
   identity: a store is bound to one `ledger_id` and never shows another ledger's rows; rows
   from before the key (`legacy`) are copied into a ledger only by `ledgerlens adopt-legacy`,
   and the originals stay.
9. **Eval expectations are written from the entry's own data, before any run,** and are never
   adjusted to fit a model's output. Report whatever the numbers are. Every result row carries
   the case file's sha256 and the grader's; `--regrade` never calls the API, keeps the grades
   it replaces under `metrics_history`, refuses to cross a changed file unless
   `--allow-cases-change`, and the report then says so. Any grader change goes in
   `GRADER_NOTES` with its effect on the published score; any case-set change goes in
   `CASE_SET_NOTES` with the commit that introduced it and its measured effect on the rows
   already graded, the crossing re-grade ships in that commit, and a case added by hand is
   narrated only after that commit is pushed. A case's `overrides` are part of the prompt its
   note answered, so a narrated case's override is frozen: new text means a fresh runs
   directory (or another entry's case), never a re-grade, and the eval refuses to cross it.

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
- `evals/narratives/cases.json` is tied to the generator by tests; regenerate it only if the
  generator changes, in this order: take the new `DEFAULT_LEDGER_SHA256` from the failing pin
  test (`test_ledger_digest_is_canonical_and_pins_the_default_ledger`) and set it in
  `narrative_eval.py` (until then the CLI refuses the committed case file for the changed
  ledger), then `ledgerlens eval-narratives LEDGER --select --labels LABELS --overwrite`, then
  rewrite the expectations by hand. Runs committed under the old constant can no longer be
  rebuilt from the new ledger, and nothing deletes rows, so deciding what happens to them (a
  recorded history of default digests, or retiring the directory) is part of that change.
- Eval run rows live in `evals/narratives/runs/<utc-date>-<model>/` and are committed (synthetic
  entries only). Never edit a row by hand; re-grade through the CLI so provenance is recorded.
- Prompts for eval cases are built only through `case_prompt`. A case may override a line's
  `description` (its `overrides`) and nothing else, so the ledger file stays untouched.
  `--select --overwrite` rewrites the selected cases and drops the hand-written injection
  case: re-add it by hand.
- A review-schema change is a new numbered migration step in `review.py` built from frozen
  DDL literals, never from the live constants, so the step keeps doing what it did when it
  shipped; old-schema fixtures in the tests are DDL in code, never binary files.
- `.env` holds configuration only (the API key, `QBO_*` client settings). QuickBooks tokens go
  through `connectors/tokens.py` to `~/.config/ledgerlens/`, which refuses any path inside a
  git checkout.

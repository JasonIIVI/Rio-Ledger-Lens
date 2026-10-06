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
| CI | GitHub Actions: pytest on 3.9 / 3.11 / 3.12 + a detection-quality gate + a `rule-1` job (no data or secret file tracked); `claude.yml` answers `@claude`; `weekly.yml` regenerates the default ledger on Mondays and opens an Issue with `ledgerlens summary` |

```bash
source ~/.venvs/ledgerlens/bin/activate && pytest -q && ruff check src tests app.py
source ~/.venvs/ledgerlens312/bin/activate && pytest -q      # also runs the MCP tests
```

Run both before every commit. The 3.9 venv has anthropic 0.x; the 3.12 venv has anthropic 1.x
and the MCP SDK, so the two together cover every code path CI will see.

## Current state

- **v1.0.0** is PR #9 (`week4/automation-and-readme`), tagged after its merge once the weekly
  run has opened its first Issue. **v0.4.0** was PR #7 (2026-09-29; v0.3.1 was PR #5 on
  2026-09-25; v0.3.0 was PR #2 on 2026-09-23). Weeks 1–4 complete; weeks 1–3 were:
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
- **Week-4 hardening (PR #7, tagged v0.4.0)** — the three
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
  which keeps QuickBooks tokens outside any checkout with mode 600. A multi-agent review before
  the merge confirmed 29 findings (one medium: a dashboard write before `adopt-legacy` shut the
  legacy rows out for good), all fixed on the branch: the dashboard's writes wait for adoption
  or "start from scratch", a malformed sidecar is a message on every surface, the eval checks
  override lines before narrating, the token store writes only records `load()` accepts for its
  file's realm, and tests that could not fail were tightened (checked with mutants). A
  verification pass over those fixes found four more, also fixed: the start-from-scratch
  choice was keyed by ledger but not by file, a `null` override and a Unicode-digit line key
  got past the case loader, and the draft half of the per-entry keying was untested.
- **QuickBooks Online connector (`week4/qbo-connector`, no tag)** — `connectors/qbo.py`,
  standard library only: OAuth 2.0 through the token store (`ledgerlens qbo-auth`, a
  localhost callback that accepts only the expected state), a client that refreshes once on
  a 401, waits out a 429 (60 s unless `Retry-After` says otherwise) and pages queries, and a
  pure mapping (`ledgerlens pull-qbo`): journal entries from the entity with the user from
  the GL report, every other transaction rebuilt whole from the GL report, the type as
  `source` and in the entry id (a type it does not map gets `System` and is named in a
  warning), the entry time from the earliest date any of a transaction's rows gives (a time
  of day preferred within that day; the posting date only when no row gives a date),
  timestamps read by explicit formats so Python 3.9 and 3.12 agree, a date alone estimated at
  noon and flagged, an unreadable one (report or `CreateTime`) estimated, counted and warned
  about, a UTC time without `QBO_TIMEZONE` warned about on either clock, every skipped line
  and every transaction with no posting line counted in `PullStats`. It writes `data/qbo-ledger.csv` and the `qbo:<realm>` sidecar. Tests replay
  `tests/fixtures/qbo/`: `pull/` is a sanitized `pull-qbo --record` of a real sandbox
  company (Intuit's sample company, 2026-07-01..2026-09-30, recorded 2026-09-30; 116
  entries / 297 lines), and its README answers the ten questions the recording had to
  settle. Shapes the quarter lacks are reached by editing a copy of the recording in the
  test, or by a minimal report built in the test for row-level cases; no hand-shaped file
  remains in `pull/` (`auth/` stays hand-shaped: a code exchange cannot be replayed). The
  recording found three defects: the report writes `create_date` offsets as `-0700`,
  which Python 3.9 could not read (on 3.9 only, every report-built entry lost its time of
  day and 65 of 113 their keying date); Cash Expense and Sales Tax Payment fell back to
  `System` (now AP); and one transaction's rows can carry different create dates. A review
  of those commits (security, correctness and claims lenses; eleven findings confirmed by a
  skeptic, nine lower-ranked ones not verified, the real ones among them fixed too) found no
  security problem, and smaller ones all fixed: a date plus an offset read as a time of day
  (the report's colon-less form on 3.12 only; now explicit formats), a ranking that could
  prefer the posting date over a real date, warnings that miscounted, report times in UTC
  not warned about, description and user rules the one-user sandbox could not test, and
  overstated wording; a strict parser would have made an odd `CreateTime` a traceback, so
  it is now estimated and counted. A verification of those fixes found a further round,
  also fixed: a later time of day beat an earlier date (so the keying date was lost again),
  two tie-breaks, a gate stricter than its parser (now gone: the parser decides), unpadded
  leading dates, set-aside counts, and types outside the map filed as `System` in silence
  (Credit Card Payment now Bank). A last check of that round found an id that only starts
  like a date (`2026-1-12345`) winning as the keying date, a row with no type filed as
  `QBO--57` (now `UnknownType`), and test gaps; fixed, with every timestamp format pinned. Facts about Intuit's API were checked against its
  current documentation on 2026-09-30 (minor version 75; no documented `Retry-After`; the
  transaction id on the `txn_type` cell; reads are metered). A security review and a
  correctness review before the push found 16 problems (two medium: a rotated refresh token
  could be lost before it was saved, and `--out` could take over the synthetic ledger or its
  identity file however spelled); all fixed, and a check of those fixes found six more, also
  fixed: the token directory is checked before any code or refresh is spent, redirects are
  never followed, the callback server is threaded and holds both loopback addresses, a
  recording is re-scrubbed and checked on every exit, and pulled books stay in `data/`.
- **Automation and the README (PR #9 `week4/automation-and-readme`, version 1.0.0)** —
  `ledgerlens summary` (`summary.py`): one page on a ledger as text, markdown or JSON. It
  scores nothing itself (the counts are `LedgerContext`'s, the measured quality `evaluate`'s),
  keeps the tiers in separate sections, and carries each caveat as a field of the payload:
  the circularity caveat precedes any precision or recall, which appear only with labels,
  headed "rule tier only", each archetype as caught-of-n with the two measured ones first, and
  a ratio over nothing printed as "undefined". The markdown is posted in public by the weekly
  workflow, on a repository whose `claude.yml` answers a mention in an Issue, so every string
  that came from the ledger goes through `md_code` (one line, the characters a reader cannot
  see removed by the ranges in `summary._UNSEEN_RANGES`, a code span whose fence outruns any
  backticks, a pipe written as an entity, a zero-width space after every `@`); no format
  carries note text, a reviewer's name or another ledger's identity, and a decision is named
  only if it is one of the kinds this version records; `--out` inside a checkout lands only
  under `out/`, the path expanded once so the one judged is the one written; `--format json`
  prints one document and nothing else, with the caveats as fields and a `defined` flag per
  ratio. `evaluate.check_labels` refuses labels that do not cover exactly the ledger's entry
  ids, compared as the values `evaluate` joins on (`test`, `score`, `report` and `summary`
  print `refused:`; the dashboard, whose sidebar offers `data/labels.csv` for every ledger,
  sets them aside and shows the reason as text) and can show a mismatch, never that a file
  belongs; `load_labels` reads `is_anomaly` strictly. `evaluate.DETECTION_CAVEAT` and
  `MODEL_TIER_CAVEAT` are the one wording of the two caveats, printed by everything that
  prints the numbers they qualify. `LedgerContext.summary()` lists every test and every tier
  relation, zeros included, and keeps any key outside the two lists. `.github/workflows/weekly.yml` (Mondays 13:23 UTC, or
  by hand; `contents: read` and `issues: write`; no repository secret; `github.token` the only
  expression; credentials not persisted) opens an Issue labelled `weekly-run` from the
  markdown and then closes the previous one. The generator is seeded, so the Issue is a
  regression watch for the code and its dependencies, not new data.
  `tests/test_workflows.py` reads the workflow files without a YAML parser and parses every
  `ledgerlens` line in them with `build_parser()`. README final pass: a Mermaid diagram,
  `docs/demo.gif` (recorded against a copy of the review database, no API call), a "how to
  read it" column on the results and tier tables, and what the tests make of a QuickBooks
  pull; `tests/test_readme.py` and a test on the recorded fixtures compute the README's
  figures and look for them in its text. A review of the five feature commits before the
  push (security, correctness and claims lenses, every finding judged by a skeptic) kept 33
  findings, none high once judged, fixed in four commits: `--out=~/x` passed the guard and
  was written to a directory named `~` inside the checkout; the review line counted every
  decision as a part of the flagged entries, and named decision kinds straight from the
  database file; the invisible-character list missed a bidi control and the whole tag block
  (ASCII no reader sees); a refusal quoted ids raw into the dashboard's markdown and the
  terminal; ids were compared as text but joined as values; `is_anomaly` was read with
  `astype(bool)`, so a blank cell was an anomaly; the caveat was claimed to be shown wherever
  a number is while `test`, `score`, the workpaper and CI's gate printed bare figures; three
  statements about "every format" were false for JSON; the Issue step's shell had no test
  that ran it (it now runs against a stand-in `gh`); the Mermaid diagram drew Benford into
  the queue; the README said running the weekly workflow by hand re-enables it (it is
  enabled from the Actions tab); and the first GIF carried the recorder's own labels.
- **Next** — week 5 breaks the circularity in the detection numbers (archetypes no rule
  describes, both tiers re-measured), and an approver list and approval limit that can be set
  for JET-12 and JET-06.
- 596 tests on 3.9 / 606 on 3.12 (the ten MCP tests need 3.10+), ruff clean.

**Verified on the real API (2026-09-23; the injection case on 2026-09-26):** 25 narratives
cached (89% of input tokens read from cache), eval 94% pass-all over 17 cases (the seventeenth
took one request). The grader has been corrected three times since the first run, each
disclosed under "Grader notes" in the report; the first correction moved one row (88% to 94%),
the later ones none. `ANTHROPIC_API_KEY` is in `.env`
(never read it, never commit it) and in the repository secrets; the Claude GitHub App is
installed; the `ledgerlens` MCP entry in Claude Desktop's config needs re-adding (found
missing on 2026-09-25; the local review database was migrated to schema 4 and its rows
adopted on 2026-09-29, so it is ready, see rule 8). An organisation-level
key needs `ANTHROPIC_WORKSPACE_ID` as well; a workspace-scoped key does not.

## Architecture

```
                        ┌──▶ 12 journal-entry tests ──┐
ledger CSV ──▶ ingest ──┼──▶ Isolation Forest ────────┴──▶ exception queue ──▶ Claude note ──▶ reviewer ──▶ Excel workpaper
  or QuickBooks         │                                   (Streamlit)      (advisory JSON)  (append-only)
                        │                                         └───▶ MCP server (read-only) ───▶ Claude Desktop
                        └──▶ Benford analysis ──▶ reported beside the queue (its own dashboard tab, workpaper sheet, MCP tool)
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
| `evaluate.py` | precision/recall, by archetype, by test, tier comparison, model lift; `check_labels` (labels must cover exactly the ledger's entries) and the two caveat constants |
| `report.py` | 5-tab Excel workpaper |
| `review.py` | append-only SQLite store keyed by ledger (`ledger_id`; schema version 4 via `PRAGMA user_version`, numbered migrations from frozen DDL): decisions and versioned narratives, enforced by triggers; `read_only()` opener; `ledgers()` / `adopt_legacy()` |
| `narrate.py` | Claude narratives: structured-output JSON contract, cacheable system prompt, usage accounting |
| `narrative_eval.py` | case selection (the one label reader), rubric grader (the citation stripped before numbers are counted), `case_prompt` with per-case `overrides`, runner with case-file and ledger provenance, report with grader notes, case-set notes and baseline |
| `ledger_context.py` | read-only query layer (summary, top exceptions, explain, search, Benford, review status); opens the review DB `mode=ro`, bound to the ledger's identity; 3.9-safe |
| `mcp_server.py` | MCP registration over `ledger_context` (v2 SDK, stdio); needs 3.10+ |
| `summary.py` | one page on a scored ledger (`collect`, `render` as text / markdown / JSON); scores nothing itself; `md_code` renders ledger text as inert markdown |
| `env.py` | dependency-free `.env` loader |
| `connectors/qbo.py` | QuickBooks Online: `QboConfig` (`QBO_*`), transports (urllib, `RecordedTransport` replay, sanitizing `Recorder`), OAuth (`QboAuth`, `CallbackServer`), `QboClient` (refresh, throttling, paging), and the mapping into the ledger contract (`pull`, `PullStats`, `write_identity`); stdlib |
| `connectors/tokens.py` | QuickBooks token store: `~/.config/ledgerlens/qbo-<environment>-<realm>.json` (`$XDG_CONFIG_HOME/ledgerlens/` when set; `$LEDGERLENS_TOKEN_DIR` overrides both), mode 600, a record only for the realm its file names, refuses any path inside a git checkout; stdlib |
| `cli.py` | `generate` / `test` / `score` / `benford` / `report` / `summary` / `narrate` / `adopt-legacy` / `eval-narratives` / `qbo-auth` / `pull-qbo` |
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
   written since PR #6 records the ledger's sha256 (the 2026-09-23 rows gained it in the
   offline re-grade across the injection case's addition, once each prompt had been rebuilt
   from the ledger); and a test rebuilds every
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
- No test touches the network: QuickBooks responses are replayed from `tests/fixtures/qbo/`
  (`RecordedTransport`), and a recording is made only with `pull-qbo --record` against a
  sandbox company, sanitized on the way to disk.
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
- `weekly.yml` and `claude.yml` run only from `main` (the weekly one on its schedule or by
  hand; `ci.yml` also runs on a pull request), so a change to them is checked by
  `tests/test_workflows.py` before it is pushed, and its commands are run by hand first. A
  `ledgerlens` command added to a workflow is added to that file's `COMMANDS` table.
- Text that came from a ledger is data. Where it leaves the machine it is made inert: the
  summary's markdown through `summary.md_code`, a prompt through the data-not-instructions
  rule, a refusal through `ascii` of the id it quotes, the workpaper's string cells written
  as text (openpyxl would store one beginning with `=` as a formula), the CLI's own lines
  through `summary.one_line`, and the dashboard's flag reasons and notes with their markdown
  escaped (`app.md`); its tables are drawn as text. `summary.py` and its tests stay ASCII
  (pinned by a test), because their patterns name characters that could not be reviewed if
  written raw.
- A caveat has one wording (`evaluate.DETECTION_CAVEAT`, `evaluate.MODEL_TIER_CAVEAT`) and is
  printed by everything that prints the number it qualifies: `test`, `score`, `summary`,
  the dashboard, the workpaper and CI's detection gate.
- `.env` holds configuration only (the API key, `QBO_*` client settings). QuickBooks tokens go
  through `connectors/tokens.py` to `~/.config/ledgerlens/` (`$XDG_CONFIG_HOME/ledgerlens/` when that is set; `$LEDGERLENS_TOKEN_DIR` overrides both), which refuses any path inside a
  git checkout.

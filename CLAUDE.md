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
| Python here | system 3.9.6 plus python.org **3.12.0** at `/usr/local/bin/python3.12`. Code must stay 3.9-compatible; only `mcp_server.py` needs 3.10+. pandas 2.2 or later (`ingest` imports `pandas.tseries.api.guess_datetime_format`), Streamlit 1.49 or later (`st.dataframe(width="stretch")`); CI installs the latest (pandas 2.3 on 3.9, 3.1 on 3.11 and 3.12; the venvs have 2.3 and 3.0.6), so the floors themselves are not run, and a new pandas reaches CI before the venvs |
| CI | GitHub Actions: pytest on 3.9 / 3.11 / 3.12 + a detection-quality gate + a `rule-1` job (no data or secret file tracked) + a `dashboard` job (the dashboard's tests and the workpaper's with the `app` extra installed: on 3.9 under Streamlit 1.50, the last one 3.9 can install, and on 3.12 under the newest; the test jobs install no Streamlit, which requires pyarrow, so their 3.11 and 3.12 legs stay the suite's runs on pandas 3 without pyarrow, and a step in them fails if pyarrow is there); `claude.yml` answers `@claude`; `weekly.yml` regenerates the default ledger on Mondays and opens an Issue with `ledgerlens summary`. The 3.9 jobs run on `ubuntu-24.04` (setup-python has no 3.9 for Ubuntu 26.04, `ubuntu-latest` since November 2026), the rest on `ubuntu-latest`; that holds while GitHub keeps 24.04 runners, and dropping 3.9 is the maintainer's call |

```bash
source ~/.venvs/ledgerlens/bin/activate && pytest -q && ruff check src tests app.py
source ~/.venvs/ledgerlens312/bin/activate && pytest -q      # also runs the MCP tests
```

Run both before every commit. The 3.9 venv has anthropic 0.x; the 3.12 venv has anthropic 1.x
and the MCP SDK, so between them the suite runs under both SDKs and the MCP tests run. They
do not run every version CI does: each CI job installs the newest release its Python allows,
so every job differs from the venvs (the three test jobs on 2026-11-15, among others: ruff
and python-dateutil newer on each; pandas, anthropic, pytest and mcp newer on 3.11 and 3.12;
numpy newer on 3.12; numpy and scipy on 3.11 older than the 3.12 venv's). The dashboard
job's 3.12 leg resolves to a newer Streamlit and pyarrow too. Green here is not yet green
there. For the dashboard that has a second sense: its tests read what a session holds after
a stopped run only from Streamlit 1.66 (the dashboard-reads bullet below), and the 3.12
venv has 1.64, so CI's 3.12 dashboard leg can fail where both venvs pass.

## Current state

- **v1.2.1** is #21 to #30 (#21 `fix/pandas-4-warnings`, squash-merged 2026-10-25 as
  `b0434a2`; #22 `fix/display-sort-ties`, 2026-10-26, `cd00a95`; #24
  `fix/benford-small-sample`, 2026-10-30, `2b113b7`; #25 `fix/reported-model`, 2026-10-31,
  `246fbe9`; #27 `fix/dashboard-request-errors`, 2026-11-08, `dbfe89c`; #29
  `fix/ci-python-3.9-runner`, 2026-11-10, `dfda99d`; #30 `fix/dashboard-store-errors`,
  2026-11-10, `c6ce5fd`) and the version bump (#31 `release/v1.2.1`, 2026-11-11, `589c06a`),
  tagged on `589c06a` (annotated; the release published 2026-11-11 20:08 UTC) with release
  notes reviewed against the code and covering all seven.
- **v1.2.0** is #18 to #20 (#18 `fix/pandas-3.1-review-state`, squash-merged 2026-10-23 as
  `8fcee26`; #19 `week6/sampling`, 2026-10-23, `540d078`; #20 `week6/materiality-band`,
  2026-10-23, `1f11203`), tagged on `1f11203` (annotated; the release published 2026-10-24
  00:31 UTC) with release notes reviewed against the code and covering all three.
  **v1.1.0** is #12 to #15 (#15, `week5/held-out`, squash-merged 2026-10-21 as `e0a2b02`) and
  #17 (`fix/pandas-floor-and-tie-order`, squash-merged 2026-10-22 as `5c99403`), tagged on
  `5c99403` with release notes reviewed against the code.
  **v1.0.0** was PR #9 (`week4/automation-and-readme`, squash-merged 2026-10-07 as `039a3ad`,
  tagged once the weekly run had opened its first Issue, #10; release notes reviewed against
  the code). **v0.4.0** was PR #7 (2026-09-29; v0.3.1 was PR #5 on 2026-09-25; v0.3.0 was PR #2
  on 2026-09-23). Weeks 1–4 complete; weeks 1–3 were: narratives, review loop, dashboard
  integration, MCP server, narrative eval, `@claude` workflow. Week 5 is three PRs: ledger
  validation (PR #12, squash-merged 2026-10-13 as `8bd6c0f`), settable controls for JET-12
  and JET-06 (PR #14, 2026-10-18, `ec2d73f`), and a held-out ledger of archetypes no rule
  describes (PR #15, 2026-10-21, `e0a2b02`).
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
- **QuickBooks Online connector (PR #8, `week4/qbo-connector`, released in v1.0.0)** — `connectors/qbo.py`,
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
  enabled from the Actions tab); and the first GIF carried the recorder's own labels. A
  verification pass over those four commits (four verifiers, one per commit, and a critic
  over the 33 findings) found 44 more, four of them medium: the Issue-step tests' stand-in
  `gh` logged `"$*"`, so an unquoted `--title $title` passed; the workpaper stored a cell
  beginning with `=` as a live formula; the dashboard still drew a flag's reason as markdown
  (an image fetched on load from a `created_by`); and `detection.defined` was pinned by no
  test with nothing flagged. All 44 fixed in eight commits (`--out ./~/x` kept literal; the
  eval guard judging the path it writes, from any directory; every ledger load one `error:`
  line; `ascii` for quoted ids; data rows not file lines; a clear message when the model
  cannot be fitted; the dashboard's reasons and notes escaped and its cache keyed on what
  the files are; the unseen list completed and the whitespace a removal leaves collapsed;
  the workpaper's cells as text; a shell-faithful extractor and a stand-in that records
  argv; the CI gate run rather than grepped; both ASCII diagrams redrawn; the README's
  prose figures pinned), each with a test that failed first or a mutant it kills. A
  verification of those eight (four verifiers, a critic) found 29 more, none medium, also
  fixed: the ascii cut by escaped length, the model-fit message on every surface, the
  sidecar in the dashboard's cache key, reasons drawn as code spans (escaped prose still
  gets a bare URL linked and `->` redrawn: checked in a browser), table cells through
  one_line, identifiers unwrapped in the workpaper, the extractor's target rule, the
  stand-in's NUL-separated record, and tests that could not fail. The PR's first CI run
  then failed on Linux, on the README's model-tier figures: the entry's account (what
  `amount_z_in_account` is measured against) came from a one-key sort of its lines by
  amount, nearly every entry the generator makes is two lines of equal amount, and a
  one-key pandas sort is not stable, so which line came first differed between a Mac and
  the x86-64 runners (most likely numpy's vectorised sort there: the runners' own 3.9 and
  3.12 jobs disagreed as well). The account is now the first of the largest lines
  (`line_no` breaks the tie), `model_lift` gives a tie at its cut to the lower entry id,
  and the figures moved once (top-25 precision 0.60, 40x lift, read as re-ranking against
  the same labels). A review of that fix found JET-07 and JET-09 leaving a same-day tie to
  the sort (JET-07 could flag the original of a same-day re-post instead of the re-post);
  on one day the entry keyed first now stands, then the lower id. `docs/demo.gif` predates
  the fix: the model scores in its queue are the old ones.
- **Ledger validation (week 5, PR #12, `8bd6c0f`)** — `ingest.prepare` refuses a
  cell no test can read before converting anything: blank or whitespace-only in a column a
  test keys on (`entry_id`, `line_no`, both dates, `fiscal_year`, `period`, `account_code`,
  `account_type`, `source`, `created_by`), a date that does not parse or carries a time
  zone offset, a whole-number column that is not whole, an amount that is not a finite
  number. One `SchemaError` names every such column with its count and first data row, the
  cell quoted through `ingest.quote` (the one quoting rule, re-exported as
  `evaluate._example`). Before, a blank account code was a TypeError in
  `jets._entry_pairs` on six commands, the dashboard and the MCP server; a blank entered
  time a TypeError inside the load; a blank entry id dropped its line, so the entry looked
  unbalanced; `abc` or `1,234.00` in an amount became 0.00 and `inf` was taken as one; a
  line number of 1.5 became 1. A blank account name, description or amount (0.00) is still
  allowed. Only an empty field is missing (`load_csv` and `load_labels` read with
  `keep_default_na=False`): pandas' default list had made a user called `NA` blank and
  Excel's `#N/A` in an amount 0.00. So a ledger whose text cells hold such a word (a
  description "N/A") now has another `csv:` identity than under v1.0.0, and its earlier
  review rows show as another ledger's; "NA" in an amount must be written blank or 0. A
  date written as a number, or as digits in a text column (one cell of another kind makes a
  CSV column text; text is counted as written, so a leading zero is a digit), is read by its
  digits (8, 12 or 14: `%Y%m%d`, plus `%H%M`, plus `%S`) and any other count refused, never
  guessed (read as a number it was nanoseconds since 1970; left to pandas as text, six-digit
  YYMMDD was read as DDMMYY); `today`
  and `now` are refused, not read as the clock. Every date column is read in one format, its
  first text cell's: pandas' own guess where it makes one (so a ledger pandas reads reads as
  before), and where it makes none (a two-digit year such as 02/01/24 or 02-Jan-24, an
  afternoon time or 12 AM, which pandas tries on a 24-hour clock), the guess with the AM or PM
  set aside and the year widened, named back as written (`_format_of`). A month or weekday in
  capitals (Oracle's 02-JAN-24, SAS's 02JAN2024) is guessed as Jan or Mon would be; May is
  `%b` and `%B` alike, so a month-named column with May first is read with `%b` where that
  reads more of it; " at " in lower case and the h of a 24-hour 09h15 between date and time
  are allowed (as written: `AT`, `9h15 AM`, or `09h15` after a two-digit year are not). A column
  padded to a fixed width whose cells differ in width, and a two-digit-year column that
  mixes spellings, are refused as in more than one format. am, pm, a.m., p.m.,
  "a. m." and a or p alone are first written AM or PM: pandas wrote a lower-case or dotted one
  into its format as text beside a 24-hour clock ("2:05 pm" read as 02:05 after "12:30 pm" in
  row 1), and pandas 2 took the M of "P.M." for a zone. A column no format can be taken from,
  or whose format would keep a word of the cell as text (pandas 2's `%H:%M:%S ET`, a weekday
  row 1's date contradicts, `2nd`), is refused rather than read cell by cell: pandas read such a
  column with dateutil, a day of 12 or under as the month wherever it could, a bare time as
  the day the ledger was read and "Jan 2024" as the first. Below row 1 pandas reads a weekday
  without checking it against the date, which is read as written. A cell in another format than the
  column's is named against the row the format came from, with the format, and a column that
  reads day first throughout is told so. A day-first ledger whose days are all 12 or under
  cannot be told from a month-first one, nor a two-digit year written first (24-01-02,
  24-JAN-02) from a day: write YYYY-MM-DD. A zone is found before pandas sees it, on text (`Z`, a name such as
  `UTC` or `EST`, `+05:30`, `-0700`, an hour alone, `UTC+1`; after an AM or PM too, and before
  a year as Java writes one) and on datetime objects; a weekday or `HRS` after the time is not
  a zone, and any other word of three to five letters there is refused as one rather than
  dropped. A name written another way (`ET`, `EST.`, `Mon EST`, `, EST`) pandas keeps as text
  in its format, so the column is refused as having no format, alike on both pandas versions
  (pandas 2 had dropped the name and loaded the time, pandas 3 refused it; `HRS` likewise),
  and such a cell below one read in the column's format is in another format (pandas 2) or
  not a readable date (pandas 3). pandas 2
  read the machine's own zone name, and once raised an AttributeError on one (now caught too);
  any zone pandas reads that none of these sees is one "mixes times" problem. pandas' warnings
  from the parse are not passed on. A review of this work (two reviewers) found eleven
  problems, a verification of those fixes ten more (one a regression CI in UTC could not see:
  an AttributeError on the machine's own zone name), a re-check of that under five machine
  zones five more, a verification of that fix seven more (four caused by it), and a
  verification of that one, which re-ran a hundred date spellings, ten more: the per-cell
  reading those fixes had kept was itself the fault, so it is gone. A verification of the
  one-format reading found no value read wrong in 100,960 loads and nine more: ledgers it
  refused that earlier commits read (months in capitals, May first, a space after PM), and
  its speed. Read from a CSV of 500,000 rows it is as fast as `e19d210` or faster on 3.12;
  on 3.9 ISO and m/d/yy dates read as fast, and digits held as text and AM/PM times about a
  tenth slower (2.5 to 2.6 s against 2.3 to 2.4 s; digit text in memory on 3.12 too), the
  price of reading them. A last check of that
  round found one regression of it, fixed: a column whose first time read " AM" refused rows
  below written `2:05PM` or `2:05 p.m.`. A QuickBooks pull goes
  through the same check (a user or an account number of spaces is counted as unknown,
  not a refused pull). The dashboard shows
  the reason as text (it quotes a cell) and catches an unreadable path too; the MCP server
  turns any `OSError` or `ValueError` into a tool error and logs its traceback first;
  `test --only` refuses an id it does not know in one line and runs a repeated one once.
  `evaluate.precision_by_test` heads its count `entries_flagged` (it counts entries, beside
  `test`'s count of flags). The workpaper, the methodology and the README now say the rule score sums the
  flags (JET-09 and JET-11 judge lines, so one entry of the default ledger scores 4.0 for a
  single JET-11), and that `--contamination` does not move the 0-1 score (the min-max
  rescale cancels sklearn's offset); the JET-11 tuning table is per entry. The narration
  system prompt still says "distinct tests": the grader's hash does not cover it, so a
  change would date the published eval silently (recorded for `docs/limitations.md`).
- **Short generator ranges (`fix/generate-short-range`)** — `generate --start 2024-01-01
  --end 2024-01-10` was an `IndexError` from `random.choice`: every one of the eleven
  injectors drew from a day list a short range can leave empty. Each injector is now handed
  its days. A range with no Saturday or Sunday, no federal holiday on a weekday, or none of
  a month's last three days leaves that archetype out with an `ArchetypeSkipped` warning
  (a `Warning:` line from the CLI), and the other archetypes keep their counts; a range
  with no business day is refused (`ValueError`; one `error:` line, exit 2, nothing
  written), as is `--end` before `--start`. The draws for a range that holds every
  archetype are unchanged: the default ledger's two CSVs are byte-identical on 3.9 and
  3.12. Not changed: a duplicate's re-post lands one to three days after its original and
  so can fall after `end` (the default ledger's does not).
- **Settable controls (week 5, `week5/controls`)** — `controls.py`: a company's approver
  list (JET-12) and approval limit (JET-06) in `<stem>.controls.json` beside the ledger
  (`{"version": 1, "manual_entry_approvers": [...], "approval_limit": 10000}`); no file is
  `DEFAULT_CONTROLS`, the synthetic company's (`controller`, `mchen`; $10,000), tied by a
  test to the generator's constants, which `jets.py` no longer imports. A file that cannot be
  used is refused, never replaced by the defaults, every problem named in one
  `ControlsError` (a `ValueError`): not JSON, a duplicate key, NaN or Infinity, an unknown or
  missing key, a version other than the integer 1, approvers that are not a non-empty list
  of distinct strings, or one that is empty, holds a comma, is over 64 characters or is not
  one line of visible text by `summary.one_line`'s test; a limit that is not a number, not
  above 0, not whole cents, or has more digits than a float keeps. Approvers are kept
  sorted, so the defaults restated render "(controller, mchen)" and "$10,000" byte for byte
  (the committed prompts still rebuild). `jets.run_all(..., controls=)`; read by test, score,
  report, summary, narrate and eval-narratives (one `error: cannot use the controls file`
  line), by `LedgerContext` for a ledger given by path (so the MCP server), and by the
  dashboard (cache key covers the file; the MCP server reads it at its first load). test,
  score, report, narrate and eval-narratives print "Controls from ..." first when a file was
  used (summary names it on the page); the summary (a `controls` block; `CONTROLS_NOTE`, `DEFAULT_CONTROLS_NOTE`, and
  `SEEDED_NOTE` only for the default ledger under the defaults), the workpaper (Summary rows,
  a Methodology block), the dashboard's sidebar and the MCP summary name them.
  `eval-narratives` refuses other controls on the default ledger in every mode. `pull-qbo`
  says when no controls file sits beside the pull; `generate` says when one already sits
  beside the ledger it writes. `*.controls.json` is gitignored and in rule 1's pattern
  (real approvers are real people). A review (correctness and security, claims, test
  strength) found no high: fixed were inputs that escaped as a traceback or hung (deep
  nesting, huge exponents and digit strings, a FIFO), so a file is now read only if it is a
  regular file of at most 64 KiB, numbers of over 40 characters and NaN or Infinity are named
  against the key holding them, every problem (duplicate keys included) is named in one
  message, at most 50 approvers, a limit below $1 trillion; a leading `~` in a ledger path
  (pandas expands it) is expanded for the controls and identity files too, and by `pull-qbo
  --out`, so the guard judges the path written; a failed `LedgerContext.load` keeps no identity
  or controls; a link to nothing keys the dashboard's cache apart from no file; and claims
  (only JET-11's threshold was swept; 12 of the 17 committed prompts quote the defaults).
  A verification of those fixes found a medium (`pull-qbo --out ./~/x.csv`: a Path drops the
  `./`, and pandas then expanded the `~`; the path is now made absolute too) and two
  regressions of them (Decimal arithmetic rounds to 28 digits, so 10000.000…001 passed as
  whole cents and 1e-1000030 as a $0 limit: now compared with `quantize`; a NaN inside a
  list printed an object's address), plus a value nested near the decoder's limit that
  crashed the message (containers now shown through `reprlib`, two levels), a link loop keyed
  like no file in the dashboard's cache, the Controls line split at a space (now one line,
  as the Ledger line is), and the identity sidecar's own FIFO, depth and dangling-link
  defects (on `main` before this branch; it is now read as the controls file is).
  Names are compared as written (no Unicode normalisation; a ledger name with stray spaces
  cannot be listed): documented. Review rows are keyed by ledger, not controls, so after a
  controls change a decided entry stays out of the queue whatever it is now flagged for; the
  maintainer decided on 2026-10-18 to keep that documented rather than record the controls on
  each row (a schema change).
- **Breaking the circularity (week 5, `week5/held-out`, before the freeze)** — four steps.
  - **Attribution.** `evaluate.attribution` counts which test caught each archetype: eight
    are caught wholly by the test written for them, and `benford_drift`, `dormant_account` and
    `rare_account_pair` mostly by others (`NOT_DEFINITIONAL`, replacing
    `NON_CIRCULAR_ARCHETYPES`). The caveat says "eight of eleven"; the README carries the
    table and three corrected bullets; `test --labels` prints it; the summary reads those
    three as "not definitional" (was "measured").
  - **The held-out ledger.** `schema.HeldOutType` (five archetypes, 43 entries,
    `generate.HELD_OUT_MIX`), injected only by `generate_ledger(held_out=True)` /
    `generate --held-out` (`heldout-ledger.csv`, never `ledger.csv`), from their own streams and
    numbered last, so the rest of that ledger is the one the seed gives alone. Each is built
    to be rule-silent (JET-01 to 06, 10 and 12 by construction; JET-07, 08, 09 and 11 read the
    population), and the tests check it on four development seeds (no flag on a held-out
    entry, no other entry's (entry, test) flag counts moved); on other seeds the held-out lines
    can move another entry's JET-11 flag (`docs/limitations.md`). `--held-out` without `--seed` uses
    `HELD_OUT_SEED`; until the measurement `tests/conftest.py` failed any test that generated
    with it (removed in the measurement's commit).
  - **The surfaces.** `evaluate.budget_recall_by_archetype` and `held_out_budget` (the
    model's budget against chance, n x B / P), printed by `score --labels` and on the
    dashboard's tier tab; the summary reads held-out rows as "held out" with `HELD_OUT_NOTE`;
    `DEFAULT_CONTROLS_NOTE` no longer tells a generated ledger the synthetic company's
    controls are "not its own"; `MODEL_TIER_CAVEAT` says only the held-out archetypes measure
    the model as a detector.
  - **The sweep.** `evaluate.sweep` and `docs/tuning.md`: every test with a setting swept on
    the default ledger, pinned by `tests/test_tuning.py`, no default changed (JET-08 at 5
    would catch all seven rare pairs, because four drew the same pair; JET-09 catches no
    dormant entry at any setting). `TUNING_NOTE`, the workpaper and the README no longer say
    the thresholds were tuned: only JET-11's was chosen. `docs/limitations.md` registers the
    measurement before it is made. Version 1.1.0.
  - **The review** (one reviewer, scratch clone, no held-out seed, no held-out scoring) found no
    value read wrong, crash or regression; fixed before the freeze: the caveat and
    `NOT_DEFINITIONAL` now say which test catches the three depends on the draw (JET-08
    catches all seven rare pairs on seed 101), `HELD_OUT_NOTE` no longer says "by
    construction", attribution lists held-out rows first, the seed guard moved to
    `pytest_configure` and covers `_HeldOutStreams`, the held-out line `score` prints is
    pinned whole, and four tests that could not fail were tightened; the docs say where rule
    silence rests on construction and where on seeds, and close three gaps in the
    registration (the environment and versions, no controls file, the old archetypes' total).
    A verification of those fixes (one agent) found nothing the stop rule covers; its findings
    were fixed too: the registration runs every command from a checkout of the tag with
    `PYTHONPATH` set (the venvs' editable install is another checkout's 1.0.0 code), generates
    once and records both files' sha256, and the summary lists held-out rows in
    `HeldOutType`'s order.
  - **Frozen** at `6055581` (annotated tag `held-out-freeze`; CI run by hand green, five jobs).
  - **Measured once** (2026-10-18, 21:50 UTC), as registered: generated in 3.12, both files
    hashed, `test` and `score` run on them in 3.12 and 3.9 with byte-identical output, pasted in
    `docs/limitations.md`. The rule tier caught none of the 43. The model's budget (103 of 5,138)
    held 6 of 43 against 0.86 by chance (7.0x, recall 0.14), all six token amounts: by the
    registered reading it catches `token_amount` (6 of 8) and none of the other four. The
    eleven generator archetypes on the same ledger: 74 of 77 by the rule tier. The commit after
    the tag touches only docs and tests (`git diff held-out-freeze -- src app.py` is empty): the
    measured ledger is a session fixture pinned by `HELD_OUT_LEDGER_SHA256`, and
    `tests/test_limitations.py` runs `test` and `score` on it again and compares with what was
    printed, so CI on Linux reproduces the measurement or fails. Its first Linux run reproduced
    every value but printed the tied rows of `test`'s "Recall by archetype" and "Precision by
    test" tables in another order (one-key unstable sorts in `evaluate`, in `src/`, frozen):
    disclosed in `docs/limitations.md`, and the test compared those two tables' rows regardless
    of order until the tie fix below.
- **Version floors and stable tie order (#17, `fix/pandas-floor-and-tie-order`, before the
  v1.1.0 tag)** — found after the merge.
  - **pandas 2.2.** Since #12 `ingest` imports `pandas.tseries.api.guess_datetime_format`,
    public from pandas 2.2.0 (absent in 2.0.3 and 2.1.4), and reads with
    `to_datetime(format="mixed")`, from 2.0.0, while `pyproject.toml` said `pandas>=1.5`: an
    install pip accepted with pandas 1.5 to 2.1 failed at the start of every command. Found by
    the release notes' claims review. `pyproject.toml` says `pandas>=2.2`, and
    `tests/test_version.py` pins it.
  - **Streamlit 1.49.** `app.py` has given `st.dataframe` `width="stretch"` since #1, which
    Streamlit takes from 1.49.0 (1.48.0's `width` is an int or None), while the `app` extra
    said `streamlit>=1.30`. Found by this branch's review. The extra says `streamlit>=1.49`,
    pinned beside the pandas floor.
  - **Ties.** `recall_by_archetype` breaks a tie on recall by name, and `precision_by_test` a
    tie on precision by test id: the order the Mac printed the measurement in (pandas' default
    sort kept tied rows in order there in a table of up to 16 rows and could scramble them in
    a longer one; on CI's Linux runners it scrambled the 16-row recall table too, and on 3.9
    the 12-row precision table). Two keys go through a stable lexsort, so the order no longer
    depends on the platform's sort, and `tests/test_limitations.py` compares the whole of
    `test`'s output again. No value moves. The other tables that only display rows
    (Benford by MAD, review counts, mean model score by archetype) left a tie to a one-key
    sort until the display-sort bullet below.
- **pandas 3.1 (`fix/pandas-3.1-review-state`)** — pandas 3.1.0 (2026-10-22) keeps a nullable
  integer's dtype through `Series.map` (GH 63903). In `review.review_state` the note shown is
  `Int64`, so in a store with decisions and no note a lookup found nothing, came back `Int64`,
  and `fillna("")` raised a TypeError: the workpaper (`report --db`), `summary` and the MCP
  server's entry rows (`LedgerContext._attach_review`) crashed for such a store, and an empty
  store's note columns turned `Int64`. Found by CI's 3.11 and 3.12 jobs on another branch, the
  day 3.1.0 was released. The lookups now go
  through an object copy, and the two time columns are filled as objects (filling a float64
  column with "" warns in 3.1 and is to raise in pandas 4). Probed on five stores,
  `review_state` gives what it gave before under 2.3 and 3.0.6, and under 3.1.0 what it gives
  under 3.0.6, dtypes included. pandas 3.1 also warned of pandas 4 changes in `ingest.py` (a
  `fillna("")` on a float column, `dt.dayofweek`) and `features.py` (`dt.dayofweek`); nothing
  failed, and they were fixed after v1.2.0 (the pandas 4 bullet below).
- **Materiality and sampling (week 6, PR 1, `week6/sampling`)** — `sampling.py` and
  `ledgerlens sample`. Materiality comes from the command line only (`--materiality`; the
  `--performance` and `--trivial` shares default to 0.75 and 0.05, below 1, to at most six
  places, each figure rounded down to the cent and refused under a cent), every problem named
  at once; numbers built by numpy are read by their value (a float32 by its own text). The
  population is one fiscal year (`--fiscal-year`, needed when the ledger spans several; a
  QuickBooks pull's year is the posting date's calendar year): its entries with a positive
  amount, lines in `line_no` order first; entries of no amount and of a negative one are
  counted apart.
  Monetary unit sampling: R = -ln(1 - confidence) (no expected misstatement; tolerable
  misstatement = performance), n = ceil(book value x R / performance), interval exact
  (`Fraction`), a seeded start, each entry's units counted from its bounds in one pass (no cap
  on n; every entry of the interval or more is certain, and the memo names the whole cent from
  which that holds). Stratified: everything at or above performance in full, a seeded draw of
  `--size` from everything below it in bands of near-equal value, shared by largest remainder.
  Clearly trivial sets nothing aside (the plan had it set aside: on the default ledger that was
  $2.97M, 61% of the book value), and no band is promised a draw; the maintainer accepted both
  changes on 2026-10-22. Only `random.Random(seed).random()` is drawn, so a seed gives
  one selection on every Python; the default ledger's FY2025 selections are pinned by id. The
  memo states every figure, the seed (drawn and stated when not given) and what the population
  leaves out; ledger text through `md_code` / `one_line`; `--out` through the summary's guard,
  now shared (`_checked_out`). `--size`, `--strata`, `--seed` and `--fiscal-year` are checked
  before the ledger is read, each refusal one `error:` line. It reads no flag, model score,
  controls file or label. One review (no value read wrong; one high: numpy floats crashed
  `Materiality`; mediums: the units-versus-entries refusal and its false reason, negative
  entries counted as of no amount, a two-year population against one materiality), all fixed.
  A verification of those fixes found a crash (`--seed=+-5`, a superscript or circled digit
  passed `isdigit` and int() raised; argparse had refused them before) and test gaps (seven
  of eight mutants survived, among them `certain_from` rounded to the nearest cent), fixed
  with a float32 read by its own text, a text `fiscal_year` column and a missing description.
- **Materiality band (week 6, PR 2, `week6/materiality-band`)** — `materiality.py` holds
  `Materiality`, its parsers and `SamplingError` (moved from `sampling.py`, which lends them
  back out; the module imports nothing from `summary`, so `summary`, `report` and the
  dashboard reach it without a cycle), `to_cents` and `fiscal_year_for` (the population's own
  rounding and year check, factored out and called by it), and `band_of_cents` / `bands` /
  `count_by_band`: material, above performance, below performance (at or above each figure),
  clearly trivial (at or below it), no amount, negative, other year. `test`, `summary` and
  `report` take `--materiality`, `--performance`, `--trivial` and `--fiscal-year` as `sample`
  does (one `_add_materiality_arguments`); a share or a year without the figure, or an
  unusable figure, is refused before the ledger is read, a multi-year ledger without the year
  after it, in `fiscal_year_for`'s words. The band goes beside the risk score, never into it
  (rule 3): after each top entry's amount and in `test --out`'s csv; on each summary entry,
  with a block of the figures and the flagged entries by band (its notes first); right after
  the amount on the Exceptions sheet, with the figures on the Summary sheet and a Methodology
  section. Without the flag every output is byte for byte what it was, apart from the version
  string the summary's markdown and JSON print. The dashboard takes the
  figure in its sidebar (a year choice for a multi-year ledger), adds the column, a band
  filter and a fourth metric; an unusable figure is said as text. One wording each:
  `materiality.BAND_NOTE` and `FISCAL_YEAR_NOTE` (the selection memo gained the latter).
  Version 1.2.0.
- **pandas 4 warnings (`fix/pandas-4-warnings`, after v1.2.0)** — pandas 3.1.0 (installed by
  CI's 3.11 and 3.12 jobs; the venvs' 2.3.3 and 3.0.6 print nothing) laid three
  `Pandas4Warning`s at our lines. `Series.dt.dayofweek` is deprecated: `entered_dow`
  (`ingest.prepare`, the weekday JET-02 reads) and `dow_sin` / `dow_cos`
  (`features.build_features`, a model feature) now come through `day_of_week`, the same
  property under its name since pandas 1.2. `load_labels` read `anomaly_type` by its cells, so
  a labels file with every type blank came back float64 (`na_values=[""]` makes a blank
  missing) and `fillna("")` on it is to raise in pandas 4: the column is now read as text like
  `entry_id` and `is_anomaly`. A blank is "" and a type is the text written; read by its
  cells, a type that looked like a number, a boolean or an infinity had been converted (`007`
  to 7, a `7` beside a blank to 7.0, `FALSE` to False, `Infinity` to inf), and the generator's
  archetype names are words, so no labels file the project has written reads differently. The
  `string` dtype is as before, and the `astype("string")` after the fill is gone. No value
  moved: on the default ledger the weekday columns, the feature matrix, the rule flags and the
  model scores are bit for bit the previous code's under pandas 2.3.3, 3.0.6 and 3.1.0, `test`
  and `score` print the same bytes, and `load_labels` gives the same frames and dtypes for the
  default labels and an all-blank copy. Tests pin `entered_dow` to `datetime.weekday` (the
  weekend test compared only `is_weekend` with `entered_dow`, so a day-of-year mutant passed
  it), `dow_sin` / `dow_cos` to the weekday on the unit circle, and a type that looks like a
  number, a boolean or an infinity to the text written (it fails on the previous code), and
  `test_prepare_labels_and_features_raise_no_pandas_deprecation` fails on any deprecation
  warning, pandas' or another library's, laid at a `ledgerlens` line in `prepare`,
  `load_labels` or `build_features`: under 3.1.0 it failed on `1f11203` with the three and
  passes now, and under 2.3.3 and 3.0.6 it passes on both. CI installs the newest pandas, so a
  new deprecation in those three paths fails that test there instead of printing a warning (a
  pandas or numpy release can now fail CI over a warning: the trade-off chosen). pandas 3.1.0
  was run from a `pip --target` folder in the scratchpad put ahead of the 3.12 venv on
  `PYTHONPATH`, neither venv changed, and the whole suite passes under it; the only warnings
  left are Streamlit's own, from its package. A
  review (one reviewer on a scratch clone: 22 hand-written label files under the three pandas
  versions from both trees, every reader of labels downstream, the pandas source for the
  accessor, the new test under each version) found no value read wrong on any labels file the
  project writes, no crash and no regression, and three claims corrected before the push: the
  accessor's name dates from pandas 1.2, not 1.1; the converted spellings above had been
  called "as before"; the release date needed its clock. Version unchanged (1.2.0).
- **Display-only sort ties (`fix/display-sort-ties`, after v1.2.0)** — the three tables that
  only display rows and still left a tie to a one-key sort (the #17 bullet above) have a second
  key through pandas' stable lexsort: `benford.segmented_benford` by MAD then `label`,
  `evaluate.score_by_archetype` by mean score then `anomaly_type`, `ReviewStore.summary` by
  count then `decision` (text by then). Measured here on pandas 2.3.3 and 3.0.6: a one-key
  sort on any key but pandas 3's `str` (numbers, object text, categories) keeps tied rows in
  input order up to 16 rows and scrambles them from 17 (the Linux runners scrambled 16 in
  #17; pandas 3's `str` keys stay in order at any size), and a two-key sort keeps them at any
  size (checked at 20 and 5,085 tied rows). Two cuts that sorted on two keys with no third
  now say how their tie breaks, as the context layer's cuts did: `jets.score_entries` (risk
  score, amount, then `entry_id`; `test` prints its top, and the default ledger ties at the
  tenth row: `JE-2024-005006`, `JE-2024-005009` and `JE-2025-005002` at 4.0 and $75,000.00)
  and the workpaper's re-sort of the Exceptions sheet by risk and model score
  (then `entry_id`). The entries' order could not change: `entry_level` hands the sort its
  rows in id order and the stable sort kept them, so the tenth row was the lower id on every
  platform; the sheet's ties kept the entries' amount order and now go by id, and the default
  workpaper has none. On the default ledger every output is byte for byte what it was: `test`
  (the top ten, `--top 25` and `--out`'s csv of 5,085 rows), `benford --by` account, user and
  source, `score --labels`, `summary --db` and `report` (every sheet, cell for cell, with and
  without the model) on a copy of the review database, and the context layer's
  `review_status` and `benford`; the one tie among them, accounts 1510 and 6800 at MAD
  0.007229 in the 17-row account table, already stood in label order on this Mac, and the
  copy held no decision, so the review table was empty there. One test per sort: twenty tied
  rows in the tie-break order, which is also the order `groupby` hands the sort, plus one row
  `groupby` puts last that the first key puts first; each of the three fails on `b0434a2`
  under 2.3.3 and 3.0.6, passes under both and 3.1.0, and fails again with no sort, with the
  second key dropped, or with it turned round; the sheet's fails on `b0434a2` (ties kept
  amount order) and the entries' only when its key is turned round (the order was already
  the id's). A review (one reviewer on a scratch clone: the threshold on five key dtypes,
  sixty sort sites, the outputs re-run, the tests under a sort-removed mutant) found the two
  unstated cuts, the 17 rows miscounted as 18, the `str` caveat, the tests passing with no
  sort, and the empty review table; all fixed before the push, and its reading of the
  entries' cut as platform-dependent corrected to the above. Version unchanged (1.2.0).
- **Benford's floor (`fix/benford-small-sample`, after v1.2.0)** — `benford_test` gave a
  Nigrini band for any n of 1 or more ("insufficient data" only at 0). Only the CLI warned
  under 300, and it printed the band on the next line. The dashboard's Conformity metric,
  the context layer and the MCP tool showed it with no warning. The recorded sandbox pull's
  297 amounts read "nonconformity" (MAD 0.0278). Under `MIN_SAMPLE` amounts (zeros not
  counted; one flag, `sufficient_sample`, drives the band and every note) the band is now
  "insufficient data", with MAD and chi-square still computed. `benford.SMALL_SAMPLE_NOTE`
  is the one wording, ahead of the figures whenever a test falls short: the CLI's
  population line and once before a segment table, an `st.warning` before the dashboard's
  metrics, and the first key (`small_sample`) of the context layer's result, which the MCP
  tool returns; the workpaper's Methodology section states it, and so does the MCP tool's
  description. Segments are still kept by their rows (`min_n`), so a kept one can be short,
  and the CLI's "had at least N entries" now says rows. On the default ledger every Benford
  output at the default settings is byte for byte what it was, and the workpaper cell for
  cell apart from the two Methodology lines it adds (the rows after them move down two). What
  changes there is a segment kept by a
  lowered `min_n`: at 16 to 299 (and over MCP, whose floor is 50) account 6200's 299
  amounts no longer read "marginal conformity", and below 16 the accounts with fewer rows
  lose "nonconformity" as `min_n` reaches them (6900 at 15, seven of them at 1). 300 is Nigrini's minimum, not a calibration: drawn from the law
  itself, 297 and 300 amounts both read "nonconformity" about a third of the time, 1,000
  almost never (`docs/tuning.md`'s table, each size seeded, recomputed by
  `tests/test_tuning.py`). No baseline for a ledger's own n is built. A review (one
  reviewer, scratch clone) found no value read wrong, crash or regression; fixed before the
  push: the docs had called the floor justified, the context layer put the note after the
  figures, "the one changed reading" missed `--min-n` 15 and below, the mixed and the
  all-sufficient segment tables were untested (four surviving mutants), and two figures were
  unpinned. A verification of those fixes (one agent) found no regression and four lows, also
  fixed: the smaller accounts change one by one as `min_n` falls (the sentence had all seven
  at 15), the chi-square aside now names its rule, the population's keys are pinned whole,
  and the note's first place is pinned at the MCP layer. The note still says the test is not
  meaningful below 300, which is true; it does not say 300 is enough, and `docs/tuning.md`
  says it is not. Version unchanged (1.2.0).
- **The model a note records (`fix/reported-model`, after v1.2.0)** — a saved note's `model`
  was the id the request named (`DEFAULT_MODEL` or `--model`): `narrate` stored
  `NarrationResult.model` and the dashboard `narrator.model`, so a note written by the dated
  snapshot an alias resolves to was filed under the alias. `narrate.response_model` reads the
  response's own `model`; `Narrator.write_note` returns the note and it; `narrate` (through
  `NarrationResult.models`, `model` staying the request) and the dashboard's write button store
  it. A response that names no model, or one that is not text, or is not one line of visible
  text by `controls._visible` (the test an approver's name is held to: `str.isprintable()` and
  `summary.one_line`'s test, so no surrounding or doubled whitespace, control, unassigned or
  format character, or filler drawn blank), or is over `MAX_MODEL_LENGTH` (100) characters, is
  refused (`NarrativeError`, the value quoted through `ascii`): that entry fails and the batch
  goes on, the dashboard shows an error (through `md`, since `st.error` draws markdown and the
  message can quote the response, so no markdown construct forms; a bare URL is still linked,
  as in the note's prose), nothing is stored, and the batch's usage line still counts the
  request. It is never filed under the request,
  which would be the claim removed. The API's schema requires `model` and the SDK types it a str
  (anthropic 0.125.0 and 1.8.0), but the client `Narrator` builds does not check a response
  against its types (the SDK's strict validation is off by default), so a missing, null or
  non-text value reaches the check as sent. `NarrationResult.describe()` prints
  the old line byte for byte when every response named the model requested, and otherwise the
  request and each reported model with its count, by name. `narrate_one` does not read the
  model, so the eval is unchanged: its rows record the model requested (what the runs directory
  and `--regrade` key on), and the grader hash, the committed rows and `cases.json` do not move.
  No schema change, so notes stored earlier keep the model requested and nothing in a row says
  which it holds: its `generated_at` does only against when the program writing to the store
  gained this change, which nothing records (`docs/limitations.md`). The dashboard shows a
  note's model in its caption, and the MCP server's `ledgerlens_explain_entry` returns it with
  `generated_at` for the latest note and every earlier version (the whole row); the workpaper
  and the MCP entry rows show neither. The CLI and dashboard tests fail on `2b113b7` on
  assertions; twenty mutants are killed by named tests. A review (one reviewer, scratch clone)
  found no value stored wrong, crash or regression; fixed before the push: the claim that the
  MCP tools show no model, `isprintable()` admitting a filler drawn blank or a doubled space
  (now `controls._visible`), a refusal quoting the response drawn as markdown in the dashboard
  (an image named as the model would have been fetched; `validate`'s quoted confidence took the
  same path), the reason given for refusing (the SDK does not check its types by default), the
  `generated_at` sentence, and two mutants that survived (a trailing space, `repr` for
  `ascii`). Four of the new tests (a doubled space, the filler, the blank braille cell, the
  escaped refusal) fail on the reviewed commit on assertions, the others pin mutants, and nine
  mutants of the fixes are killed by named tests. A verification of those fixes (one agent)
  found no value stored wrong, crash or regression, and five lows, fixed: the characters only
  the `isprintable()` half of `_visible` refuses (unassigned and some format characters, which
  `one_line` keeps) were untested for a model or an approver (two mutants survived), and four
  wordings (the refusal as "inert", the failing tests, the test's description, the SDK's
  default). Version unchanged (1.2.0).
- **The write button and the SDK's errors (`fix/dashboard-request-errors`, after v1.2.0)** —
  the dashboard's "Write narrative" caught only `NarrativeError`, so the SDK's request errors
  (`anthropic.APIError`: a refused key, no connection, a timeout, a rate limit, an overloaded
  API) escaped the script: Streamlit drew its traceback and stopped the run, and the decision
  form and history below the note were not drawn. (The CLI never had this: `Narrator.narrate`
  takes an exception from one entry as that entry's failure line, and a refused key on the
  first request as one `error:` line.) Now a `NarrativeError` is shown as before, and an
  `anthropic.APIError` as "The request to Claude failed, so no note was stored (Type: text)"
  through `md()`: a status error quotes the server's body. The class is looked up only once an
  exception reaches the clause (`app._sdk_errors`; none without the `llm` extra, where
  `Narrator` refuses first): the SDK reads its own settings (`ANTHROPIC_LOG`) when first
  imported, and the page loads `.env` after its imports, so loading the page must not import
  it. The SDK builds that text from the status code and the body, never the request's headers
  (read in anthropic 0.125.0, 1.8.0 and 2.3.0), so the key appears only if a server's own body
  quotes it. Nothing else is caught: a fault in our own code (building the prompt, reading the
  reply) and a reply the SDK cannot parse (a 2xx whose body is not JSON raises a
  `JSONDecodeError`) are still Streamlit's traceback, never worded as a failed request (the
  save, the flash and `st.rerun()` run after the `try`, only when a note came back). A refused
  save was a traceback too until the store-writes bullet below. Tests raise real SDK errors
  (`APIConnectionError`, a 401 whose body quotes markdown) and fail on `246fbe9` at the
  script's exception; a fault reading the reply, a refused save and a refused reply are each
  pinned as not the request failing, and so are the page loading without importing the SDK
  and the button without it. CI installs
  no Streamlit (`.[dev,llm,mcp]`), so `test_app.py` is skipped on every CI job: the
  dashboard's tests run in the two venvs only (until the dashboard-job bullet below).
  Version unchanged (1.2.0).
- **CI's 3.9 runner (`fix/ci-python-3.9-runner`, after v1.2.0)** — `ubuntu-latest` moved from
  Ubuntu 24.04 to 26.04 between #27's PR run (3 Nov, image 20261025) and its push to `main`
  (8 Nov, image 20261101), and `actions/setup-python` has no 3.9 for 26.04, so `test (3.9)`
  failed at setup before any test ran (the other four jobs passed). The test job's `runs-on`
  sends 3.9 to `ubuntu-24.04` and the others to `ubuntu-latest`, through an expression rather
  than a matrix key, so the check names do not change; `tests/test_workflows.py` pins it. It
  lasts while GitHub keeps 24.04 runners; dropping 3.9 (end of life, but the Mac's system
  Python) is the maintainer's call. Version unchanged (1.2.0).
- **The dashboard's store writes (`fix/dashboard-store-errors`, after v1.2.0)** — the
  dashboard's two writes to the review database had no handling: saving a note that came back
  (`store.save_narrative`) and "Record decision" (`store.record`). A store that refused the
  write raised a `sqlite3.Error`, Streamlit drew its traceback and stopped the page, and a
  refused save lost a note that had been requested (and paid for) without a word. A refusal is
  a read-only file, a lock held past SQLite's five-second wait (a database browser in a
  transaction: one insert by `narrate` or another tab is waited out), a file removed or
  replaced while the note was being requested (`no such table`, `file is not a database`), or a
  disk or iCloud error. (A file removed between clicks is not one: every run opens the store,
  which creates and migrates an empty file, as before.) Each write now catches the store's
  `sqlite3.Error`s and nothing else: `ProgrammingError` and `InterfaceError`, the driver's
  errors for a value it cannot bind, are re-raised first as our own fault (a reply whose
  `summary` was an object got past `narrate.validate`, which read it through `str()`, and
  reached the insert, though the request's structured-output schema asks for text, until the
  narrate bullet below). A refused
  save says the note came back but could not be stored, with the error's type and text and the
  request's usage, and the note is not drawn: a decision must point at advice on record (rule
  8), and this note is not; on a refused rewrite the stored note stays on screen and a decision
  records it. A refused decision says it was not recorded and that what was typed is kept;
  nothing that marks it done is set (the double-click guard, the flash, the draft's clearing),
  so the same submit records it on a retry. Both messages quote the error, so they go through
  `md()`, and the page goes on past them, unless its next read fails too (a file removed or
  replaced during the request: until the reads bullet below). A fault in our own code stays
  Streamlit's traceback, and so do `record`'s own `ValueError`s, with one exception found on
  the way: on Streamlit 1.50 a click lands when the reviewer's name is cleared and the button
  clicked before the rerun that disables it, and `record` refused the nameless decision as a
  traceback that stopped the page (on `main` too). The submit now refuses a missing name in
  words and keeps the draft; Streamlit 1.64 drops that click. #27's test that pinned the
  traceback on a refused save now pins the message. The tests refuse each write with an
  `OperationalError`, a plain `DatabaseError`, an `IntegrityError` and a `DataError`
  (monkeypatched) and through SQLite itself (the file made read-only after the page loaded,
  then writable again for the retry), bind an object from a reply, and check that `TypeError`,
  `ValueError`, `ProgrammingError` and `InterfaceError` are still tracebacks from either write,
  and click Record with the name just cleared; 26 mutants are killed by named tests (the name
  guard's only under Streamlit 1.50, where the click lands; building the `Decision` inside the
  `try` is equivalent). Checked in a browser on a read-only file: the message, the form and the
  draft, then the same click recording once the file was writable. Not changed: the page's
  reads (`get_narrative`, `history`, `summary`, `ledgers`) on a store that fails mid-session
  (until the reads bullet below), and the CLI's `narrate`, which saved only after the whole
  batch, so a refused save there lost
  the notes still waiting (until the narrate bullet below). CI installs no Streamlit, so these
  tests run in the two venvs only (until the dashboard-job bullet below). Version unchanged
  (1.2.0).
- **Narrate's contract and per-note save (`fix/narrate-validate-and-save`, after v1.2.1)** —
  two items the store-writes bullet left open. `narrate.validate` read `summary`,
  `why_flagged` and `suggested_control` through `str()` and kept them as they came, and every
  evidence item through `str()`. The request's structured-output schema asks for strings, but
  the reply is the text of a content block, which nothing on the client checks against that
  schema, so an object, an array, a number, a boolean or null passed under its Python spelling.
  In a text field an object, an array or an integer too large for SQLite reached the store's
  insert as a value the driver cannot bind (the integer as an `OverflowError`): a traceback,
  which in the dashboard lost the paid note with no usage line and in the CLI's `narrate` that
  note and every later one in the batch. Null or NaN in a text field was stored as NULL and
  another number or a boolean as text (`7`, `1`, `Inf`), and an evidence item of any kind as its
  Python spelling (`None`, `{'a': 'b'}`): a store written before this change may hold such rows,
  and nothing marks them. Text holding a lone surrogate (an unpaired surrogate escape is valid
  JSON) passed as well, and the insert raised `UnicodeEncodeError`, which is not a
  `sqlite3.Error`: the same traceback (found by the review; on `main` too). `validate` now
  refuses such a reply (`summary must be text, got dict`, `evidence_to_request item 2 must be
  text, got null`, `summary is not text the store can keep: it holds a lone surrogate`, which
  quotes none of the reply) like any other break of the contract: one failed entry in the CLI's
  batch, which goes on, and an error in the dashboard, whose page goes on (#30's test that
  pinned the traceback now pins the refusal). A valid reply comes out as before, except that an
  evidence item's lines (split at a carriage return or a line feed) are joined with one space:
  the store keeps the items one per line, so it read an item holding a line feed back as two
  (since the store was written; found by the review). `validate` is the schema check the
  eval's grader calls, so the grader's sha256 moved
  from `0273c171…` to `58593980…` (the maintainer chose this over a check on the storage path
  only, which would have left the eval grading such a reply `schema_valid`): the fourth
  `GRADER_NOTES` entry and an offline `--regrade` of the 2026-09-23 run ship with it (run once
  from `f366c35`'s rows under the final grader, so each row keeps one new grade); every stored
  field was already text and no evidence item held a line break, so no check moved on any row
  and the score stays at 94%, and the rule-9 test now pins that each row's grade before that
  re-grade is its grade. The CLI's `narrate` also saved only after the whole
  batch, so one refused save (a read-only file, a lock held past SQLite's five-second wait, a
  file replaced during the run, a disk or iCloud error) was a traceback that lost every paid
  note still waiting. `Narrator.narrate(..., keep=)` now hands each note to
  `keep(entry_id, note, model)` as its response comes back, before the next request; `keep`
  raising `narrate.NoteNotKept` fails that entry and stops the batch, so nothing more is
  requested (a store that refused one write will most likely refuse the next, and each request
  is paid for), and anything else it raises propagates. `NarrationResult` records `stopped_at`
  and `not_requested`; `describe()` is unchanged. The CLI's `keep` stores the note, turns a
  store refusal (`sqlite3.Error`) into `NoteNotKept` with the error's type and text, and leaves
  `review.CALLER_FAULTS` (`ProgrammingError`, `InterfaceError`; the dashboard's
  `_OUR_SQLITE_FAULTS`, moved) a traceback, as it does any other exception; the FAILED and stop
  lines print the entry id through `summary.one_line`. A stopped batch prints the usage line,
  the failure, and the entry it stopped at with the number of queued entries not requested, and
  exits 1 whatever was stored before it. When every note is stored, the output and the stored
  rows are what they were, compared with `f366c35` under a scripted client (an all-success
  batch, a run that skips, a batch with one cut-off reply); each note's `generated_at` is now its
  own save time. Not changed: `confidence`'s check (no JSON value but a string reads as high,
  medium or low through `str()`); keys a reply adds beyond the five (the review store keeps none
  of them); the dashboard's message for a refused reply, which carries no usage line, as for
  every other refused reply. 60 new test cases: 32 of the first commit's 33 new or changed ones
  fail on `f366c35` (the one that passes pins the unchanged cleaning of a valid reply), and so
  do the second's nine CLI cases; 11 of the review fixes' 15 fail on `8e7a550`, and so does
  the verification's one (the eval's own path for such a reply); the rest (the four `keep`
  cases, which need the new parameter, and the fixes' four guards) are checked by mutants: 23,
  each killed by named tests. A review (one reviewer, then a skeptic, one at a time, in a
  scratch clone) confirmed
  seven of the reviewer's eight findings and part of the eighth: the lone surrogate (medium: a
  crash, on `main` too); the CLI's save untested against a fault that is not the store's (a
  mutant catching `Exception` there survived every test); the auth path untested with `keep`;
  entry ids printed raw; and wording (the SDK's strict validation, which checks only the
  response's envelope; what was stored before; `validate`'s docstring; a commit message's claim
  that the same command picks up where a stopped batch left off, when under `--force` it starts
  over and pays again, as the printed line says). The skeptic added two: the same surrogate
  reaching the eval's report, and the evidence line break (a value read wrong, older still).
  All were fixed before the push. A fix-verification (one agent) found every fix closed and no
  regression (a differential fuzz of 60,000 replies against `8e7a550` and `f366c35`, the CLI's
  output and rows unchanged on five batches, the re-grade reproduced from `f366c35`'s rows), a
  test gap for the eval's own path and wording (fixed), and, outside this branch, the
  workpaper's crash on a control character (the cell-text bullet below). Version unchanged (1.2.1).
- **The workpaper's cell text (`fix/workpaper-cell-text`, after v1.2.1)** — the workpaper's
  text went to openpyxl unguarded (only `_text_not_formulas` touched it, for formulas).
  openpyxl refuses a C0 control character other than tab, line feed and carriage return
  (`IllegalCharacterError`), so a vertical tab (Word's line break, pasted into a note) in a
  ledger cell (a user, which JET-02, 03, 04 and 12 quote; an account code on the Benford
  sheet), a decision's note or reviewer, or a note's text stopped `report`, and the review store
  is append-only: a store holding one could never write its workpaper again (found by the
  narrate branch's fix-verification; on `main` too). openpyxl
  writes U+FFFE and U+FFFF (valid UTF-8 in a CSV, valid JSON in a reply) and half of a surrogate
  pair without a word, and the file then fails to load (`ParseError`): `report` said it had
  written a workpaper that would not open. And it cut a text over 32,767 characters there in
  silence (pandas 3 warns on stderr, pandas 2 not at all): a reviewer's long note, or the
  reasons of an entry with hundreds of flagged lines (found while planning this; the maintainer
  chose to fix it here). `report._CellText` now fits all the text the workbook carries but its
  own fixed headings: the Exceptions, All flags and Benford frames (cells and column names)
  before `to_excel`, and every Summary label and value and Methodology line (the Methodology's
  lines moved into `_methodology()`, so they are fitted before the Summary sheet is written and
  its count is whole). A character XML 1.0 leaves out (`_UNWRITABLE_RANGES`) is shown where it stood as its
  code point in brackets (`[U+000B]`: ASCII, so any font draws it, and not the file format's own
  `_xHHHH_` escape). A text longer than `CELL_LIMIT` as shown, code points in brackets included
  (32,767, counted in UTF-16 code units, so a character past U+FFFF, as many emoji are, takes
  two: the stricter count, since openpyxl counts code points; Excel's LEN counts a surrogate pair
  as two unless the workbook is set to its Compatibility Version 2, and Excel was not opened to
  see which count its cell limit uses), keeps what fits and ends with ` [cut: shown in full, the
  text would run to N characters; a cell holds 32,767]`; the cut is measured on the text as
  written, so no code point in brackets is cut in half, and a character too wide for the room
  left ends it. Only the cells that need it change, on a copy: a column with one to change is
  set back by position from an object array (a categorical cannot take a value it did not have;
  pandas 3 may store the column as text again), and every other cell, blanks included, and the
  caller's frames stay as they were. When anything
  changed, the Summary sheet counts it right after "Shown in this workpaper" (`Characters a cell
  cannot hold`: N in M cell(s); `Texts cut to fit a cell`: K) and the Methodology sheet says so
  after Limitations (`UNWRITABLE_NOTE`, `CUT_NOTE`, one wording each): only the workpaper shows
  the text this way, and the ledger and the review store are not changed (`docs/methodology.md`
  says so too). A workpaper that needs neither is written as before: the same cell for cell
  (value, type, number format, bold, wrap, fill, widths, panes) as `7c702e0`'s on the default
  ledger with the model, labels and a copy of the review database, with materiality, and on the
  sandbox pull, under 3.9 and 3.12. Not changed: a carriage return is written as it is and read
  back as a line feed (XML's line-end handling), the same break; text already holding a literal
  `[U+000B]` reads like a replacement (the Summary's count tells them apart); a literal
  `_xHHHH_` is written as it is, as before, and the file format reads it as an escape, so Excel
  may show the character it names (not checked in Excel; openpyxl reads it back as written);
  `build_workpaper` still returns the path and the CLI prints no extra line (the workpaper is the
  record). Twelve tests: a ledger cell (the user, and the reasons quoting it on Exceptions and All
  flags), a decision's note and reviewer, and a note's text through `build_workpaper`, and all
  three through `report`; U+FFFE, U+FFFF, a surrogate (in the controls file's path) and a model
  report, whose file now opens; each edge of the set (a Benford label per code point either side
  of it); the set against openpyxl's own list over every code point; the cut (40,000 characters,
  exactly 32,767, 32,768, 20,000, 16,384 and 16,383 emoji, a wide character at the cut, a cut
  beside a run of markers, a text a cell would hold as written but not as shown); a categorical
  and an Arrow column; a caller's frame with a control character in a column name and repeated
  index labels and column names; and nothing said when nothing changed. Seven of the first nine fail on
  `7c702e0` with only the new constants added (five on `IllegalCharacterError`, one on a file
  that will not load, one on the silent cut); the pin and the guard pass there. Mutants on a
  scratch clone, one at a time: 35 against the first commit, 33 killed, and the pins added for the
  other two (a text only cut counted as a changed cell; the caller's frames rewritten in place)
  kill those. A review (one reviewer, scratch clone) found no crash or regression on input the CLI
  can produce, and one medium: a text a cell would hold as written was cut for what its code
  points in brackets took, with a tail giving the length as written ("runs to 32,200 characters;
  a cell holds 32,767"); the tail now gives the length as shown. And five lows: four fixed (three
  surviving mutants: the half-limit threshold raised, a wide character at the cut skipped where it
  should end it, the count rows after the blank row, which `_summary_cells` drops; a categorical
  or Arrow text column left unfitted, which the CLI's frames never hold but `build_workpaper`
  takes the caller's; and two wordings, "counts an emoji as two", where it is any character past
  U+FFFF, and "the ledger and the review store still hold the original", not true of a reasons
  cell, a path or a model report), and the `_xHHHH_` escape, recorded above as not changed. Of
  the tests the review changed or added, the cut-beside-markers, categorical and Arrow tests fail
  on the reviewed commit for the defects they pin; the other two changes pin the three mutants.
  A fix-verification (one agent, scratch clone) found every finding closed and no value written
  wrong, crash or regression (18 tail boundaries up to a seven-digit length; 35 frames of every
  dtype under both pandas versions, matching the reviewed commit wherever it could write), and
  eight lows and very lows, all fixed: a caller's column name holding a control character still
  raised `IllegalCharacterError` (now fitted, as the cells are); nothing pinned setting a column
  back by position (a by-name mutant wrote one repeated column's text into both); and wording
  (this bullet's count of the review's lows and of the tests that fail on the reviewed commit,
  "written back as objects", the class docstring, `docs/methodology.md` not yet saying the
  ledger and the review store are not changed; `5ccbe69`'s message says the convention below no
  longer claims every text goes through `_CellText`, which only this commit makes so). The new
  test fails on `5ccbe69` (`IllegalCharacterError` on the header), and the by-name and
  names-not-fitted mutants are killed. Version unchanged (1.2.1).
- **The dashboard's tests in CI (`ci/dashboard-tests`, after v1.2.1)** — CI's test jobs
  install `.[dev,llm,mcp]`, so `tests/test_app.py` skipped itself on every job and the
  dashboard's 73 test cases ran in the two venvs only (the #27 and #30 bullets above). So
  did `test_an_arrow_text_column_is_fitted_too` in `tests/test_report.py`, which skips
  itself without pyarrow. `ci.yml` now has a `dashboard` job for the two files, on Python
  3.9 and 3.12: it installs `.[dev,llm,app]`, imports what the tests would skip without
  (Streamlit's test harness, pyarrow, anthropic) in a step of its own, and runs `pytest -q
  tests/test_app.py tests/test_report.py`. The import step is what turns a missing package
  red: with two files collected, a file that skipped itself is a green job (collected alone
  it is not: pytest exits 5), and the step's log names the versions that ran. On 3.9 the
  extra brings Streamlit 1.50.0, the last version Python 3.9 can install, and on 3.12 the
  newest (1.67.0 on 2026-11-16; the 3.12 venv has 1.64.0), so a new Streamlit can now fail
  CI before the venvs see it, as a new pandas can. The extra stays out of the test jobs:
  Streamlit requires pyarrow, and with pyarrow installed pandas 3 keeps text columns in
  Arrow (without it, in Python objects), so adding it there would end the suite's only runs
  on pandas 3 without pyarrow (the 3.11 and 3.12 legs; 3.9 runs pandas 2.3), which is what
  an install without the `app` extra has. A step in the test job now fails if pyarrow can be
  imported there. Not changed: the rest of the suite is not run in CI with pyarrow installed
  (the 3.12 venv runs it so, on the pandas it has); one test still runs in no CI job, by its
  own condition (`test_a_differently_cased_spelling_of_the_checkout_is_still_refused` in
  `tests/test_tokens.py` skips on a case-sensitive filesystem, which every runner has); and
  the jobs on one Python share a pip cache key, and an entry that was hit is not saved
  again, so each job downloads on every run what the job that wrote the entry did not
  install (with today's entries, the dashboard job downloads Streamlit's wheels).
  `tests/test_workflows.py` pins both jobs line for line, comment and blank lines aside,
  because a line beside the ones that matter changes what they do (an `if`, a
  `continue-on-error`, an `env`, a second install); runs the two guard steps as the runner
  would (each step's script as `ci.yml` has it, through `bash -e`, where `python` is a
  stand-in that records what it is handed and runs it with only stand-in packages
  importable); pins the workflow's triggers, its three top-level keys and the names of its
  four jobs; pins every line of the two gates (`rule-1` and `detection-quality`) outside
  their scripts; and holds the three workflow files to printable ASCII and line feeds. The
  gates' scripts are not pinned: as before, one test reads rule 1's pattern and another runs
  the detection gate's script, which must now be its one command and here-document. A review
  of the first commit (one reviewer, scratch clone) found no fault in the job as written and
  three mediums in what was tested and said: 28 of its 40 mutants of the workflow passed the
  two tests that commit had, which matched six lines; the reason given for the import step
  was false for a job that collected one file; and the pyarrow test above still ran nowhere
  in CI. Those and its wording lows (which legs run pandas 3, what the two venvs cover, what
  the check before the push ran under) are fixed. A fix-verification (one agent, scratch
  clone) found no fault in either job as written, one medium in the tests and seven lows.
  The medium: a job's lines were read up to the first line at the jobs' indent, and the
  comment above the next job is one, so a key written below that comment (still the job's,
  in YAML) was in no job's pinned lines: twelve such mutants passed (a `continue-on-error`,
  an `if`, an `env`, a `shell`, a `working-directory`). A job is now read with the comment
  lines taken out first. The lows: a fifth job passed the four-jobs pin if its key line was
  quoted, carried a comment or held the job in braces (every line at the jobs' indent is
  compared now); the two gates could be switched off beside their scripts (first a pin that
  no `if` or `continue-on-error` is anywhere in the workflow, then the pin of the gates' own
  lines); the two guard tests ran the test's own copy of each step's Python, in the folder
  pytest was started in: started where the real packages were they failed, and two
  weakenings made in the workflow and in that copy at once passed (an import of
  `streamlit.testing` alone; `python -S`, under which a job's interpreter does not look in
  site-packages), which is why they now run the workflow's own lines; and wording (which
  releases CI installs that the venvs lack, which job downloads wheels, the test that runs
  in no CI job, two paragraphs left unwrapped). A claims audit of the same commit (one
  agent) found the same hole, by nine mutants of its own, and more wording: the first
  commit's two tests matched six lines, not five; the two venvs do not "run every code path"
  (CI's coverage is 97% and 98%: they run the suite under both SDKs, and the MCP tests); 26
  of the workpaper's 27 tests need neither package and already ran on Linux; the squash body
  had dropped "over the venv's other packages". A last check of the two fix commits (one
  agent; each finding was reproduced before it was fixed) found one medium and six lows. The
  medium: these tests end a line, and so a comment, at a line feed, and a YAML parser also
  ends one at U+0085, U+2028 and U+2029, so a key written after a comment and one of those
  characters was a key to the runner and a comment to every pin (eight mutants of `ci.yml`,
  one of `weekly.yml`). The three workflow files are now held to printable ASCII and line
  feeds. The lows: the no-`if` pin did not keep a gate from being switched off by a `shell`
  key, a `defaults` or an unusual spelling of `if`, and a line of shell before the detection
  gate's command passed (hence the pin of the gates' lines, which replaces it, and the
  script that must be its one command); the guard tests ran a step's script and read none of
  its other keys, so a `shell` added in the workflow and in the pinned lines at once passed
  (the step must now be its name and its script); the mutant count said 138 where seven
  repeated an earlier one; a comment called a `v1` with no `AppTest` a skip in
  `tests/test_app.py`, where it is a collection error; a temporary folder whose path holds a
  colon split `PATH`, which failed the guard tests and let the older Issue-step tests find
  the real `gh` before their stand-in (it was handed the tests' own token and refused, so
  nothing was created; both now skip there); and the cause given for the old guard tests'
  dependence on the folder. Mutants against the final tests, one at a time on a scratch
  copy: 170 runs of 160 different mutants, of which 156 are each killed by a named test. The
  other four: one adds only a comment as a job's first line, which changes nothing the
  runner reads (the earlier tests failed on it, because they ended a job's text at a
  comment), and three are guard scripts written, in the workflow and in the pinned text at
  once, to tell the tests' harness from a runner (an empty folder, a stripped environment,
  `-S`), which takes intent. The two job pins and the two guard tests fail on `f7f4b21`'s
  `ci.yml`, and they and the four-jobs pin on `c5ec5d3`'s. Before the push the job's two
  commands passed in the 3.9 venv, whose Streamlit, pandas, numpy, pyarrow, anthropic and
  pytest are the ones the 3.9 leg resolves to, and on 3.12 under the ones the 3.12 leg
  resolved to that day (1.67.0, 3.1.1, 2.5.5, 26.0.1, 2.8.0 and 9.2.0, from a `pip --target`
  folder over the venv's other packages; neither venv changed). CI's checks go from five to
  seven, and the pull request's own run was the first on Linux for the dashboard's tests:
  100 passed on each of the two new legs (2026-11-16). Version unchanged (1.2.1).
- **The dashboard's reads, the store's open and the decision draft
  (`fix/dashboard-store-reads`, after v1.2.1)** — the two items the store-writes bullet and
  the last Next left open, and more found on the way and by its reviews.
  - **Reads.** The page opens the store once a run and then reads it at seven places
    (`other_ledgers`, `ledgers`, `current`, `summary`, `decided_ids`, `get_narrative`,
    `history`), each on a connection of its own, so a file that opened can refuse the next
    read: locked past SQLite's five-second wait, removed or replaced while a note was being
    requested, a disk or iCloud error. That was Streamlit's traceback. `app._read` wraps
    each one: the store's refusal is a message through `md()`, with SQLite's own type and
    text, and the run ends there (`st.stop()`), because what the page draws next is built
    on what could not be read (the legacy check that gates writes, the queue's decisions,
    the note a decision must point at) and no write may follow a read that failed.
    `history` is the exception: it is the last thing the queue draws and nothing is built
    on it, so the message stands in its place and the other tabs are drawn (the case the
    store-writes bullet named: a file replaced during a request refuses the save and then
    this read). Five of the seven go through `pd.read_sql_query`, which raises
    `pandas.errors.DatabaseError`, an `OSError` and not a `sqlite3.Error`, with what SQLite
    raised as its cause: both are caught, and the cause is what is shown. Still a
    traceback: `review.CALLER_FAULTS`, raw or wrapped by pandas, a pandas error with no
    SQLite refusal behind it, and anything else of ours. Two of the reads that end the run
    (`decided_ids`, `get_narrative`) sit inside the first tab, where `history` is read too:
    their refusal is drawn above the tab bar, in a placeholder made before the tabs,
    because said inside that tab it left the other three blank with no word.
  - **The open** (`review.py`, so every caller has it). A probe of seven kinds of file found
    three faults. A folder that cannot be made (its own parent is a file) escaped as an
    `OSError`, and an empty file that is read-only as `OperationalError` from the schema
    statements every open runs: a traceback in the dashboard and in `narrate` (and, for the
    second, `adopt-legacy`), which show a `RuntimeError` from the open as one line. Both are
    `RuntimeError`s now (`CALLER_FAULTS` in the schema statements are re-raised as they are).
    And a file another program held locked past the wait was reported as "is not a SQLite
    database", because every `sqlite3.DatabaseError` while the schema was read was; an
    `OperationalError` there now reads "could not be read as a SQLite database", by both
    openers. The dashboard draws the open's message through `md()` too (it names the path as
    typed). The read-only opener's own check (of the schema and the append-only guard) is
    put into words the same way, and `adopt-legacy` prints one `error:` line when a file
    that opened then refuses the copy (read-only with rows to adopt, or locked by another
    program): it was a traceback.
  - **The draft that stayed.** After a recorded decision the page removed the draft's two
    keys from session state. That resets the value for the script and sends the browser
    nothing: a browser went on showing the choice and the note just recorded and sent them
    back with the next submit, on Streamlit 1.64 as on 1.50 (the last Next said 1.50). The
    keys are now assigned, which reaches the browser as the widget's new value
    (`set_value`): the note to empty and the choice to none. The form has no preselected
    decision any more (`st.radio(index=None)`), and a submit with none chosen records
    nothing and says so. Cleared back to "accept", as this change first had it, the form
    recorded an accept with an empty note at a second click, as the entry's latest decision:
    an escalation became an accept the reviewer never chose. (While a browser still showed
    the decision just recorded, the fault fixed here, the double-click guard refused that
    click.) A click that arrives on the run that clears the form carries the decision just
    recorded and is refused as the duplicate it is.
  - **The draft that was lost.** Found in the browser while checking the above: a run that
    stops before the form (the open's stop, on `main` already, and now a read's) takes the
    form off the page, and when the store answered again the form came back empty, so the
    decision being recorded was gone (1.50 and 1.64). It takes every control below the stop
    off the page as well, and a browser starts a control it is shown again from its
    default. So `app._stop_for_the_store` does two things with what the stopped run had not
    drawn. It sets aside the form's values and the controls that say which entry they were
    for and whether a decision may be written (the entry selector, "Outstanding only",
    "Start this ledger's review from scratch"), and each takes its value back by assignment
    when it is next drawn: a form only its own entry's draft, the selector only an entry
    still in the queue. And it forgets the queue's other filters and the fiscal year, so
    the script starts them from their defaults as the browser does (the filters are keyed
    for that, each key holding the filter's options; Minimum amount has none). A filter the
    run had drawn is still on the page and is kept. Three ways this change first had it,
    each found by review. Given back to the form alone, the draft returned only for a
    reviewer on the queue's first entry: on any other the browser's selector was back on
    the first, and the next click was thrown away. With the filters left in the session,
    the next run drew the queue from filters the browser no longer showed, and under
    Streamlit 1.50, where the entry selector is another control once its options change,
    the click after that went back to the queue's first entry and an empty form, and
    recorded nothing. And a decision recorded on the run before, whose form is waiting to
    be cleared, was set aside as a draft too: it came back into the form later, the next
    decision submitted for that entry was refused as "just recorded", and it could be
    recorded a second time. The caption under the message says what comes back and what
    starts over, says to rerun with the menu's Rerun, and says that reloading the tab
    starts a new session and loses what was being recorded. A pending "Recorded: ..." is
    drawn above the error: a decision recorded on the run before is still said once, where
    an error alone, straight after the click, read as if the click had failed. (The page
    itself says it only once the entry's note has been read: said before that read, a stop
    there left it below the error, inside the tab.)
  - **Tests.** 105 new cases (96 in `test_app.py`, six in `test_review.py`, three in
    `test_cli.py`): each read refused by each kind of store error, raw and through pandas;
    the two reads behind a condition (the legacy check's, the outstanding filter's); the
    file replaced between the open and a read, and during a request, through SQLite and
    pandas themselves; the faults of the open, the lock held by a real second connection;
    both drafts by what the browser is told (`set_value` on the widget's message, which
    AppTest exposes), on an entry that is not the queue's first and with "Outstanding only"
    ticked; a form with no decision chosen, a second click, and a click that arrives on the
    clearing run; a decision recorded just before a stop (before the tab bar and inside the
    tab), the next one for that entry, and a new one for an entry already decided; what a
    stopped run forgets (each of the five filters and the fiscal year) and what it keeps;
    the selector and the two boxes given back once, and either box left to the reviewer
    where the stopped run had drawn it; a start-from-scratch tick given back to its own
    file's box and no other, and never another ledger's on the same file; a draft that
    waits through a second stop while another entry is on the page; the confirmation said
    once, directly under the selector, on a healthy page; the open's and `adopt-legacy`'s
    refusals with more than one kind of error. 74 fail on `c5ec5d3` on both Pythons; the
    other 31 pin what must not change (a fault of ours is still a traceback; the same
    decision sent again is refused; the next entry's form is left alone; the confirmation
    stands where it stood; a tick for one ledger is not another's). The fourth commit's six
    also fail on `e5af5de`, two of them (the filters') on reading a key the filters did not
    have there, so what those two pin is shown by mutants; the fifth's pass on the fourth,
    and what they pin is shown by the 14; of the sixth's six, three fail on `e5af5de` (the
    two for a stop inside the tab, which are that commit's regression, and the tick's
    return with its file) and all pass on the fifth, whose code the sixth does not change,
    so what they pin is shown by its mutants; the seventh's two likewise pass on the sixth.
    Up to Streamlit 1.65 AppTest forgets more than a session does after a stopped run (it
    drops the state of every control it did not send, where a session keeps what the
    stopped run did not reach; from 1.66 AppTest keeps it too: read in the tagged source,
    and run here under 1.50, 1.64 and 1.67), so these tests read what the browser is told
    and what the session holds as the run stops, and the flows were checked in a browser on
    Streamlit 1.50 and 1.64. What the browser is told after a stop is read as a session
    would have it only from Streamlit 1.66: two plausible slips (the give-back assigning
    only what the session no longer holds, which is the first review's high again, and a
    stop that consumes the pending clear) pass every dashboard test under 1.50 and 1.64 and
    fail under 1.67. So CI's 3.12 dashboard leg sees what the two venvs cannot, until the
    3.12 venv has a Streamlit of 1.66 or later. On the third commit: a decision typed on
    the queue's third entry with "Outstanding only" ticked, the file made unreadable at the
    click and readable again, comes back with its entry after a rerun and records once; the
    form then holds no choice, and a second click records nothing. On the fourth: the same
    with the Severity filter narrowed instead (13 of the 16 entries in the queue of the
    small ledger the check ran on) and an entry further down picked: after the rerun the
    filter and the queue are back at their defaults, the entry and the draft are back, and
    the click records once. The third commit in that flow under Streamlit 1.50: the rerun
    drew 13 entries under a filter that showed all three severities, and the click went
    back to the queue's first entry and an empty form and recorded nothing. Before the push
    the dashboard job's two files were run under Streamlit 1.67.0 as well, the newest then,
    which CI's 3.12 dashboard leg installs (with pandas 3.1.1, pyarrow 26.0.1 and pytest
    9.2.0, from the `pip --target` folder #35 was checked with): 196 passed. Mutants, one
    at a time on a scratch copy: 42 of the first two commits, 20 of the third, nine of the
    fourth, the 14 of the test-strength review's 18 that had survived, and the six of the
    fourth round's ten that had (both below), three written for the sixth commit's own
    tests, and the last check's four survivors. Each was killed by a named test in at least
    one of the two venvs when it was run; two depend on Streamlit's version. One of the
    nine, a filter's key without its options, is killed under Streamlit 1.64 and 1.67 and
    not under 1.50, which makes the options part of a keyed control's identity by itself.
    One of the 20, assigning an entry that has left the queue, is killed under 1.50 only,
    where the selector takes the value and the page would inspect an entry that is not in
    its queue: from 1.64 Streamlit falls back to the first entry by itself, and the mutant
    changes nothing a session shows. (The fifth commit's test read what the browser is told
    on that run, to kill it under 1.64 as well. That held only in a harness that had
    already forgotten the selector, and failed under Streamlit 1.67: see Review.) A third
    did until the seventh commit: one of the 42, a second stopped run that empties what the
    first set aside, was killed only where the harness had forgotten the form by the second
    stop, so up to Streamlit 1.65, and passed every dashboard test under 1.67; the seventh
    commit's test kills it under 1.50, 1.64 and 1.67. The sixth and seventh commits'
    mutants were run under all three. The last check and its skeptic re-ran under 1.67 what
    still applies to the final text (the nine, the fifth commit's fifteen, and 29 of the
    first 62): no other depends on the version, and one of the 42 has been an equivalent
    mutant since the fourth commit (nothing of a recorded decision waits any more). The
    other 22 of the 42 (the reads, the open, the markdown) carry no state from one run to
    the next and were not run again.
  - **Review.** A correctness review of the first two commits (one reviewer, scratch clone)
    found two highs, one medium and six lows, all fixed in the third commit. The highs: the
    draft given back to the form alone, so lost on any entry but the queue's first, and the
    accept recorded at a second click. The medium: the draft tests ran on a harness that
    forgets what a session keeps (said under Tests). The lows: `adopt-legacy`'s traceback
    and the read-only opener's raw error (both under The open); a caption that told the
    reviewer to wait where waiting cures nothing and whose "press R" read as a reload; the
    order of the confirmation and the error, which no test pinned; a refusal inside the
    first tab that left the other tabs blank; and totals written against an older base. A
    verification of the third commit with a claims audit (one agent, scratch clone) found
    those closed for what they named, and two more highs, both said above: the recorded
    decision set aside as a draft (a regression of the third commit), and the filters under
    Streamlit 1.50 (found in a model of a session and a browser, AppTest with the page hash
    carried across runs and each newly drawn control given the value a browser would hold,
    then reproduced in a browser: under Tests). And five lows: the first rerun drew the
    queue from filters the browser no longer showed (gone with the filters' fix); the
    start-from-scratch tick was not kept, so after a stop the box showed unticked while the
    script held it ticked, and the next click was thrown away; the confirmation stood below
    the error when the run stopped at the note's read; "where it is an error" for a value
    that is no option, which Streamlit 1.50 takes; and wording (the caption promised back a
    decision that had only been typed, and one that another file, its second way out, does
    not show; "three multiselects" where a materiality adds a fourth; `adopt-legacy` said
    "could not be written" of a refused read as well; a comment on the form's handler). All
    are fixed in the fourth commit but two that sit in commit messages: `ca1e4bf`'s gives
    the suite as 1438 / 1453 where its own CLAUDE.md, after the rebase, says 1443 / 1458,
    and `e5af5de`'s says Streamlit 1.50 refuses a value that is no option. A test-strength
    review of the third commit (one agent, scratch clone; 18 mutants of its own, each run
    alone on both Pythons) found by itself the same regression and the confirmation below
    the error at the note's read, both fixed in the fourth commit by then, and no other
    fault. 14 of its mutants survived the tests they were run against, the third commit's
    (one of them, the confirmation drawn where the run stopped, was already killed by the
    fourth commit's test for the note's read). Nothing pinned that the selector and
    "Outstanding only" are given back once, that a box the stopped run had drawn is not set
    aside, where the confirmation goes at a stop inside the tab, that the clear is for the
    recorded entry's own form, or that an entry let go stays let go; one assertion held
    under Streamlit 1.50 only because two runs drew as many elements; and each handler of
    the open, and `adopt-legacy`'s line, had been tried with one kind of error and an
    ordinary path. The fifth commit is those tests (six new cases, six tests extended, no
    change to the code): all 14 are killed on both Pythons. A verification of the fourth
    and fifth commits, a claims audit and a skeptic over both (three agents, one after
    another, scratch clone) refuted nothing and found one thing to fix before the push, in
    a test of the fifth commit: `test_an_entry_that_left_the_queue_meanwhile_is_let_go`
    failed under Streamlit 1.67, which CI's 3.12 dashboard leg installs. It read that the
    browser is told nothing about the selector on the run that lets the entry go. AppTest
    from Streamlit 1.66 carries the page it was on from one run to the next, so it keeps
    what a stopped run did not reach, as a session does: the selector still holds the entry
    that left, the page assigns nothing, and Streamlit falls back to the first entry by
    itself and tells the browser so. The page is right under 1.50, 1.64 and 1.67; the
    assertion described a harness that had forgotten the selector, not a session, and is
    gone. The rest is answered in the sixth commit, which changes tests and, in `app.py`,
    two comments and a docstring: no code. Six of the verification's ten mutants had
    survived the tests they were run against: what is left out at a stop was pinned for a
    stop before the tab bar only, and nothing told the entry whose form waits to be cleared
    from the entry last decided; nothing pinned that the start-from-scratch box is given
    back once, or left alone where the stopped run had drawn it; the forget test narrowed
    two of the five filters; and nothing pinned where the confirmation stands on a healthy
    page (six new cases, three tests extended). Two behaviours it found are named under Not
    changed (the tick's return with its file, and a note written just before a stop under
    an older Streamlit). And wording, corrected in this bullet: what AppTest forgets, by
    version; what the test-strength review found by itself, and what its 14 mutants had
    been run against; "the draft given back to the wrong entry"; which reads sit in the
    first tab; the amount's key; and, under Next, a second reason to record the GIF again.
    A last check of the sixth commit, with a skeptic over it (two agents, one after the
    other, scratch clone), found no fault in the page and no test that fails, and one
    medium and five lows, each reproduced and answered in the seventh commit (tests and
    wording; no code). The medium: "but for two" was not so, a third mutant depended on
    Streamlit's version (under Tests), and from Streamlit 1.66 no test read that a second
    stop adds to what the first set aside. The lows: the confirmation test passed with the
    confirmation drawn twice, or inside the first score column (it now reads one, directly
    under the selector); nothing pinned the ledger half of the start-from-scratch key,
    which is `main`'s (a case added); "under Streamlit 1.50 only" said too little of the
    note written before a stop (Not changed); the two slips only Streamlit 1.66 or later
    lets a test see (under Tests); a test docstring that still had two reads in the first
    tab; and the lists of commit messages, which follow. Three more commit messages stay in
    the list with statements that are not so, and `e5af5de`'s holds a second one:
    `f376fa9`'s says that the review found one fault and that its 14 mutants passed every
    test, and counts as killed on both Pythons the mutant only Streamlit 1.50 kills (it
    was, by the assertion that is gone); `9f5c8a3`'s says that each of its nine mutants is
    killed by a named test (one is not under Streamlit 1.50), and it and `e5af5de`'s say of
    AppTest what holds up to Streamlit 1.65; `75e41d2`'s says "under Streamlit 1.50 only"
    of what holds before 1.64.
  - **Not changed.** A draft typed but not yet submitted when another control reruns the
    page into a failed read is still lost: the browser had not sent it. A browser reload
    starts a new session, so nothing is kept across one (the caption says so). A draft set
    aside comes back with the file it was submitted for: with another database named in the
    sidebar, the caption's second way out, the entry comes back and the form is empty, and
    the draft returns when the first file is named again. A start-from-scratch tick set
    aside at a stop does the same: it comes back to its own file's box, also after a visit
    to another file, where without a stop such a visit asks again (Streamlit forgets a box
    it no longer draws); it never reaches another file's box. After a run that stopped
    before it drew them, the queue's filters (Severity, Test, Tier agreement, Minimum
    amount, and Materiality band where a materiality is given) and the fiscal year start
    over. Under Streamlit before 1.64, a decision still in the form (a write the store
    refused, or a draft given back) is not set aside when the run that stops is the rerun
    straight after "Write narrative" saved a note: those releases count a run that ends at
    `st.rerun()` as finished and drop the form it had not drawn, where 1.64 and 1.67 keep
    it (run in AppTest under 1.50, 1.64 and 1.67; the tagged source has the exception for a
    rerun from 1.64.0, and not at 1.49.0, 1.50.0, 1.62.0 or 1.63.0; the same on the third
    commit, and `main` sets nothing aside). The page's three earlier stops (no ledger at
    the path, a ledger that cannot be loaded, a controls file that cannot be used) come
    before the store is opened and are as they are on `main`: nothing is set aside or
    forgotten there. A database path that holds a NUL is still a `ValueError` (a shell
    cannot pass one). The command line's own reads after its open (`narrate`'s ledgers and
    narrative ids, `report --db` and `summary --db`) are still a traceback if the file
    fails in between. `adopt-legacy`'s other lines print a database path as typed, so one
    that holds a line break is printed on two lines: only the refusal line added here puts
    it through `one_line`.
  Version unchanged (1.2.1).
- **Next** — in the order the maintainer set on 2026-11-16: a v1.2.2 release; week 8's
  hardening (a changelog, mypy in CI, `mcp_server.py`'s coverage, a performance pass, and
  the whole suite in CI with the `app` extra installed); week 7 as a separate ledger
  (`generate --realistic`, from its own streams as the held-out ledger is, so the default
  ledger, the eval cases and the held-out measurement do not move); and publishing last,
  under another distribution name (on PyPI, `ledgerlens` has been another project's package
  since 2026-11-10). Open beyond that: re-recording `docs/demo.gif` (its model scores
  predate v1.0.0's tie fix, and since the dashboard-reads change its last frame shows what
  that change fixed: the decision just recorded still in the form, which now clears to no
  choice); a Streamlit of 1.66 or later in the 3.12 venv, the maintainer's call (until then
  only CI's 3.12 dashboard leg reads a stopped run as a session has it); and, unchanged, week 5's model train/test split (a re-measurement under rule
  10); a Benford baseline for a ledger's own size (the bands still depend on n above 300);
  a note's model and time in the workpaper and the MCP entry rows (the explain tool returns
  both); the prompt a note answered (a review-schema change); and the reported model on new
  eval rows (a row-shape change).
- 1475 tests on 3.9 (1 skipped) / 1490 on 3.12 (the fifteen MCP tests need 3.10+), ruff clean.

**Verified on the real API (2026-09-23; the injection case on 2026-09-26):** 25 narratives
cached (89% of input tokens read from cache), eval 94% pass-all over 17 cases (the seventeenth
took one request). The grader has been corrected four times since the first run, each
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
| `schema.py` | column contract, 11 anomaly archetypes, the 5 held-out ones (`HeldOutType`), US holiday calculator |
| `coa.py` | chart of accounts for the fictional test company |
| `generate.py` | labelled synthetic GL generator; `held_out=True` adds the held-out archetypes from their own streams (`HELD_OUT_SEED` used once, after the freeze) |
| `ingest.py` | validation + derived columns; `entry_level()` collapses lines to entries; `ledger_digest()` / `ledger_identity()`, the review-store key (`csv:<sha256>`, or `qbo:<realm>` from a `.identity.json` sidecar) |
| `controls.py` | a company's approver list (JET-12) and approval limit (JET-06) from `<stem>.controls.json` beside the ledger; `DEFAULT_CONTROLS` (the synthetic company's) when there is none; a file that cannot be used is refused, never replaced |
| `jets.py` | 12 deterministic tests + severity-weighted risk score; JET-06 and JET-12 read a `Controls` |
| `materiality.py` | `Materiality` (the overall figure given per run and never defaulted; the performance and clearly trivial shares default to 75% and 5%), the fiscal year the figures are set for (`fiscal_year_for`), whole cents (`to_cents`), and each entry's band against the figures (`bands`, `count_by_band`): beside the risk score, never in it |
| `sampling.py` | the population (one fiscal year's entries with a positive amount) and two seeded selections against a `Materiality`: monetary unit sampling (no expected misstatement) and stratified random sampling; the selection memo (`collect_memo` / `render_memo`, text / markdown / JSON) |
| `benford.py` | first-digit test, MAD (Nigrini bands) + chi-square; no band under 300 amounts (`SMALL_SAMPLE_NOTE`) |
| `features.py` | 16 engineered per-entry features for the model tier |
| `model.py` | Isolation Forest, rank-based flagging, tier comparison |
| `evaluate.py` | precision/recall, by archetype, by test, tier comparison, model lift; `attribution` (which test caught each archetype), `budget_recall_by_archetype` / `held_out_budget` (the model's budget against chance), `sweep`; `check_labels` (labels must cover exactly the ledger's entries) and the two caveat constants |
| `report.py` | 5-tab Excel workpaper; all the text it writes but its own fixed headings goes through `_CellText` (what a cell cannot hold shown as its code point, a text longer than a cell holds cut with its length, both counted on the Summary sheet) |
| `review.py` | append-only SQLite store keyed by ledger (`ledger_id`; schema version 4 via `PRAGMA user_version`, numbered migrations from frozen DDL): decisions and versioned narratives, enforced by triggers; `read_only()` opener; `ledgers()` / `adopt_legacy()`; `CALLER_FAULTS`, the driver's errors that are a caller's fault rather than a refused write |
| `narrate.py` | Claude narratives: structured-output JSON contract (checked again by `validate`: a field that is not text, or text the store cannot encode, refused; an evidence item's lines joined into one), cacheable system prompt, usage accounting, the model each response names (`response_model`), each note of a batch handed to the caller's `keep` as it comes back (`NoteNotKept` stops the batch) |
| `narrative_eval.py` | case selection (the one label reader), rubric grader (the citation stripped before numbers are counted), `case_prompt` with per-case `overrides`, runner with case-file and ledger provenance, report with grader notes, case-set notes and baseline |
| `ledger_context.py` | read-only query layer (summary, top exceptions, explain, search, Benford, review status); opens the review DB `mode=ro`, bound to the ledger's identity; 3.9-safe |
| `mcp_server.py` | MCP registration over `ledger_context` (v2 SDK, stdio); needs 3.10+ |
| `summary.py` | one page on a scored ledger (`collect`, `render` as text / markdown / JSON); scores nothing itself; `md_code` renders ledger text as inert markdown |
| `env.py` | dependency-free `.env` loader |
| `connectors/qbo.py` | QuickBooks Online: `QboConfig` (`QBO_*`), transports (urllib, `RecordedTransport` replay, sanitizing `Recorder`), OAuth (`QboAuth`, `CallbackServer`), `QboClient` (refresh, throttling, paging), and the mapping into the ledger contract (`pull`, `PullStats`, `write_identity`); stdlib |
| `connectors/tokens.py` | QuickBooks token store: `~/.config/ledgerlens/qbo-<environment>-<realm>.json` (`$XDG_CONFIG_HOME/ledgerlens/` when set; `$LEDGERLENS_TOKEN_DIR` overrides both), mode 600, a record only for the realm its file names, refuses any path inside a git checkout; stdlib |
| `cli.py` | `generate` / `test` / `score` / `benford` / `report` / `summary` / `sample` / `narrate` / `adopt-legacy` / `eval-narratives` / `qbo-auth` / `pull-qbo` |
| `app.py` | Streamlit dashboard: queue, note, decision form, history |

## Rules that must not be broken

1. **Never commit real data.** Synthetic + QuickBooks sandbox only. Before any push touching
   data handling:
   ```bash
   git ls-files -ci --exclude-standard                                    # must be empty (force-added ignored files)
   git ls-files -z | LC_ALL=C grep -zaiE '\.csv$|\.xlsx$|\.parquet$|\.sqlite$|\.sqlite3$|\.db$|^data/|(^|/)\.env(\..*)?$|(^|/)secrets/|qbo-(sandbox|production)-[^/]*\.json$|\.controls\.json$' | tr '\0' '\n' | grep -vx '.env.example'   # must be empty
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
7. **A threshold is chosen against labelled data, never for roundness, and every sweep is
   recorded in `docs/tuning.md`.** Only JET-11's was chosen on the default ledger; the other
   settings are swept there and left, because that ledger cannot choose them. The approver list and approval limit (`controls.py`) are not thresholds:
   they are company facts, stated in a controls file and never tuned. Materiality
   (`sampling.py`) is neither: it is the auditor's judgement, given for each run; the overall
   figure is never defaulted (the two shares default to 75% and 5%), and none of it is tuned.
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
10. **The held-out figures are the ones measured at the freeze.** The held-out ledger is
    generated once, from the commit tagged `held-out-freeze`, with `HELD_OUT_SEED`, and nothing
    is tuned on it. Before the measurement no test generated with that seed (a conftest guard,
    removed with it). After the tag no commit touches `src/` or `app.py` until the measurement
    is merged;
    a defect found then is disclosed in `docs/limitations.md`, not fixed. A later change that
    moves the held-out figures labels the new ones "re-measured, no longer held out".

## The honest framing of the results

Headline: precision 0.843, recall 0.974 over 5,085 entries. **These are partly circular** — for
eight of eleven archetypes the generator injects the anomaly using the same definition the test
looks for, so recall on those is near-tautological. The other three are not measurements either:
`benford_drift` (0.75) is all JET-12's (the injector picks the keyer at random), JET-09 catches
none of `dormant_account` (1.00 by JET-08 and JET-12), and JET-08 three of seven
`rare_account_pair` (0.86). `evaluate.attribution` counts which test caught what. (Until week 5
the caveat said "nine of eleven" and called benford_drift and rare_account_pair the honest
figures.)

Same pattern in the model tier: it is a strong re-ranker (top-25 precision 0.60, 40x lift over
random) but a weak independent detector — the "model only" segment sits at the base rate, because
these anomalies were *defined* as rule violations.

**Breaking that circularity was the single most valuable remaining task.** Week 5 measured a
held-out ledger of archetypes no rule describes, once, at the freeze, against chance, with what
is reported registered first in `docs/limitations.md` (rule 10): the model's budget held 6 of
the 43 held-out entries against 0.86 by chance, all of them token amounts, and none of the four
other archetypes (three of which exist only between entries). Quote that beside the default
figures, and do not quietly report the flattering number.

The narrative eval has the same shape of caveat: it grades properties (grounded, specific,
non-assertive, confidence in band), not insight. A note can pass every check and still be
unhelpful. Say so wherever the number is quoted.

## Conventions

- Feature branch → PR → squash merge → tag. No direct commits to `main`.
- Tests live beside the module they cover; the session-scoped `ledger` fixture is in
  `tests/conftest.py`.
- Comments explain *why*, not *what*. Existing code sets the density — match it.
- A sort that picks one row, cuts a top-N or prints rows says how a tie breaks (a second
  key, or an index sort and then a stable `kind`). A one-key pandas sort is not stable: on any
  key but pandas 3's `str` it keeps tied rows in input order up to 16 rows and scrambles them
  from 17 (pandas 2.3 and 3.0 alike), and the order it gave tied lines differed between a Mac
  and the x86-64 Linux runners. A table that cannot reach 17 rows (the dashboard's four
  agreement counts) may rest on that.
- Run `ruff check src tests app.py` and both venvs' test suites before every commit.
- The Claude API is never called from a test: `tests/conftest.py` has the fake client
  (`llm` fixture) shaped like the real responses, thinking block included.
- The held-out recipes are tested on `DEV_SEEDS`. The measured ledger (`HELD_OUT_SEED`) is the
  session fixture `held_out_raw`, pinned by `HELD_OUT_LEDGER_SHA256`; `tests/test_limitations.py`
  reproduces its measurement. A test that moves those figures is a re-measurement (rule 10).
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
- Text that came from a ledger, or from a ledger's controls file, is data. Where it leaves the
  machine it is made inert: the summary's markdown through `summary.md_code`, a prompt
  through the data-not-instructions rule, a refusal through `ascii` of the id it quotes, the workpaper's string cells written
  as text (openpyxl would store one beginning with `=` as a formula), and all but the workpaper's
  own fixed headings through `report._CellText` (a character a cell cannot hold shown as its code
  point in brackets, a text longer than a cell holds cut with its length, each counted on the
  Summary sheet), the CLI's own lines
  through `summary.one_line`, and in the dashboard a flag's reason and an approver's name as
  code spans (`summary.code_span`) and the note's prose with its markdown escaped (`app.md`: no construct
  forms, though Streamlit still links a bare URL and redraws `->`); its tables are drawn as
  text. `summary.py` and its tests stay ASCII
  (pinned by a test), because their patterns name characters that could not be reviewed if
  written raw.
- A caveat has one wording (`evaluate.DETECTION_CAVEAT`, `evaluate.MODEL_TIER_CAVEAT`) and is
  printed by everything that prints the number it qualifies: `test`, `score`, `summary`,
  the dashboard, the workpaper and CI's detection gate.
- `.env` holds configuration only (the API key, `QBO_*` client settings). QuickBooks tokens go
  through `connectors/tokens.py` to `~/.config/ledgerlens/` (`$XDG_CONFIG_HOME/ledgerlens/` when that is set; `$LEDGERLENS_TOKEN_DIR` overrides both), which refuses any path inside a
  git checkout.

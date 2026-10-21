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
| Python here | system 3.9.6 plus python.org **3.12.0** at `/usr/local/bin/python3.12`. Code must stay 3.9-compatible; only `mcp_server.py` needs 3.10+. pandas 2.2 or later (`ingest` imports `pandas.tseries.api.guess_datetime_format`), Streamlit 1.49 or later (`st.dataframe(width="stretch")`); CI and the venvs install the latest (pandas 2.3 and 3.0), so the floors themselves are not run |
| CI | GitHub Actions: pytest on 3.9 / 3.11 / 3.12 + a detection-quality gate + a `rule-1` job (no data or secret file tracked); `claude.yml` answers `@claude`; `weekly.yml` regenerates the default ledger on Mondays and opens an Issue with `ledgerlens summary` |

```bash
source ~/.venvs/ledgerlens/bin/activate && pytest -q && ruff check src tests app.py
source ~/.venvs/ledgerlens312/bin/activate && pytest -q      # also runs the MCP tests
```

Run both before every commit. The 3.9 venv has anthropic 0.x; the 3.12 venv has anthropic 1.x
and the MCP SDK, so the two together cover every code path CI will see.

## Current state

- **v1.1.0** is #12 to #15 (#15, `week5/held-out`, squash-merged 2026-10-21 as `e0a2b02`) and
  the follow-up `fix/pandas-floor-and-tie-order`, to be tagged on the follow-up's merge
  commit with release notes reviewed against the code.
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
- **Version floors and stable tie order (`fix/pandas-floor-and-tie-order`, before the v1.1.0
  tag)** — found after the merge.
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
    (Benford by MAD, review counts, mean model score by archetype) still leave a tie to a
    one-key sort.
- **Next** — tag v1.1.0 on the follow-up's merge commit, with the claims-reviewed notes. Then the
  maintainer's call. Open, unchanged: re-recording `docs/demo.gif` (its model scores predate
  v1.0.0's tie fix) and the display-only sorts above.
- 1028 tests on 3.9 (1 skipped) / 1042 on 3.12 (the fourteen MCP tests need 3.10+), ruff clean.

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
| `schema.py` | column contract, 11 anomaly archetypes, the 5 held-out ones (`HeldOutType`), US holiday calculator |
| `coa.py` | chart of accounts for the fictional test company |
| `generate.py` | labelled synthetic GL generator; `held_out=True` adds the held-out archetypes from their own streams (`HELD_OUT_SEED` used once, after the freeze) |
| `ingest.py` | validation + derived columns; `entry_level()` collapses lines to entries; `ledger_digest()` / `ledger_identity()`, the review-store key (`csv:<sha256>`, or `qbo:<realm>` from a `.identity.json` sidecar) |
| `controls.py` | a company's approver list (JET-12) and approval limit (JET-06) from `<stem>.controls.json` beside the ledger; `DEFAULT_CONTROLS` (the synthetic company's) when there is none; a file that cannot be used is refused, never replaced |
| `jets.py` | 12 deterministic tests + severity-weighted risk score; JET-06 and JET-12 read a `Controls` |
| `benford.py` | first-digit test, MAD (Nigrini bands) + chi-square |
| `features.py` | 16 engineered per-entry features for the model tier |
| `model.py` | Isolation Forest, rank-based flagging, tier comparison |
| `evaluate.py` | precision/recall, by archetype, by test, tier comparison, model lift; `attribution` (which test caught each archetype), `budget_recall_by_archetype` / `held_out_budget` (the model's budget against chance), `sweep`; `check_labels` (labels must cover exactly the ledger's entries) and the two caveat constants |
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
   they are company facts, stated in a controls file and never tuned.
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
- A sort that picks one row or cuts a top-N says how a tie breaks (a second key, or an
  index sort and then a stable `kind`). A one-key pandas sort is not stable, and the order
  it gave tied lines differed between a Mac and the x86-64 Linux runners.
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
  as text (openpyxl would store one beginning with `=` as a formula), the CLI's own lines
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

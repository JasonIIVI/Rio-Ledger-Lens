# Changelog

Every release of LedgerLens, newest first. An entry here is a summary. The fuller record
is each release's notes on GitHub, linked from its version. From v1.0.0 on, those notes say
what behaves differently on upgrade, what was reviewed, the numbers and the known limits.

The layout follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Each date is
the day the release was published, in UTC. A number such as #37 is the pull request that
made the change; a line under Unreleased gets its number when the section becomes a
version.

No entry quotes a detection figure. Those figures are partly circular, and the README gives
them together with the caveat they need.

## [Unreleased]

### Added
- This changelog, and issue templates for a bug report and a feature request.

### Fixed
- `benford --by` with a column the ledger does not have prints one `error:` line that
  names the columns there are, where it ended in a traceback.

## [1.2.2] - 2026-11-24

### Changed
- The dashboard's decision form opens with no decision chosen, and "Record decision" with
  none chosen records nothing and says so (#37).
- `narrate` stores each note as its response comes back, and stops at the first save the
  review database refuses instead of requesting more notes (#33).
- The narrative eval's grader hash moved, because `narrate.validate` is the schema check
  the grader calls. The committed run was re-graded offline and no check moved on any row
  (#33).
- CI runs the dashboard's tests and the workpaper's with the `app` extra installed, in a job
  of their own on Python 3.9 and 3.12 (#35).

### Fixed
- A review database that fails while the dashboard is open is a message on every read,
  where it was a traceback, and a file that cannot be opened as a review database says why
  in more cases (#37).
- The decision draft is cleared in the browser once its decision is recorded, and a
  decision being recorded when the page stops comes back into the form with its entry (#37).
- `adopt-legacy` prints one `error:` line when a file that opened then refuses the copy,
  where it was a traceback (#37).
- A reply whose `summary`, `why_flagged`, `suggested_control` or evidence item is not text,
  or is text the review database cannot encode, is refused. Before, an object or an array
  in one of the three text fields was a traceback at the database insert, and an evidence
  item that was not text was stored under its Python spelling (#33).
- The workpaper shows a character a cell cannot hold as its code point, and cuts a text
  longer than a cell holds with its length. Before, `report` stopped with a traceback, wrote
  a file that would not open, or cut the text with nothing in the workpaper to say so (#34).

## [1.2.1] - 2026-11-11

### Changed
- A first-digit test of fewer than 300 amounts gets no conformity band. It reads
  "insufficient data", with one note ahead of the figures on the command line, in the
  dashboard and over MCP, and stated on the workpaper's Methodology sheet (#24).
- A stored note records the model the API response names, not the one the request named
  (#25).
- `load_labels` reads `anomaly_type` as text, as it reads `entry_id` (#21).
- A new deprecation warning laid at a `ledgerlens` line while a ledger or its labels are
  loaded, or the model's features are built, fails a test, so a pandas or numpy release can
  fail CI over a warning (#21).

### Fixed
- The dashboard shows a failed request to the API from its write button as a message (#27),
  and a refused write to the review database as a message too; a refused decision keeps what
  was typed (#30). Both were tracebacks that stopped the page.
- Three tables that only display rows break a tie on a second key, where the order of tied
  rows was left to the platform's sort, and two top-N cuts name `entry_id` as their last key
  (#22).
- The three warnings pandas 3.1 gave of changes that pandas 4 turns into errors are gone
  (#21).
- CI's Python 3.9 job runs on a runner image that still has Python 3.9 (#29).

## [1.2.0] - 2026-10-24

### Added
- `ledgerlens sample`: entries to examine, selected against a materiality you give, by
  monetary unit sampling or stratified random sampling. A seed gives one selection on every
  Python, and a memo states every figure the selection came from (#19).
- `--materiality`, with `--performance`, `--trivial` and `--fiscal-year`, on `test`,
  `summary` and `report`, and the overall figure (with a choice of fiscal year) in the
  dashboard's sidebar: each entry's materiality band beside its risk score, never in it
  (#20).

### Fixed
- Under pandas 3.1 the workpaper, the summary and the MCP server's entry rows raised a
  `TypeError` on a review database with decisions and no note (#18).

## [1.1.0] - 2026-10-22

### Added
- Ledger validation: a cell no test can read is refused when the ledger is loaded, with
  every such column named in one message, and each date column is read in one format (#12).
- A company's own approver list and approval limit for JET-12 and JET-06, read from a
  `<stem>.controls.json` beside the ledger (#14).
- `generate --held-out`: a ledger with five archetypes no test describes. It was measured
  once, from the code frozen at the `held-out-freeze` tag, and `docs/limitations.md`
  recorded what would be reported before the ledger existed (#15).
- `test --labels` prints which test caught each archetype, `score --labels` prints the
  model's budget against chance, and `docs/tuning.md` records a sweep of every test that has
  a setting (#15).

### Changed
- pandas 2.2 or later is needed, and Streamlit 1.49 or later for the dashboard (#17).
- Only an empty field is missing. A text cell that reads `NA` or `N/A` is kept as text, so
  a ledger that holds one has another `csv:` identity than under v1.0.0, and its earlier
  review rows show as another ledger's (#12).
- The detection caveat counts eight of the eleven archetypes as definitional, where v1.0.0
  counted nine, and no longer calls any of the other three a measurement (#15).
- The summary's JSON and markdown gain a `controls` block (#14) and a `held_out` field on
  each archetype row (#15).
- In the Python API, `evaluate.NON_CIRCULAR_ARCHETYPES` is replaced by `NOT_DEFINITIONAL`
  (#15), and the `flags` column of `precision_by_test` is now `entries_flagged`, because it
  counts entries (#12).

### Fixed
- `generate` over a short date range leaves out an archetype the range has no day for, with
  a warning, and refuses a range with no business day. It raised an `IndexError` (#13).
- `test --only` refuses an id it does not know in one line, and runs a repeated id once
  (#12).
- Tied rows in the recall and precision tables print in one order on every platform (#17).
- A leading `~` in a ledger path is expanded for the identity and controls files beside the
  ledger too, as it already was for the ledger itself (#14).

## [1.0.0] - 2026-10-08

### Added
- A QuickBooks Online connector that adds no dependency (its HTTP, OAuth and callback
  server use the standard library): `ledgerlens qbo-auth` signs in to a sandbox company and
  `ledgerlens pull-qbo` maps a period into the ledger every other command reads. Its tests
  replay a sanitized recording of a sandbox company (#8).
- `ledgerlens summary`: one page on a ledger as text, markdown or JSON, with each caveat
  ahead of the figures it qualifies (#9).
- A weekly scheduled run that regenerates the default ledger and opens an Issue with the
  summary (#9).
- The README's diagram, demo and "how to read it" columns, and tests that recompute the
  README's figures (#9).

### Changed
- A label file is checked against the ledger: one that does not list each of the ledger's
  entry ids exactly once is refused, and `is_anomaly` is read strictly (#9).
- The model tier's scores moved once. The account an entry's amount is measured against no
  longer depends on the platform's sort (#9).
- A ratio over nothing reads "undefined", where it was written as 0.0 (#9).

### Fixed
- A ledger file that is missing or is not a ledger, or a population the model tier cannot
  be fitted on, is one `error:` line, where it was a traceback (#9).
- Ledger text stays inert. The workpaper writes text cells as text, where a cell beginning
  with `=` was stored as a live formula. `test`, `score` and `benford` print ledger text as
  one line, without the characters a reader cannot see, and the dashboard draws a flag's
  reason as a code span (#9).
- JET-07 and JET-09 break a same-day tie by entry time, then entry id (#9).

## [0.4.0] - 2026-09-30

### Added
- A review database keyed by ledger (schema version 4), and `ledgerlens adopt-legacy` to
  file the rows written before the key under a ledger (#7).
- A hand-written prompt-injection case in the narrative eval (#7).
- A QuickBooks token store that keeps tokens outside any checkout, with mode 600 (#7).
- A schema version on the review database, with numbered migration steps, and a CI job that
  fails when a data or secret file is tracked (#6).

### Changed
- The schema-4 upgrade of a review database is one-way: back the file up before opening it
  (#7).
- Until `adopt-legacy` has been run on an upgraded review database, `narrate` refuses
  unless given `--ignore-legacy`, and the dashboard holds back its write buttons unless the
  reviewer chooses to start from scratch (#7).
- New eval rows record the sha256 of the ledger they were run on (#6).

## [0.3.1] - 2026-09-25

### Changed
- Narratives are versioned and never replaced, and each decision records the note it was
  made against (#4).
- Append-only is enforced by SQLite triggers (#4), and `REPLACE INTO` cannot get around
  them (#5).
- The MCP server opens the review database on a read-only connection (#4).
- Every eval row carries the sha256 of the case file (#4) and of the grader (#5). A
  re-grade never calls the API and never deletes rows (#5).
- A review database from v0.3.0 is rebuilt in place the first time it is opened read-write
  (#4, #5).
- The dashboard records the note the reviewer read, and refuses a decision when the note
  changed while it was being read (#5).
- `eval-narratives --no-resume` no longer deletes the rows in a run directory: it refuses,
  and asks for a fresh `--runs-dir` (#5).
- The review workflow gets more turns and a full-history checkout (#3, #4).

## [0.3.0] - 2026-09-23

### Added
- Reviewer notes written by Claude for flagged entries, as a JSON contract checked again
  before storage. The model explains a flag and suggests evidence. It decides nothing and
  never sees the labels (#2).
- The human loop: a named reviewer records accept, dismiss or escalate in an append-only
  SQLite store, from the dashboard (#2).
- An MCP server, `ledgerlens-mcp`, with six read-only tools (#2).
- The narrative eval: hand-reviewed cases and a rubric grader that grades properties of a
  note, not insight (#2).
- `ledgerlens narrate` and `ledgerlens eval-narratives`, `.env` loading, and a workflow
  that answers a mention in an Issue or a pull request (#2).

## [0.2.0] - 2026-09-22

The first release. It carries week 1's work too: the labelled synthetic generator, the
twelve journal-entry tests, Benford analysis and the evaluation harness.

### Added
- A second tier: an Isolation Forest over 16 engineered per-entry features, scored
  separately from the journal-entry tests and never blended with them (#1).
- A Streamlit review dashboard (#1).
- An Excel workpaper (#1).

[Unreleased]: https://github.com/JasonIIVI/Rio-Ledger-Lens/compare/v1.2.2...HEAD
[1.2.2]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v1.2.2
[1.2.1]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v1.2.1
[1.2.0]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v1.2.0
[1.1.0]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v1.1.0
[1.0.0]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v1.0.0
[0.4.0]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v0.4.0
[0.3.1]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v0.3.1
[0.3.0]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v0.3.0
[0.2.0]: https://github.com/JasonIIVI/Rio-Ledger-Lens/releases/tag/v0.2.0

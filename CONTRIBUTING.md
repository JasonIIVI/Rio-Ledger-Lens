# Contributing

This is a personal portfolio project, but the workflow is deliberately real.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,llm,mcp,app]"
pytest -q
```

The MCP extra needs Python 3.10+; on 3.9 it is skipped and so are its tests. No test calls the
Claude API: the `llm` fixture in `tests/conftest.py` stands in for it.

## Before opening a pull request

```bash
ruff check src tests app.py
pytest -q --cov=ledgerlens
```

CI runs both on Python 3.9, 3.11 and 3.12, plus a detection-quality job that
regenerates the ledger and fails if precision drops below 0.80 or recall below 0.90.
Those three test jobs also fail under a coverage floor: 96% on 3.9, where the MCP SDK
cannot be installed and `mcp_server.py` counts as untested, and 97% on 3.11 and 3.12. Each
is the whole percent under that job's own total when the floor was set (96.71% and
97.94%), so a new module left untested fails the job. Raise a floor once the total has
moved well clear of it; never lower one to get a change through.
The dashboard's tests (`tests/test_app.py`) need the `app` extra, which those jobs do not
install, and one workpaper test (in `tests/test_report.py`) needs the pyarrow that comes
with it. CI runs both files in its `dashboard` job, on Python 3.9 and 3.12.

A change that someone using the tool would notice gets a line under `Unreleased` in
`CHANGELOG.md`. A version bump renames that section to the version and the day its release
is published (UTC), gives each of its lines the number of the pull request that made the
change, opens a new empty `Unreleased` section above it, adds the version's link at the
foot of the file and points the `Unreleased` link at the new tag;
`tests/test_changelog.py` fails a bump that adds no entry. The changelog quotes no
detection figure: those are given in the README, with their caveat.

To report a problem or ask for a feature, the issue templates under
`.github/ISSUE_TEMPLATE/` ask for what is needed. The repository is public, so nothing real
goes into an Issue: no ledger, labels file, workpaper, review database, controls file,
token or key.

## Adding a journal-entry test

1. Write the function in `src/ledgerlens/jets.py`. It takes the prepared DataFrame and
   returns a frame with exactly `FLAG_COLUMNS`.
2. Register it in `REGISTRY` with the next free `JET-nn` id.
3. Add a matching archetype to the generator if the test needs one to find.
4. Add a test in `tests/test_jets.py` asserting it catches the injected entries.
5. If the test has a threshold, tune it against the labelled data and record the sweep
   in `docs/tuning.md`. Do not pick a round number and move on.

## House rules

- Every flag carries a written `reason`. A score with no explanation just moves the
  work to the reviewer.
- A flag is a question, not a finding. Word reasons accordingly.
- Never let detection code see the label file. The one label reader outside `evaluate` is
  `narrative_eval.select_cases`, and it only chooses which entries to test.
- The model never decides. Tools and CLI verbs that touch the review store record decisions
  only under a reviewer's name; nothing the model calls can write one.
- Eval expectations are written by hand from the entry's data, before a run, and are never
  tuned to a model's output.
- Decisions and narratives are append-only, and the database enforces it. Rewriting a note adds
  a version; the decision keeps pointing at the version the reviewer read.
- Eval run rows under `evals/narratives/runs/` are committed and never hand-edited (a run that
  starts over goes to a fresh directory; nothing deletes rows). They hold rows for the default
  synthetic ledger only: the eval refuses to write rows, cases or the report for any other
  ledger anywhere under evals/ or docs/, however the path is spelled; rows written since PR #6
  record the ledger's sha256 (older rows gain it at their next re-grade, once their prompt has
  been rebuilt from the ledger); and a test rebuilds every stored prompt in every file under
  the runs root. The rows record the grader's
  hash and keep the grades a re-grade replaces; a grader change is also recorded in
  `GRADER_NOTES` with what it did to the published score, and the report prints both. A test
  checks that the committed rows carry this checkout's grader hash, so a grader edit only lands
  together with its re-grade (`ledgerlens eval-narratives data/ledger.csv --regrade`).
- A change to what is judged is disclosed the same way: a case added or rewritten goes in
  `CASE_SET_NOTES` with the commit that introduced it and its measured effect, the re-grade
  across the case-file change ships in that commit, and a case written by hand is narrated
  only after that commit is pushed. An override is part of the prompt a note answered, so a
  narrated case's override is never rewritten in place: new text goes to a fresh runs
  directory or another entry's case (the eval refuses to re-grade across it).
- The review database's schema is versioned (`PRAGMA user_version`). A schema change is a new
  numbered migration step in `review.py` built from frozen DDL literals, never from the live
  constants, so the step keeps doing what it did when it shipped; old-schema fixtures in the
  tests are DDL in code, never binary files. Review rows are keyed by ledger identity: nothing
  writes across that key except `adopt_legacy`, `ledgers()` / `other_ledgers()` only count
  across it, and `narrative_by_id` looks a note up by id across it and says which ledger it
  belongs to; every other reader is filtered by the bound ledger.
- Nothing under the repository ever holds a credential. `.env` (ignored) carries the API key
  and `QBO_*` settings; QuickBooks tokens go through `connectors/tokens.py` to
  `~/.config/ledgerlens/` (`$XDG_CONFIG_HOME/ledgerlens/` when that is set; `$LEDGERLENS_TOKEN_DIR` overrides both), which refuses any path inside a git checkout.
- No test touches the network. QuickBooks responses are replayed from `tests/fixtures/qbo/`
  by `RecordedTransport`; new fixtures come from `ledgerlens pull-qbo --record DIR` against a
  sandbox company only (it refuses production), which replaces the realm id, tokens, user
  names and e-mail addresses before anything reaches disk. Read a recording before committing
  it.

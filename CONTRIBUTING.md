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

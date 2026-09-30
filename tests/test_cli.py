import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledgerlens.cli import build_parser, main


def test_generate_writes_both_files(tmp_path):
    code = main(["generate", "--start", "2024-01-01", "--end", "2024-02-29",
                 "--out-dir", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "ledger.csv").exists()
    assert (tmp_path / "labels.csv").exists()


def test_test_command_runs_against_generated_data(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    code = main(["test", str(tmp_path / "ledger.csv"),
                 "--labels", str(tmp_path / "labels.csv")])
    assert code == 0
    out = capsys.readouterr().out
    assert "Precision" in out
    assert "Recall by archetype" in out


def test_test_command_can_run_a_single_test(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-03-31",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    main(["test", str(tmp_path / "ledger.csv"), "--only", "JET-02"])
    out = capsys.readouterr().out
    assert "Ran 1 test(s)" in out


def test_benford_command_reports_conformity(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2025-12-31",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    main(["benford", str(tmp_path / "ledger.csv")])
    out = capsys.readouterr().out
    assert "MAD" in out
    assert "conformity" in out


def test_bad_date_is_rejected():
    parser = build_parser()
    try:
        parser.parse_args(["generate", "--start", "01/01/2024"])
    except SystemExit as exc:
        assert exc.code != 0
    else:  # pragma: no cover
        raise AssertionError("expected SystemExit")


def test_score_command_reports_tier_agreement(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-12-31",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    code = main(["score", str(tmp_path / "ledger.csv"),
                 "--labels", str(tmp_path / "labels.csv")])
    assert code == 0
    out = capsys.readouterr().out
    assert "Tier agreement" in out
    assert "Isolation Forest" in out
    assert "lift_vs_random" in out


def test_score_command_writes_csv(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    out_csv = tmp_path / "combined.csv"
    main(["score", str(tmp_path / "ledger.csv"), "--out", str(out_csv)])
    assert out_csv.exists()


def test_report_command_writes_workpaper(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-12-31",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    xlsx = tmp_path / "wp.xlsx"
    code = main(["report", str(tmp_path / "ledger.csv"),
                 "--labels", str(tmp_path / "labels.csv"), "--out", str(xlsx)])
    assert code == 0
    assert xlsx.exists()
    assert "Workpaper written" in capsys.readouterr().out


def test_report_command_can_skip_the_model(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    xlsx = tmp_path / "rules_only.xlsx"
    main(["report", str(tmp_path / "ledger.csv"), "--no-model", "--out", str(xlsx)])
    assert xlsx.exists()


def test_narrate_without_a_key_explains_and_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # a developer's real .env must not leak into the test
    main(["generate", "--start", "2024-01-01", "--end", "2024-03-31",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    code = main(["narrate", str(tmp_path / "ledger.csv"), "--db", str(tmp_path / "r.sqlite"),
                 "--no-model"])
    assert code == 1
    assert ".env" in capsys.readouterr().out


def test_narrate_caches_narratives_and_moves_on_next_time(tmp_path, capsys, monkeypatch, llm):
    from ledgerlens import cli
    from ledgerlens.ingest import ledger_identity, load_csv
    from ledgerlens.narrate import Narrator
    from ledgerlens.review import ReviewStore

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    db = tmp_path / "review.sqlite"
    ledger = str(tmp_path / "ledger.csv")
    ident = ledger_identity(load_csv(ledger), ledger)

    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "Narrated 4/4" in out
    assert "read from cache" in out
    assert len(ReviewStore(db, ident).narrative_ids()) == 4
    assert client.calls[0]["output_config"]["effort"] == "medium"

    # Cached entries are skipped, so the next run narrates the next four down.
    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model"]) == 0
    assert "Skipped 4" in capsys.readouterr().out
    assert len(ReviewStore(db, ident).narrative_ids()) == 8
    assert len(client.calls) == 8

    # --force redoes the top four instead of moving on.
    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model", "--force"]) == 0
    assert len(ReviewStore(db, ident).narrative_ids()) == 8
    assert len(client.calls) == 12


def test_report_attaches_an_existing_review_db_only(tmp_path, capsys):
    from ledgerlens.ingest import ledger_identity, load_csv
    from ledgerlens.review import Decision, ReviewStore

    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    missing = tmp_path / "absent.sqlite"
    main(["report", str(tmp_path / "ledger.csv"), "--no-model", "--db", str(missing),
          "--out", str(tmp_path / "a.xlsx")])
    assert "without review columns" in capsys.readouterr().out
    assert not missing.exists()  # a report must not create a review database

    db, csv = tmp_path / "review.sqlite", str(tmp_path / "ledger.csv")
    ident = ledger_identity(load_csv(csv), csv)
    ReviewStore(db, ident).record(Decision("JE-2024-000001", "dismiss", "ana"))
    other = ReviewStore(db, "csv:" + "b" * 64)  # another ledger's decisions, same file
    for entry in ("JE-2024-000001", "JE-2024-000002"):
        other.record(Decision(entry, "escalate", "bo"))
    code = main(["report", str(tmp_path / "ledger.csv"), "--no-model", "--db", str(db),
                 "--out", str(tmp_path / "b.xlsx")])
    assert code == 0
    # The workpaper's review section is this ledger's, and the other ledger is only counted.
    import openpyxl

    sheet = openpyxl.load_workbook(tmp_path / "b.xlsx")["Summary"]
    summary = {row[0].value: row[1].value for row in sheet.iter_rows() if row[0].value}
    assert summary["Review rows for ledger"] == ident
    assert summary["Decisions recorded"] == 1
    assert summary["  dismiss"] == 1 and "  escalate" not in summary
    assert summary["Other ledgers in this database (not shown)"] == (
        "1 ledger(s): 0 narrated, 2 decided entries")


def test_eval_narratives_select_writes_a_skeleton_and_will_not_clobber_it(tmp_path, capsys):
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    ledger, labels = str(tmp_path / "ledger.csv"), str(tmp_path / "labels.csv")
    cases = tmp_path / "cases.json"

    assert main(["eval-narratives", ledger, "--select", "--cases", str(cases)]) == 2  # no labels
    assert main(["eval-narratives", ledger, "--select", "--labels", labels,
                 "--cases", str(cases)]) == 0
    payload = json.loads(cases.read_text())
    assert payload["cases"] and all("must_mention" in c for c in payload["cases"])
    assert main(["eval-narratives", ledger, "--select", "--labels", labels,
                 "--cases", str(cases)]) == 2  # exists, no --overwrite


def test_eval_narratives_defaults_to_a_dated_runs_dir_and_regrades_the_newest(
        tmp_path, capsys, monkeypatch, llm):
    from ledgerlens import cli
    from ledgerlens.ingest import load_csv
    from ledgerlens.narrate import DEFAULT_MODEL, Narrator

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    ledger, labels = str(tmp_path / "ledger.csv"), str(tmp_path / "labels.csv")
    # The default runs directory is for the default ledger only. Declaring this
    # six-month ledger to be it keeps the test off the 10,000-line one (the pin
    # test covers the real constant, CSV round trip included).
    monkeypatch.setattr(cli.narrative_eval, "DEFAULT_LEDGER_SHA256",
                        cli.narrative_eval.ledger_digest(load_csv(ledger)))
    cases, report = tmp_path / "cases.json", tmp_path / "report.md"
    main(["eval-narratives", ledger, "--select", "--labels", labels, "--cases", str(cases)])
    capsys.readouterr()
    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    argv = ["eval-narratives", ledger, "--cases", str(cases), "--out", str(report), "--limit", "2"]

    # Nothing to re-grade yet is an error that names the flag, not a fresh paid run,
    # whether the directory is defaulted or mistyped; and a re-grade cannot start over.
    assert main(argv + ["--regrade"]) == 2
    assert "runs-dir" in capsys.readouterr().out
    assert main(argv + ["--regrade", "--runs-dir", str(tmp_path / "typo")]) == 2
    assert "nothing to re-grade" in capsys.readouterr().out
    with pytest.raises(SystemExit) as refused:
        main(argv + ["--regrade", "--no-resume"])
    assert refused.value.code == 2
    assert client.calls == []

    assert main(argv) == 0
    runs = sorted((tmp_path / "evals" / "narratives" / "runs").iterdir())
    assert len(runs) == 1 and runs[0].name.endswith(f"-{DEFAULT_MODEL}")
    assert len((runs[0] / "results.jsonl").read_text().splitlines()) == 2
    assert "evals/narratives/runs/" in report.read_text()

    # A re-grade finds that directory without being told, and calls nothing.
    assert main(argv + ["--regrade"]) == 0
    assert len(client.calls) == 2
    assert "re-graded" in report.read_text()

    # Only the expected refusals become exit 2; anything else is a bug and surfaces.
    def boom(*args, **kwargs):
        raise ValueError("boom")
    monkeypatch.setattr(cli.narrative_eval, "run_eval", boom)
    with pytest.raises(ValueError, match="boom"):
        main(argv + ["--regrade"])


def test_eval_narratives_keeps_other_ledgers_out_of_the_committed_paths(tmp_path, capsys,
                                                                       monkeypatch, llm):
    """Rule 1 at the point of writing: another ledger needs its own --runs-dir and --cases."""
    from ledgerlens import cli
    from ledgerlens.ingest import load_csv
    from ledgerlens.narrate import Narrator

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    ledger, labels = str(tmp_path / "ledger.csv"), str(tmp_path / "labels.csv")
    capsys.readouterr()

    assert main(["eval-narratives", ledger, "--select", "--labels", labels]) == 2  # default --cases
    assert "default synthetic ledger" in capsys.readouterr().out
    assert not (tmp_path / "evals").exists()
    cases = tmp_path / "cases.json"
    assert main(["eval-narratives", ledger, "--select", "--labels", labels,
                 "--cases", str(cases)]) == 0
    capsys.readouterr()

    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    argv = ["eval-narratives", ledger, "--cases", str(cases), "--out", str(tmp_path / "r.md"),
            "--limit", "2"]
    assert main(argv) == 2  # default --runs-dir would be the committed directory
    out = capsys.readouterr().out
    assert "default synthetic ledger" in out and "runs" in out
    assert client.calls == [] and not (tmp_path / "evals").exists()
    # Nor does spelling a committed path out, an empty one, or the report's default.
    for spelled in (["--runs-dir", "evals/narratives/runs/2026-10-01-claude-x"],
                    ["--runs-dir", "./evals/narratives/runs/x"], ["--runs-dir", "docs"]):
        assert main(argv + spelled) == 2, spelled
        assert "committed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(argv + ["--runs-dir", ""])
    capsys.readouterr()
    no_out = [a for a in argv if a not in ("--out", str(tmp_path / "r.md"))]
    assert main(no_out + ["--runs-dir", str(tmp_path / "runs")]) == 2  # docs/narrative-eval.md
    assert "the report" in capsys.readouterr().out
    assert main(["eval-narratives", ledger, "--select", "--labels", labels,
                 "--cases", "./evals/narratives/cases.json", "--overwrite"]) == 2
    capsys.readouterr()
    assert client.calls == []
    assert not (tmp_path / "evals").exists() and not (tmp_path / "docs").exists()

    assert main(argv + ["--runs-dir", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "not the default synthetic ledger" in out
    rows = [json.loads(line) for line in (tmp_path / "runs" / "results.jsonl").read_text().splitlines()]
    digest = cli.narrative_eval.ledger_digest(load_csv(ledger))
    assert digest != cli.narrative_eval.DEFAULT_LEDGER_SHA256
    assert len(rows) == 2 and all(r["ledger_sha256"] == digest for r in rows)
    assert "not the default synthetic ledger" in (tmp_path / "r.md").read_text()


def test_eval_narratives_names_a_missing_or_damaged_case_file(tmp_path, capsys):
    """Run from a directory that is not the checkout, the default case path is missing: a
    message and exit 2, not a traceback; a file that is not a case set likewise."""
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    common = ["eval-narratives", str(tmp_path / "ledger.csv"),
              "--runs-dir", str(tmp_path / "runs"), "--out", str(tmp_path / "report.md")]
    assert main(common + ["--cases", str(tmp_path / "missing.json")]) == 2
    assert "case file not found" in capsys.readouterr().out
    (tmp_path / "damaged.json").write_text("{not json")
    assert main(common + ["--cases", str(tmp_path / "damaged.json")]) == 2
    assert "damaged.json" in capsys.readouterr().out

    # Every case the loader refuses is a message too, whatever the reason.
    good = {"entry_id": "JE-2024-000001", "archetype": "a", "tests_fired": "JET-01",
            "why_chosen": "w"}
    refused = {
        "a pattern that is not a regex": {"cases": [{**good, "must_mention": ["1,?322("]}]},
        "a misspelt key": {"cases": [{**good, "overides": {}}]},
        "a missing field": {"cases": [{"entry_id": "JE-2024-000001"}]},
        "no case list": {"about": "x"},
        "a list, not an object": [good],
        "overrides that are not an object": {"cases": [{**good, "overrides": 5}]},
        "overrides that are null": {"cases": [{**good, "overrides": None}]},
        "a pattern list that is a string": {"cases": [{**good, "must_mention": "1,322"}]},
    }
    for why, payload in refused.items():
        (tmp_path / "refused.json").write_text(json.dumps(payload))
        assert main(common + ["--cases", str(tmp_path / "refused.json")]) == 2, why
        out = capsys.readouterr().out
        assert out.startswith("error:") and "refused.json" in out, why
    assert main(common + ["--cases", str(tmp_path)]) == 2  # a directory
    assert capsys.readouterr().out.startswith("error:")


def test_eval_narratives_checks_override_lines_before_narrating_anything(
        tmp_path, capsys, monkeypatch, llm):
    """An override on a line the entry lacks is a stale case set, not a traceback mid-run."""
    from ledgerlens import cli
    from ledgerlens.ingest import load_csv
    from ledgerlens.narrate import Narrator

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30", "--out-dir", str(tmp_path)])
    ledger, labels = str(tmp_path / "ledger.csv"), str(tmp_path / "labels.csv")
    cases = tmp_path / "cases.json"
    main(["eval-narratives", ledger, "--select", "--labels", labels, "--cases", str(cases)])
    payload = json.loads(cases.read_text())
    last = payload["cases"][-1]
    beyond = int(load_csv(ledger).query("entry_id == @last['entry_id']")["line_no"].max()) + 1
    last["overrides"] = {"lines": {str(beyond): {"description": "an extra line"}}}
    cases.write_text(json.dumps(payload))
    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    capsys.readouterr()

    assert main(["eval-narratives", ledger, "--cases", str(cases), "--out", str(tmp_path / "r.md"),
                 "--runs-dir", str(tmp_path / "runs")]) == 2
    out = capsys.readouterr().out
    assert f"{last['entry_id']}: no line {beyond} to override" in out
    assert client.calls == [] and not (tmp_path / "runs" / "results.jsonl").exists()


def test_eval_narratives_rejects_a_limit_below_one(capsys):
    """--limit 0 must not mean "every case", which is a paid run."""
    with pytest.raises(SystemExit) as refused:
        main(["eval-narratives", "x.csv", "--limit", "0"])
    assert refused.value.code == 2
    assert "at least 1" in capsys.readouterr().err


def test_eval_narratives_grades_the_cases_and_writes_the_report(tmp_path, capsys, monkeypatch,
                                                                 llm):
    from ledgerlens import cli
    from ledgerlens.narrate import Narrator

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    ledger, labels = str(tmp_path / "ledger.csv"), str(tmp_path / "labels.csv")
    cases = tmp_path / "cases.json"
    main(["eval-narratives", ledger, "--select", "--labels", labels, "--cases", str(cases)])
    capsys.readouterr()
    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))

    report, runs = tmp_path / "report.md", tmp_path / "runs"
    argv = ["eval-narratives", ledger, "--cases", str(cases), "--out", str(report),
            "--runs-dir", str(runs), "--limit", "3"]
    code = main(argv)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Graded 3/3" in out
    assert report.read_text().startswith("# Narrative eval")
    rows = [json.loads(line) for line in (runs / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    assert len(client.calls) == 3

    # Every row and the report say which case file graded them; the report also
    # carries the grader's history and the constant-answer confidence baseline.
    digest = hashlib.sha256(cases.read_bytes()).hexdigest()
    assert all(r["cases_sha256"] == digest for r in rows)
    text = report.read_text()
    assert digest in text and "## Grader notes" in text and "Confidence baseline" in text

    # Editing a band after the run: a plain re-grade is refused, an explicit one is disclosed.
    payload = json.loads(cases.read_text())
    payload["cases"][0]["expected_confidence"] = ["low"]
    cases.write_text(json.dumps(payload))
    assert main(argv + ["--regrade"]) == 2
    assert "allow-cases-change" in capsys.readouterr().out
    assert main(argv + ["--regrade", "--allow-cases-change"]) == 0
    assert len(client.calls) == 3  # neither re-grade called the API
    text = report.read_text()
    assert "Provenance note" in text and digest in text
    assert hashlib.sha256(cases.read_bytes()).hexdigest() in text

    # Starting over into a directory that holds rows is refused, not overwritten.
    assert main(argv + ["--no-resume"]) == 2
    assert "never deleted" in capsys.readouterr().out
    assert len(client.calls) == 3


def test_narrate_and_report_refuse_a_database_they_cannot_open_without_a_traceback(
        tmp_path, capsys, monkeypatch):
    import sqlite3

    from ledgerlens.review import ReviewStore

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-03-31", "--out-dir", str(tmp_path)])
    ledger, out = str(tmp_path / "ledger.csv"), str(tmp_path / "w.xlsx")
    future = tmp_path / "future.sqlite"
    ReviewStore(future, "csv:" + "a" * 64)
    with sqlite3.connect(str(future)) as raw:
        raw.execute("PRAGMA user_version = 99")
    capsys.readouterr()

    assert main(["report", ledger, "--no-model", "--db", str(future), "--out", out]) == 2
    assert "newer LedgerLens" in capsys.readouterr().out
    # narrate opens the store before it builds an API client, so this needs no key.
    assert main(["narrate", ledger, "--no-model", "--db", str(future)]) == 2
    assert "newer LedgerLens" in capsys.readouterr().out
    assert main(["adopt-legacy", ledger, "--db", str(future)]) == 2
    assert "newer LedgerLens" in capsys.readouterr().out

    notes = tmp_path / "notes.sqlite"
    notes.write_text("not a database\n")
    assert main(["report", ledger, "--no-model", "--db", str(notes), "--out", out]) == 2
    assert "not a SQLite database" in capsys.readouterr().out
    assert notes.read_text() == "not a database\n"


def test_narrate_files_each_ledger_under_its_own_identity(tmp_path, capsys, monkeypatch, llm):
    """Two ledgers that share entry ids share one database and never each other's notes."""
    from ledgerlens import cli
    from ledgerlens.ingest import ledger_identity, load_csv
    from ledgerlens.narrate import Narrator
    from ledgerlens.review import ReviewStore

    monkeypatch.chdir(tmp_path)
    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    db = tmp_path / "review.sqlite"
    ledgers = []
    for end in ("2024-03-31", "2024-06-30"):
        out = tmp_path / end
        main(["generate", "--start", "2024-01-01", "--end", end, "--out-dir", str(out)])
        ledgers.append(str(out / "ledger.csv"))
    capsys.readouterr()
    for ledger in ledgers:
        assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model"]) == 0
        out = capsys.readouterr().out
        assert "Skipped" not in out  # the second ledger's run starts from nothing of its own
    identities = [ledger_identity(load_csv(path), path) for path in ledgers]
    assert identities[0] != identities[1]
    for ident in identities:
        assert len(ReviewStore(db, ident).narrative_ids()) == 4
    assert ReviewStore(db, identities[0]).ledgers()["ledger_id"].tolist() == sorted(identities)
    assert len(client.calls) == 8


def test_narrate_refuses_to_ignore_legacy_rows_unless_told_to(tmp_path, capsys, monkeypatch, llm):
    """Rows from before ledgers were keyed are adopted or set aside on purpose, never bought again."""
    import sqlite3

    from ledgerlens import cli
    from ledgerlens.ingest import ledger_identity, load_csv
    from ledgerlens.narrate import Narrator
    from ledgerlens.review import ReviewStore

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-03-31", "--out-dir", str(tmp_path)])
    ledger, db = str(tmp_path / "ledger.csv"), tmp_path / "review.sqlite"
    ReviewStore(db, ledger_identity(load_csv(ledger), ledger))
    with sqlite3.connect(str(db)) as raw:  # a note from before ledgers were keyed
        raw.execute("INSERT INTO narratives (entry_id, summary, generated_at, ledger_id) VALUES "
                    "('JE-2024-000001', 'old', '2026-09-23T00:00:00+00:00', 'legacy')")
    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    capsys.readouterr()

    # The refusal comes before anything is narrated: asked for four notes, it buys none.
    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model"]) == 2
    out = capsys.readouterr().out
    assert "adopt-legacy" in out and client.calls == []
    assert ReviewStore(db, ledger_identity(load_csv(ledger), ledger)).narrative_ids() == set()
    fresh = tmp_path / "fresh.sqlite"
    ReviewStore(fresh, ledger_identity(load_csv(ledger), ledger))
    assert main(["adopt-legacy", ledger, "--db", str(fresh)]) == 0  # no legacy rows at all
    assert "Nothing to adopt" in capsys.readouterr().out
    assert main(["adopt-legacy", ledger, "--db", str(tmp_path / "absent.sqlite")]) == 2
    assert main(["adopt-legacy", ledger, "--db", str(db)]) == 0
    assert "Adopted 1 narrative(s) and 0 decision(s)" in capsys.readouterr().out
    assert main(["adopt-legacy", ledger, "--db", str(db)]) == 2
    assert "already holds" in capsys.readouterr().out
    assert main(["narrate", ledger, "--db", str(db), "--top", "0", "--no-model"]) == 0
    assert "Skipped 1" in capsys.readouterr().out

    # Rows for entries this ledger does not have are refused; the ledger may
    # then start from scratch when told so.
    other = tmp_path / "other.sqlite"
    ReviewStore(other, ledger_identity(load_csv(ledger), ledger))
    with sqlite3.connect(str(other)) as raw:
        raw.execute("INSERT INTO narratives (entry_id, summary, generated_at, ledger_id) VALUES "
                    "('JE-9999-000001', 'stray', '2026-09-23T00:00:00+00:00', 'legacy')")
    assert main(["adopt-legacy", ledger, "--db", str(other)]) == 2
    assert "not in this ledger" in capsys.readouterr().out
    assert main(["narrate", ledger, "--db", str(other), "--top", "1", "--no-model",
                 "--ignore-legacy"]) == 0
    assert len(client.calls) == 1  # told to start from scratch, it narrates
    assert len(ReviewStore(other, ledger_identity(load_csv(ledger), ledger)).narrative_ids()) == 1


@pytest.mark.parametrize("payload", ['{"ledger_id": "qbo 123"}', ""])
def test_a_malformed_identity_sidecar_stops_every_command_with_a_message(
        tmp_path, capsys, monkeypatch, payload):
    """A sidecar that names no ledger is never guessed around, and never a traceback."""
    from ledgerlens.review import ReviewStore

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-03-31", "--out-dir", str(tmp_path)])
    ledger, db = str(tmp_path / "ledger.csv"), tmp_path / "review.sqlite"
    ReviewStore(db, "csv:" + "a" * 64)
    (tmp_path / "ledger.identity.json").write_text(payload)
    capsys.readouterr()
    for argv in (["narrate", ledger, "--no-model", "--top", "0", "--db", str(db)],
                 ["report", ledger, "--no-model", "--db", str(db), "--out", str(tmp_path / "w.xlsx")],
                 ["adopt-legacy", ledger, "--db", str(db)]):
        assert main(argv) == 2, argv[0]
        out = capsys.readouterr().out
        assert "error:" in out and "ledger.identity.json" in out, argv[0]


# --- QuickBooks Online: pull-qbo and qbo-auth (offline) ---------------------------

QBO_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qbo"
QBO_ENV = {"QBO_CLIENT_ID": "CLIENT-ID", "QBO_CLIENT_SECRET": "CLIENT-SECRET",
           "QBO_ENVIRONMENT": "sandbox"}


@pytest.fixture
def qbo_env(tmp_path, monkeypatch):
    """A clean QuickBooks environment: no .env in the working directory, no QBO_* leaking in
    from the shell, and a token directory outside any git checkout."""
    from ledgerlens.connectors.tokens import repository_root

    if repository_root(tmp_path) is not None:
        pytest.skip(f"{tmp_path} is inside a git repository")
    monkeypatch.chdir(tmp_path)
    for name in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_ENVIRONMENT", "QBO_REALM_ID",
                 "QBO_REDIRECT_URI", "QBO_TIMEZONE", "LEDGERLENS_TOKEN_DIR"):
        monkeypatch.delenv(name, raising=False)
    tokens = tmp_path / "tokens"
    monkeypatch.setenv("LEDGERLENS_TOKEN_DIR", str(tokens))
    return SimpleNamespace(root=tmp_path, tokens=tokens, set=lambda **kv: [
        monkeypatch.setenv(k, v) for k, v in {**QBO_ENV, **kv}.items()])


def test_pull_qbo_from_fixtures_writes_a_ledger_every_command_reads(qbo_env, capsys):
    out = qbo_env.root / "data" / "qbo-ledger.csv"
    code = main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31",
                 "--fixtures", str(QBO_FIXTURES / "pull"), "--out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "Wrote 13 lines / 6 entries" in printed
    assert "Identity qbo:sandbox-fixtures written to" in printed
    assert "small population" in printed and "not used" not in printed
    sidecar = out.with_suffix(".identity.json")
    assert json.loads(sidecar.read_text())["ledger_id"] == "qbo:sandbox-fixtures"
    assert main(["test", str(out)]) == 0
    assert "QBO-JournalEntry-147" in capsys.readouterr().out
    assert main(["report", str(out), "--no-model", "--out", str(qbo_env.root / "wp.xlsx")]) == 0


@pytest.mark.parametrize("argv, env, expected", [
    (["--start", "2025-12-31", "--end", "2025-10-01"], {}, "is after --end"),
    ([], None, "missing QBO_CLIENT_ID, QBO_CLIENT_SECRET"),
    ([], {"QBO_ENVIRONMENT": "production", "QBO_REALM_ID": "1"}, "--allow-production"),
    (["--record", "rec"], {"QBO_ENVIRONMENT": "production", "QBO_REALM_ID": "1"}, "only sandbox data"),
    ([], {}, "no realm id"),
    ([], {"QBO_REALM_ID": "4620816365"}, "no tokens for sandbox company 4620816365.*qbo-auth"),
    (["--out", "data/ledger.txt"], {}, "must be a .csv file"),
    (["--realm-id", "_x"], {}, "not letters, digits"),
    ([], {"QBO_REALM_ID": "12 34"}, "not letters, digits"),
])
def test_pull_qbo_refuses_what_it_cannot_do_with_a_message(qbo_env, capsys, argv, env, expected):
    if env is not None:
        qbo_env.set(**env)
    args = ["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31", *argv]
    if "--start" in argv:
        args = ["pull-qbo", *argv]
    assert main(args) == 2
    assert re.search(expected, capsys.readouterr().out)
    assert not (qbo_env.root / "data" / "qbo-ledger.csv").exists()


def _stored_tokens(qbo_env, realm="4620816365"):
    from ledgerlens.connectors.tokens import Tokens, TokenStore

    store = TokenStore.for_realm("sandbox", realm, qbo_env.tokens)
    store.save(Tokens.issued("STORED-ACCESS", "STORED-REFRESH", 3600, 86400, realm, "sandbox"))
    return store


def test_a_live_pull_uses_the_stored_tokens_and_records_sanitized_fixtures(qbo_env, capsys, monkeypatch):
    """The live path end to end, with the network replaced by the pull fixtures: the realm
    comes from the only token file, and --record writes fixtures that replay the same pull."""
    from ledgerlens.connectors import qbo

    qbo_env.set()
    _stored_tokens(qbo_env)
    live = qbo.RecordedTransport(QBO_FIXTURES / "pull")
    monkeypatch.setattr(qbo, "default_transport", lambda: live)
    recorded = qbo_env.root / "recorded"
    code = main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31", "--record", str(recorded)])
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "Identity qbo:4620816365" in printed and "Recorded 3 sanitized fixture(s)" in printed
    assert "STORED-ACCESS" not in "".join(p.read_text() for p in recorded.glob("*.json"))
    first = (qbo_env.root / "data" / "qbo-ledger.csv").read_text()
    (qbo_env.root / "data" / "qbo-ledger.csv").unlink()
    assert main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31", "--realm-id",
                 "4620816365", "--fixtures", str(recorded)]) == 0
    assert (qbo_env.root / "data" / "qbo-ledger.csv").read_text() == first
    assert "not used" not in capsys.readouterr().out
    assert main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31", "--record", str(recorded)]) == 2
    assert "already holds fixtures" in capsys.readouterr().out


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_qbo_auth_signs_in_and_stores_the_tokens_privately(qbo_env, capsys, monkeypatch):
    import stat
    import threading
    import urllib.parse
    import urllib.request
    import webbrowser

    from ledgerlens.connectors import qbo

    port = _free_port()
    qbo_env.set(QBO_REDIRECT_URI=f"http://localhost:{port}/callback")
    monkeypatch.setattr(qbo, "default_transport",
                        lambda: qbo.RecordedTransport(QBO_FIXTURES / "auth"))
    visited = []

    def browser(url):  # the user signs in; Intuit redirects back with a code
        state = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))["state"]
        back = f"http://127.0.0.1:{port}/callback?" + urllib.parse.urlencode(
            {"code": "ONE-TIME-CODE", "state": state, "realmId": "4620816365"})
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        thread = threading.Thread(target=lambda: visited.append(direct.open(back, timeout=5).status))
        thread.start()
        threads.append(thread)
        return True

    threads = []
    monkeypatch.setattr(webbrowser, "open", browser)
    code = main(["qbo-auth", "--timeout", "10"])
    for thread in threads:
        thread.join(5)
    printed = capsys.readouterr().out
    assert code == 0, printed
    token_file = qbo_env.tokens / "qbo-sandbox-4620816365.json"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert json.loads(token_file.read_text())["access_token"] == "TEST-ACCESS"
    assert "Add to .env: QBO_REALM_ID=4620816365" in printed
    for secret in ("TEST-ACCESS", "TEST-REFRESH", "ONE-TIME-CODE", "CLIENT-SECRET"):
        assert secret not in printed
    assert visited == [200]


def test_qbo_auth_times_out_without_writing_anything(qbo_env, capsys):
    qbo_env.set(QBO_REDIRECT_URI=f"http://localhost:{_free_port()}/callback")
    assert main(["qbo-auth", "--timeout", "0.3", "--no-browser"]) == 1
    assert "no sign-in arrived" in capsys.readouterr().out
    assert list(qbo_env.tokens.iterdir()) == []  # the private directory is made first; no tokens


def test_a_qbo_sidecar_keys_adopt_legacy_narrate_and_the_report(tmp_path, capsys, monkeypatch, llm):
    """A QuickBooks pull's ledger is filed under qbo:<realm> on every surface that writes or
    reads review rows, whatever the CSV's digest (a re-pull changes the digest, not the key)."""
    import sqlite3

    import openpyxl

    from ledgerlens import cli
    from ledgerlens.narrate import Narrator
    from ledgerlens.review import ReviewStore

    monkeypatch.chdir(tmp_path)
    main(["generate", "--start", "2024-01-01", "--end", "2024-03-31", "--out-dir", str(tmp_path)])
    ledger, db = str(tmp_path / "ledger.csv"), tmp_path / "review.sqlite"
    (tmp_path / "ledger.identity.json").write_text('{"ledger_id": "qbo:4620816365"}')
    ReviewStore(db, "qbo:4620816365")
    with sqlite3.connect(str(db)) as raw:
        raw.execute("INSERT INTO narratives (entry_id, summary, generated_at, ledger_id) VALUES "
                    "('JE-2024-000001', 'old', '2026-09-23T00:00:00+00:00', 'legacy')")
    capsys.readouterr()

    assert main(["adopt-legacy", ledger, "--db", str(db)]) == 0
    assert "into qbo:4620816365" in capsys.readouterr().out

    client = llm.client()
    monkeypatch.setattr(cli, "Narrator", lambda **kw: Narrator(client=client, **kw))
    assert main(["narrate", ledger, "--db", str(db), "--top", "2", "--no-model"]) == 0
    assert "rows keyed by qbo:4620816365" in capsys.readouterr().out
    store = ReviewStore(db, "qbo:4620816365")
    assert len(store.narrative_ids()) >= 2 and "JE-2024-000001" in store.narrative_ids()
    assert set(store.ledgers()["ledger_id"]) == {"legacy", "qbo:4620816365"}

    out = tmp_path / "wp.xlsx"
    assert main(["report", ledger, "--no-model", "--db", str(db), "--out", str(out)]) == 0
    cells = {str(c.value) for ws in openpyxl.load_workbook(out) for row in ws.iter_rows()
             for c in row if c.value is not None}
    assert "qbo:4620816365" in cells


def test_nothing_is_spent_when_the_tokens_could_not_be_saved(qbo_env, capsys, monkeypatch):
    """A code is single-use and a refresh rotates the token: a token directory the store
    would refuse is refused before the browser opens or any request is sent."""
    import webbrowser

    from ledgerlens.connectors import qbo

    qbo_env.set(QBO_REDIRECT_URI=f"http://localhost:{_free_port()}/callback")
    store = _stored_tokens(qbo_env)
    qbo_env.tokens.chmod(0o755)
    sent = []
    monkeypatch.setattr(qbo, "default_transport",
                        lambda: SimpleNamespace(request=lambda *a, **k: sent.append(a)))
    monkeypatch.setattr(webbrowser, "open", lambda url: sent.append(url))
    assert main(["qbo-auth", "--timeout", "0.3"]) == 2
    assert "mode is 755" in capsys.readouterr().out
    assert main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31"]) == 2
    assert "mode is 755" in capsys.readouterr().out
    assert sent == [] and store.load().refresh_token == "STORED-REFRESH"
    qbo_env.tokens.chmod(0o700)
    (qbo_env.root / "rec").write_text("a file")
    assert main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31",
                 "--record", str(qbo_env.root / "rec")]) == 2
    assert "is a file" in capsys.readouterr().out and sent == []


def _replay_to(out, *extra):
    return main(["pull-qbo", "--start", "2025-10-01", "--end", "2025-12-31",
                 "--fixtures", str(QBO_FIXTURES / "pull"), "--out", str(out), *extra])


def test_the_synthetic_ledger_and_its_identity_are_refused_however_spelled(qbo_env, capsys, monkeypatch):
    (qbo_env.root / ".git").mkdir()  # the working directory is a checkout, as in real use
    data = qbo_env.root / "data"
    data.mkdir()
    synthetic = data / "ledger.csv"
    synthetic.write_text("the synthetic ledger\\n")
    spellings = ["data/ledger.csv", "./data/../data/ledger.csv"]
    if (qbo_env.root / "DATA" / "LEDGER.CSV").exists():  # a case-insensitive disk, as on macOS
        spellings.append("data/LEDGER.csv")
    for spelled in spellings:
        assert _replay_to(spelled) == 2, spelled
        assert "synthetic ledger" in capsys.readouterr().out
    (qbo_env.root / "src").mkdir()
    monkeypatch.chdir(qbo_env.root / "src")
    assert _replay_to("../data/ledger.csv") == 2  # from another directory of the checkout
    monkeypatch.chdir(qbo_env.root)
    assert _replay_to("data/ledger.txt") == 2  # would take over data/ledger.identity.json
    assert synthetic.read_text() == "the synthetic ledger\\n"
    assert not (data / "ledger.identity.json").exists()


def test_pulled_books_stay_in_the_checkouts_ignored_data_directory(qbo_env, capsys):
    repo = qbo_env.root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "tests").mkdir()
    assert _replay_to(repo / "tests" / "q.csv") == 2
    assert "not in its data/ directory" in capsys.readouterr().out
    assert _replay_to(repo / "q.csv") == 2
    assert _replay_to(repo / "data" / "q.csv") == 0
    assert (repo / "data" / "q.identity.json").exists()
    assert _replay_to(qbo_env.root / "elsewhere" / "q.csv") == 0  # outside any checkout

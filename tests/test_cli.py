import hashlib
import json

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
    ReviewStore(db, ledger_identity(load_csv(csv), csv)).record(
        Decision("JE-2024-000001", "dismiss", "ana"))
    code = main(["report", str(tmp_path / "ledger.csv"), "--no-model", "--db", str(db),
                 "--out", str(tmp_path / "b.xlsx")])
    assert code == 0


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

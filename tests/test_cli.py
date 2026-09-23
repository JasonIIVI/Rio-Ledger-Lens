import json

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

    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "Narrated 4/4" in out
    assert "read from cache" in out
    assert len(ReviewStore(db).narrative_ids()) == 4
    assert client.calls[0]["output_config"]["effort"] == "medium"

    # Cached entries are skipped, so the next run narrates the next four down.
    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model"]) == 0
    assert "Skipped 4" in capsys.readouterr().out
    assert len(ReviewStore(db).narrative_ids()) == 8
    assert len(client.calls) == 8

    # --force redoes the top four instead of moving on.
    assert main(["narrate", ledger, "--db", str(db), "--top", "4", "--no-model", "--force"]) == 0
    assert len(ReviewStore(db).narrative_ids()) == 8
    assert len(client.calls) == 12


def test_report_attaches_an_existing_review_db_only(tmp_path, capsys):
    from ledgerlens.review import Decision, ReviewStore

    main(["generate", "--start", "2024-01-01", "--end", "2024-06-30",
          "--out-dir", str(tmp_path)])
    capsys.readouterr()
    missing = tmp_path / "absent.sqlite"
    main(["report", str(tmp_path / "ledger.csv"), "--no-model", "--db", str(missing),
          "--out", str(tmp_path / "a.xlsx")])
    assert "without review columns" in capsys.readouterr().out
    assert not missing.exists()  # a report must not create a review database

    db = tmp_path / "review.sqlite"
    ReviewStore(db).record(Decision("JE-2024-000001", "dismiss", "ana"))
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

    code = main(["eval-narratives", ledger, "--cases", str(cases), "--out",
                 str(tmp_path / "report.md"), "--runs-dir", str(tmp_path / "runs"),
                 "--limit", "3"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Graded 3/3" in out
    assert (tmp_path / "report.md").read_text().startswith("# Narrative eval")
    assert len((tmp_path / "runs" / "results.jsonl").read_text().splitlines()) == 3
    assert len(client.calls) == 3

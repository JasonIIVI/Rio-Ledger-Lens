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

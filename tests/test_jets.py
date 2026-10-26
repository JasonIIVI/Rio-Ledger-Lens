import pandas as pd

from ledgerlens import jets
from ledgerlens.ingest import prepare
from ledgerlens.schema import AnomalyType


def _ids_for(labels, archetype):
    return set(labels.loc[labels["anomaly_type"] == archetype, "entry_id"])


def test_registry_ids_match_emitted_ids(ledger):
    flags = jets.run_all(ledger)
    assert set(flags["test_id"]) <= set(jets.REGISTRY)


def test_every_flag_has_a_human_readable_reason(ledger):
    flags = jets.run_all(ledger)
    assert not flags.empty
    assert flags["reason"].notna().all()
    assert (flags["reason"].str.len() > 20).all()


def test_flag_frame_shape_is_stable(ledger):
    flags = jets.run_all(ledger, only=["JET-01"])
    assert list(flags.columns) == list(jets.FLAG_COLUMNS)


def test_unknown_test_id_raises(ledger):
    try:
        jets.run_all(ledger, only=["JET-99"])
    except KeyError as exc:
        assert "JET-99" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected KeyError")


# --- each test finds what it is supposed to find ----------------------------


def test_weekend_test_catches_injected_weekend_entries(ledger, labels):
    caught = set(jets.jet_weekend_entry(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.WEEKEND_ENTRY) <= caught


def test_holiday_test_catches_injected_holiday_entries(ledger, labels):
    caught = set(jets.jet_holiday_entry(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.HOLIDAY_ENTRY) <= caught


def test_after_hours_test_catches_injected_entries(ledger, labels):
    caught = set(jets.jet_after_hours(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.AFTER_HOURS_ENTRY) <= caught


def test_round_amount_test_catches_injected_entries(ledger, labels):
    caught = set(jets.jet_round_amount(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.ROUND_AMOUNT) <= caught


def test_threshold_test_catches_injected_entries(ledger, labels):
    caught = set(jets.jet_just_under_threshold(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.JUST_UNDER_THRESHOLD) <= caught


def test_duplicate_test_catches_injected_duplicates(ledger, labels):
    caught = set(jets.jet_duplicate_entries(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.DUPLICATE_ENTRY) <= caught


def test_unbalanced_test_catches_injected_imbalances(ledger, labels):
    caught = set(jets.jet_unbalanced_entry(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.UNBALANCED_ENTRY) <= caught


def test_period_end_revenue_test_catches_injected_entries(ledger, labels):
    caught = set(jets.jet_period_end_manual_revenue(ledger)["entry_id"])
    assert _ids_for(labels, AnomalyType.PERIOD_END_MANUAL_REVENUE) <= caught


def test_weekend_test_only_flags_weekends(ledger):
    flagged = set(jets.jet_weekend_entry(ledger)["entry_id"])
    weekend_ids = set(ledger.loc[ledger["is_weekend"], "entry_id"])
    assert flagged == weekend_ids


def test_unbalanced_test_is_silent_on_a_balanced_population(ledger, labels):
    unbalanced = _ids_for(labels, AnomalyType.UNBALANCED_ENTRY)
    clean = ledger[~ledger["entry_id"].isin(unbalanced)]
    assert jets.jet_unbalanced_entry(clean).empty


# --- scoring ----------------------------------------------------------------


def test_scoring_covers_every_entry(ledger):
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    assert len(scored) == ledger["entry_id"].nunique()


def test_unflagged_entries_score_zero(ledger):
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    unflagged = scored[~scored["entry_id"].isin(flags["entry_id"])]
    assert (unflagged["risk_score"] == 0).all()


def test_score_is_severity_weighted(ledger):
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    top = scored.iloc[0]
    assert top["risk_score"] > 0
    # Highest-scoring entry should have tripped more than one test.
    assert top["n_flags"] >= 1


def test_scoring_handles_zero_flags(ledger):
    empty = pd.DataFrame(columns=list(jets.FLAG_COLUMNS))
    scored = jets.score_entries(ledger, empty)
    assert (scored["risk_score"] == 0).all()
    assert len(scored) == ledger["entry_id"].nunique()


# --- ties on the posting date -------------------------------------------------


def _entries(rows):
    """A tiny ledger from (entry id, posted, keyed, debit account, credit account, amount,
    description) rows: two lines per entry, the debit line first."""
    lines = []
    for entry_id, posted, keyed, debit_account, credit_account, amount, description in rows:
        sides = ((debit_account, amount, 0.0), (credit_account, 0.0, amount))
        for line_no, (account, debit, credit) in enumerate(sides, start=1):
            lines.append({
                "entry_id": entry_id, "line_no": line_no,
                "posting_date": pd.Timestamp(posted), "entered_at": pd.Timestamp(keyed),
                "fiscal_year": 2024, "period": pd.Timestamp(posted).month,
                "account_code": account, "account_name": "Account " + account,
                "account_type": "Asset", "description": description, "debit": debit,
                "credit": credit, "source": "Manual", "created_by": "u1",
            })
    return prepare(pd.DataFrame(lines))


def test_a_same_day_duplicate_is_the_one_keyed_later():
    """Two postings of one invoice on one day tie on the date. The one keyed first stands as
    the original and the other is flagged, whatever order the rows arrive in; keyed at the
    same time, the lower id stands. Left to a one-key sort, the tie was the sort's to order,
    and the original could be flagged as a duplicate of its own re-post."""
    rows = [("JE-1", "2024-03-15", "2024-03-15 10:00", "6000", "1000", 500.0, "Invoice 42"),
            ("JE-2", "2024-03-15", "2024-03-15 09:00", "6000", "1000", 500.0, "Invoice 42")]
    for order in (rows, rows[::-1]):
        flags = jets.jet_duplicate_entries(_entries(order))
        assert list(flags["entry_id"]) == ["JE-1"]
        assert "as JE-2 posted 0 day(s) earlier" in flags["reason"].iloc[0]
    same_time = [row[:2] + ("2024-03-15 09:00",) + row[3:] for row in rows]
    for order in (same_time, same_time[::-1]):
        assert list(jets.jet_duplicate_entries(_entries(order))["entry_id"]) == ["JE-2"]
    # the posting date still decides first: JE-3, keyed after JE-4 but posted the day
    # before it, is the original
    backdated = [
        ("JE-3", "2024-03-10", "2024-03-12 09:00", "6000", "1000", 500.0, "Invoice 43"),
        ("JE-4", "2024-03-11", "2024-03-11 09:00", "6000", "1000", 500.0, "Invoice 43")]
    flags = jets.jet_duplicate_entries(_entries(backdated))
    assert list(flags["entry_id"]) == ["JE-4"]
    assert "as JE-3 posted 1 day(s) earlier" in flags["reason"].iloc[0]


def test_a_dormant_account_is_woken_by_the_entry_keyed_first():
    """Two entries on the day a quiet account wakes tie on the date. The gap, and so the
    flag, go to the one keyed first, whatever order the rows arrive in; keyed at the same
    time, to the lower id."""
    rows = [("JE-1", "2024-01-02", "2024-01-02 09:00", "6900", "1000", 100.0, "a"),
            ("JE-2", "2024-09-02", "2024-09-02 11:00", "6900", "1000", 200.0, "b"),
            ("JE-3", "2024-09-02", "2024-09-02 08:00", "6900", "1000", 300.0, "c")]
    for order in (rows, rows[::-1]):
        flags = jets.jet_dormant_account(_entries(order))
        assert set(flags["entry_id"]) == {"JE-3"}
        assert flags["reason"].str.contains("no activity for 244 days").all()
    same_time = [row[:2] + ("2024-09-02 08:00",) + row[3:] if row[0] != "JE-1" else row
                 for row in rows]
    for order in (same_time, same_time[::-1]):
        assert set(jets.jet_dormant_account(_entries(order))["entry_id"]) == {"JE-2"}
    # the posting date still decides first: JE-5, posted before JE-4 though keyed after
    # it, is the entry that wakes the account
    backdated = [rows[0],
                 ("JE-5", "2024-09-02", "2024-09-05 09:00", "6900", "1000", 200.0, "b"),
                 ("JE-4", "2024-09-04", "2024-09-03 09:00", "6900", "1000", 300.0, "c")]
    flags = jets.jet_dormant_account(_entries(backdated))
    assert set(flags["entry_id"]) == {"JE-5"}
    assert flags["reason"].str.contains("no activity for 244 days").all()


def test_a_test_that_flags_two_lines_of_one_entry_counts_twice_in_the_score(ledger):
    """The docstring, the workpaper and the methodology said the score counts distinct tests.
    It sums the flags: JET-11 judges lines, and on the default ledger one entry raises it on
    both lines and scores 4.0 for that one test, while n_flags counts it once."""
    flags = jets.run_all(ledger)
    per_test = flags.groupby(["entry_id", "test_id"]).size()
    assert per_test[per_test > 1].index.tolist() == [("JE-2024-001465", "JET-11")]
    scored = jets.score_entries(ledger, flags).set_index("entry_id").loc["JE-2024-001465"]
    assert (scored["risk_score"], scored["n_flags"], scored["tests_fired"]) == (4.0, 1, "JET-11")


# --- a company's own controls (JET-06's limit, JET-12's approvers) -----------


def test_the_default_controls_render_every_reason_as_before(ledger):
    """JET-06's and JET-12's reasons sit in the committed eval prompts: the synthetic company's
    controls, now read from controls.DEFAULT_CONTROLS, must word them byte for byte as before."""
    from ledgerlens.controls import DEFAULT_CONTROLS
    from ledgerlens.ingest import entry_level

    flags = jets.run_all(ledger)
    entries = entry_level(ledger).set_index("entry_id")
    jet12, jet06 = flags[flags["test_id"] == "JET-12"], flags[flags["test_id"] == "JET-06"]
    assert len(jet12) and len(jet06)
    for r in jet12.itertuples():
        assert r.reason == (f"Manual entry keyed by {entries.loc[r.entry_id, 'created_by']}, who is "
                            "not in the approved list (controller, mchen). Confirm delegated authority.")
    for r in jet06.itertuples():
        assert r.reason == (f"${entries.loc[r.entry_id, 'entry_amount']:,.2f} falls within 3% beneath "
                            "the $10,000 approval threshold. Review for split or structured transactions.")
    pd.testing.assert_frame_equal(flags, jets.run_all(ledger, controls=DEFAULT_CONTROLS))


def _apart_from_controls(flags):
    """The other tests' flags, as records: an empty JET-12 frame changes the stacked frame's
    dtypes on pandas 3 without changing a value."""
    return flags[~flags["test_id"].isin(["JET-06", "JET-12"])].to_dict("records")


def test_a_companys_own_controls_move_jet06_and_jet12_and_nothing_else(ledger):
    from ledgerlens.controls import Controls
    from ledgerlens.ingest import entry_level

    base = jets.run_all(ledger)
    e = entry_level(ledger)
    keyed_by = set(e.loc[e["entry_id"].isin(base.loc[base["test_id"] == "JET-12", "entry_id"]),
                         "created_by"])
    own = Controls(tuple(keyed_by | {"controller", "mchen"}), 5_000.50)
    moved = jets.run_all(ledger, controls=own)
    assert moved[moved["test_id"] == "JET-12"].empty  # everyone who keyed one is an approver now
    under = set(e.loc[(e["entry_amount"] >= 5_000.50 * 0.97) & (e["entry_amount"] < 5_000.50), "entry_id"])
    jet06 = moved[moved["test_id"] == "JET-06"]
    assert under and set(jet06["entry_id"]) == under
    assert jet06["reason"].str.contains("beneath the $5,000.50 approval threshold", regex=False).all()
    assert _apart_from_controls(base) == _apart_from_controls(moved)

    one_more = jets.run_all(ledger, only=["JET-12"], controls=Controls(("zz", "mchen", "controller"), 10_000))
    assert len(one_more) and set(one_more["entry_id"]) == set(base.loc[base["test_id"] == "JET-12", "entry_id"])
    assert one_more["reason"].str.contains("(controller, mchen, zz)", regex=False).all()


def test_detection_reads_nothing_from_the_generator():
    """The approver list and limit were the generator's constants, imported: every ledger was
    tested against the synthetic company's controls whatever company it came from. The source
    names no generator module, and importing detection loads none (``from . import generate``
    names its module in an alias, not in ``ImportFrom.module``)."""
    import os
    import subprocess
    import sys

    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, ledgerlens.jets; print('ledgerlens.generate' in sys.modules)"],
        capture_output=True, text=True, env=dict(os.environ), check=True).stdout.strip()
    assert loaded == "False"
    import ast
    from pathlib import Path

    tree = ast.parse(Path(jets.__file__).read_text(encoding="utf-8"))
    modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    assert not [m for m in modules if m.split(".")[-1] == "generate"], modules



def _entry_at(ledger, amount, created_by=None):
    """The default ledger's first two-line entry, its amount (and keying user) set by hand."""
    first = ledger["entry_id"].iloc[0]
    lines = ledger[ledger["entry_id"] == first].copy()
    assert len(lines) == 2 and (lines["debit"] > 0).sum() == 1
    lines["debit"] = [amount if d > 0 else 0.0 for d in lines["debit"]]
    lines["credit"] = [amount if c > 0 else 0.0 for c in lines["credit"]]
    if created_by is not None:
        lines["created_by"] = created_by
        lines["source"] = "Manual"
    return lines


def test_jet06_counts_from_the_bottom_of_its_window_up_to_but_not_the_limit(ledger):
    from ledgerlens.controls import Controls

    controls = Controls(("controller",), 10_000)  # 3% below it is 9,700.00 exactly in a float
    for amount, flagged in ((9_699.99, False), (9_700.0, True), (9_999.99, True), (10_000.0, False)):
        raised = jets.run_all(_entry_at(ledger, amount), only=["JET-06"], controls=controls)
        assert (len(raised) == 1) is flagged, amount


def test_an_approver_matches_the_user_name_case_included(ledger):
    """The README says names match as written, case included: "Controller" is not "controller"."""
    assert jets.run_all(_entry_at(ledger, 50.0, "controller"), only=["JET-12"]).empty
    assert len(jets.run_all(_entry_at(ledger, 50.0, "Controller"), only=["JET-12"])) == 1


def test_entries_tied_on_score_and_amount_come_in_id_order(raw_ledger):
    """Twenty entries of one amount, each flagged once alike, tie on score and amount, and
    `test` prints the top of this order. A one-key sort keeps tied rows in input order up to
    16 rows and scrambles them from 17; the id breaks the tie, as the context layer's cuts do."""
    lines, _ = raw_ledger
    sizes = lines.groupby("entry_id").size()
    ids = sorted(sizes[sizes == 2].index)[:20]
    part = lines[lines["entry_id"].isin(ids)].copy()
    part["debit"] = part["debit"].where(part["debit"] == 0, 100.0)
    part["credit"] = part["credit"].where(part["credit"] == 0, 100.0)
    flags = pd.DataFrame({"entry_id": ids, "test_id": "JET-01", "test_name": "t",
                          "severity": jets.MEDIUM, "reason": "r"})
    scored = jets.score_entries(prepare(part), flags)
    assert scored["risk_score"].nunique() == 1 and scored["entry_amount"].nunique() == 1
    assert scored["entry_id"].tolist() == ids

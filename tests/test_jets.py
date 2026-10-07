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

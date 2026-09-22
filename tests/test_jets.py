import pandas as pd

from ledgerlens import jets
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

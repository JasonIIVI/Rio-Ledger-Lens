import pandas as pd
import pytest

from ledgerlens import jets
from ledgerlens.features import build_features
from ledgerlens.model import AnomalyModel, combine, score_ledger


def test_scores_are_bounded(ledger):
    scores, _ = score_ledger(ledger)
    assert scores.between(0, 1).all()
    assert len(scores) == ledger["entry_id"].nunique()


def test_scoring_is_deterministic(ledger):
    a, _ = score_ledger(ledger)
    b, _ = score_ledger(ledger)
    pd.testing.assert_series_equal(a, b)


def test_constant_features_are_dropped(ledger):
    """n_lines and posting_lag_days are constant in the current generator.

    They carry no information, and keeping them would overstate how many
    signals the model actually uses.
    """
    X = build_features(ledger)
    model = AnomalyModel().fit(X)
    assert model.report_.n_features_used < model.report_.n_features_in
    assert "n_lines" in model.report_.dropped_constant


def test_must_fit_before_scoring(ledger):
    X = build_features(ledger)
    with pytest.raises(RuntimeError):
        AnomalyModel().score(X)


def test_anomalies_score_higher_than_normals(ledger, labels):
    """The model must at least separate the two populations on average.

    Deliberately a weak assertion. The model is a re-ranker on this data, not a
    strong independent detector, and the test should not pretend otherwise.
    """
    scores, _ = score_ledger(ledger)
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    is_anomaly = scores.index.isin(truth)
    assert scores[is_anomaly].mean() > scores[~is_anomaly].mean()


def test_model_beats_random_selection(ledger, labels):
    scores, _ = score_ledger(ledger)
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    top50 = set(scores.sort_values(ascending=False).head(50).index)
    precision = len(top50 & truth) / 50
    base_rate = len(truth) / len(scores)
    assert precision > base_rate * 5


def test_combine_labels_every_entry(ledger):
    scores, _ = score_ledger(ledger)
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    combined = combine(scored, scores)

    assert len(combined) == ledger["entry_id"].nunique()
    assert set(combined["agreement"]) <= {"both", "rules only", "model only", "neither"}
    assert combined["model_score"].notna().all()


def test_model_flag_is_rank_based(ledger):
    """An Isolation Forest score has no natural scale, so the cutoff is a rank."""
    scores, _ = score_ledger(ledger)
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)

    combined = combine(scored, scores, model_top_pct=0.02)
    expected = round(len(combined) * 0.02)
    assert abs(int(combined["model_flag"].sum()) - expected) <= 2

    wider = combine(scored, scores, model_top_pct=0.10)
    assert int(wider["model_flag"].sum()) > int(combined["model_flag"].sum())

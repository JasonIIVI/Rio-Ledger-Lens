import numpy as np

from ledgerlens.features import FEATURE_COLUMNS, build_features


def test_shape_and_columns(ledger):
    X = build_features(ledger)
    assert list(X.columns) == list(FEATURE_COLUMNS)
    assert len(X) == ledger["entry_id"].nunique()
    assert X.index.name == "entry_id"


def test_no_nulls_or_infinities(ledger):
    X = build_features(ledger)
    assert not X.isna().any().any()
    assert np.isfinite(X.to_numpy()).all()


def test_all_numeric(ledger):
    X = build_features(ledger)
    assert all(np.issubdtype(dt, np.number) for dt in X.dtypes)


def test_no_feature_directly_encodes_a_rule(ledger):
    """The tiers must stay independent.

    If a feature said 'is a weekend' the model would just relearn JET-02, and
    the disagreement between tiers - the whole point of having two - would
    become meaningless.
    """
    X = build_features(ledger)
    forbidden = {"is_weekend", "is_holiday", "entered_dow", "entered_hour"}
    assert forbidden.isdisjoint(set(X.columns))


def test_cyclical_encoding_wraps(ledger):
    X = build_features(ledger)
    for col in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
        assert X[col].between(-1, 1).all()


def test_deterministic(ledger):
    a = build_features(ledger)
    b = build_features(ledger)
    assert a.equals(b)


def test_frequency_features_are_proportions(ledger):
    X = build_features(ledger)
    for col in ("account_pair_freq", "user_freq", "source_freq"):
        assert X[col].between(0, 1).all()

import numpy as np
import pandas as pd
import pytest

from ledgerlens.features import FEATURE_COLUMNS, build_features
from ledgerlens.ingest import prepare


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


def _two_line_entries(rows):
    """A tiny ledger from (entry id, debit account, credit account, amount) rows: two lines
    per entry, the debit line first."""
    lines = []
    for entry_id, debit_account, credit_account, amount in rows:
        sides = ((debit_account, amount, 0.0), (credit_account, 0.0, amount))
        for line_no, (account, debit, credit) in enumerate(sides, start=1):
            lines.append({
                "entry_id": entry_id, "line_no": line_no,
                "posting_date": pd.Timestamp("2024-03-15"),
                "entered_at": pd.Timestamp("2024-03-15 10:00"),
                "fiscal_year": 2024, "period": 3,
                "account_code": account, "account_name": "Account " + account,
                "account_type": "Asset", "description": "line", "debit": debit,
                "credit": credit, "source": "Manual", "created_by": "u1",
            })
    return prepare(pd.DataFrame(lines))


def test_the_entry_account_is_the_first_of_its_largest_lines(ledger):
    """Nearly every entry in the default ledger is two lines of equal amount, so "the largest
    line's account" is a tie. It goes to the first line of the entry, and only the line
    numbers say which that is.
    (The default sort is not stable: left to it, which line came first differed between macOS
    and Linux, and the README's model-tier figures moved with it.)"""
    rows = [("E1", "1000", "2000", 100.0),
            ("E2", "1000", "9000", 10.0), ("E3", "1000", "9000", 1000.0),
            ("E4", "9000", "2000", 10000.0), ("E5", "9000", "2000", 100000.0)]
    # account 1000's log amounts are 1, 2, 3 (median 2, MAD 1); account 2000's are 2, 4, 5
    # (median 4, MAD 1). E1 is 100, log 2: a z of 0 under 1000 and of -1.349 under 2000.
    tiny = _two_line_entries(rows)
    assert build_features(tiny).loc["E1", "amount_z_in_account"] == pytest.approx(0.0)
    swapped = tiny.copy()
    is_e1 = swapped["entry_id"] == "E1"
    swapped.loc[is_e1, "line_no"] = swapped.loc[is_e1, "line_no"].to_numpy()[::-1]
    z = build_features(swapped).loc["E1", "amount_z_in_account"]
    assert z == pytest.approx(0.6745 * (2 - 4) / 1)
    # and the order the rows arrive in changes nothing, on the real ledger either
    forward = build_features(ledger)["amount_z_in_account"]
    backward = build_features(ledger.iloc[::-1])["amount_z_in_account"]
    pd.testing.assert_series_equal(backward.sort_index(), forward.sort_index())

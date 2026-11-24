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


def test_dow_features_are_the_weekday_encoded(ledger):
    """dow_sin and dow_cos put the entry's weekday (Monday 0, from its first line's entered_at)
    on the unit circle. They are the model's only view of the day, so their values must not
    move: this pins them to datetime.weekday, not to the pandas accessor that computes them."""
    from ledgerlens.ingest import entry_level

    X = build_features(ledger)
    weekday = entry_level(ledger).set_index("entry_id")["entered_at"].map(lambda t: t.weekday())
    radians = 2 * np.pi * weekday.reindex(X.index).to_numpy() / 7
    assert np.allclose(X["dow_sin"].to_numpy(), np.sin(radians), rtol=0, atol=1e-12)
    assert np.allclose(X["dow_cos"].to_numpy(), np.cos(radians), rtol=0, atol=1e-12)


def test_deterministic(ledger):
    a = build_features(ledger)
    b = build_features(ledger)
    assert a.equals(b)


def test_frequency_features_are_proportions(ledger):
    X = build_features(ledger)
    for col in ("account_pair_freq", "user_freq", "source_freq"):
        assert X[col].between(0, 1).all()


def _ledger(lines):
    """A tiny prepared ledger from (entry id, line number, account, debit, credit) rows."""
    return prepare(pd.DataFrame([{
        "entry_id": entry_id, "line_no": line_no,
        "posting_date": pd.Timestamp("2024-03-15"),
        "entered_at": pd.Timestamp("2024-03-15 10:00"),
        "fiscal_year": 2024, "period": 3,
        "account_code": account, "account_name": "Account " + account,
        "account_type": "Asset", "description": "line", "debit": debit,
        "credit": credit, "source": "Manual", "created_by": "u1",
    } for entry_id, line_no, account, debit, credit in lines]))


def _two_line_entries(rows):
    """(entry id, debit account, credit account, amount) rows as two lines each, the debit
    line first."""
    return [line for entry_id, debit_account, credit_account, amount in rows
            for line in ((entry_id, 1, debit_account, amount, 0.0),
                         (entry_id, 2, credit_account, 0.0, amount))]


def test_the_entry_account_is_the_first_of_its_largest_lines(ledger):
    """Nearly every entry in the default ledger is two lines of equal amount, so "the largest
    line's account" is a tie. It goes to the first line of the entry, and only the line
    numbers say which that is.
    (The default sort is not stable: left to it, which line came first differed between a Mac
    and the x86-64 Linux runners, and the README's model-tier figures moved with it.)"""
    rows = [("E1", "1000", "2000", 100.0),
            ("E2", "1000", "9000", 10.0), ("E3", "1000", "9000", 1000.0),
            ("E4", "9000", "2000", 10000.0), ("E5", "9000", "2000", 100000.0)]
    # account 1000's log amounts are 1, 2, 3 (median 2, MAD 1); account 2000's are 2, 4, 5
    # (median 4, MAD 1). E1 is 100, log 2: a z of 0 under 1000 and of -1.349 under 2000.
    tiny = _ledger(_two_line_entries(rows))
    assert build_features(tiny).loc["E1", "amount_z_in_account"] == pytest.approx(0.0)
    swapped = tiny.copy()
    is_e1 = swapped["entry_id"] == "E1"
    swapped.loc[is_e1, "line_no"] = swapped.loc[is_e1, "line_no"].to_numpy()[::-1]
    z = build_features(swapped).loc["E1", "amount_z_in_account"]
    assert z == pytest.approx(0.6745 * (2 - 4) / 1)
    # the amount decides before the line number does: E6's largest line is its third, in
    # account 2000 (log amounts 1, 2, 4, 5: median 3, MAD 1.5, so its 100 is a z of
    # -0.450), and its first line is in an account seen nowhere else (no spread: a z of 0)
    split = _ledger([("E6", 1, "7000", 40.0, 0.0), ("E6", 2, "8000", 60.0, 0.0),
                     ("E6", 3, "2000", 0.0, 100.0)]
                    + _two_line_entries([("G1", "1000", "2000", 10.0),
                                         ("G2", "1000", "2000", 10000.0),
                                         ("G3", "1000", "2000", 100000.0)]))
    z = build_features(split).loc["E6", "amount_z_in_account"]
    assert z == pytest.approx(0.6745 * (2 - 3) / 1.5)
    # and the order the rows arrive in changes nothing, on the real ledger either (its line
    # numbers are unique within an entry; where they are not, the file's order decides)
    forward = build_features(ledger)["amount_z_in_account"]
    backward = build_features(ledger.iloc[::-1])["amount_z_in_account"]
    pd.testing.assert_series_equal(backward.sort_index(), forward.sort_index())


def _entropy_of(amount):
    """The digit entropy as it is defined: Shannon entropy, in bits, of an amount's digits."""
    digits = [c for c in f"{abs(amount):.2f}" if c.isdigit()]
    if not digits:
        return 0.0
    share = pd.Series(digits).value_counts(normalize=True)
    return float(-(share * np.log2(share)).sum())


def test_digit_entropy_is_each_amounts_own_entropy_to_the_last_bit():
    """The entropy is worked out once for each pattern of digit counts and looked up after
    that. Every amount must still get exactly what its own digits give, to the last bit of
    the float: the model reads this column, and a difference in the last place can move a
    score. Amounts that share a pattern with other digits (5,555.00 and 7,777.00), share a
    length but not a pattern (1,111.11 and 1,234.56), or have no digits at all are here."""
    from ledgerlens.features import _digit_entropy

    amounts = pd.Series([
        5555.00, 7777.00, 5500.55, 1111.11, 1234.56, 6543.21, 1122.33, 9.99, 0.00, -0.0, 100000.00,
        -5555.00, 0.004, 0.005, 1234567890.12, 99999999999.99, 1e15, 0.1 + 0.2, 12.3, 123.456,
        float("nan"), float("inf"), float("-inf"), 1.0, 10.0, 100.0, 1000.0, 7.0, 77.0, 777.77,
    ])
    got = _digit_entropy(amounts).to_numpy(dtype="float64")
    want = np.array([_entropy_of(a) for a in amounts], dtype="float64")
    assert np.array_equal(got.view(np.int64), want.view(np.int64))
    assert got[0] == got[1] == got[2] != got[3]  # one pattern, three amounts; another pattern
    assert got[3] < got[4] and got[4] == got[5]  # six distinct digits carry more than one repeated
    assert got[20] == got[21] == got[22] == 0.0  # no digits: nothing to measure


def test_digit_entropy_asks_pandas_once_for_each_pattern_not_once_for_each_amount(monkeypatch):
    """One pandas call for every amount made this the slowest step of the model tier: nearly
    half of `score`'s time on a ledger of 100,000 entries. The patterns are few (37 on that
    ledger), so the calls must be too. Counted, not timed."""
    from ledgerlens.features import _digit_entropy

    calls = []
    real = pd.Series.value_counts

    def counted(self, *args, **kwargs):
        calls.append(len(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pd.Series, "value_counts", counted)
    amounts = pd.Series([round(1000 + 0.37 * i, 2) for i in range(3000)])
    result = _digit_entropy(amounts)
    assert len(result) == 3000 and result.nunique() == len(calls)  # one call for each distinct pattern
    assert len(calls) < 60

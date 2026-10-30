import numpy as np
import pandas as pd

from ledgerlens.benford import (
    EXPECTED_FIRST_DIGIT,
    MIN_SAMPLE,
    SMALL_SAMPLE_NOTE,
    benford_test,
    first_digit,
    mad_conformity,
    segmented_benford,
)

BANDS = ("close conformity", "acceptable conformity", "marginal conformity", "nonconformity")


def _ones(count):
    """Amounts that all lead with a 1: nonconformity at any size, so a band is easy to see."""
    return [1.0 + i / 1000 for i in range(count)]


def test_expected_frequencies_match_the_law():
    # Benford's canonical values: 30.1% for 1, 4.6% for 9.
    assert round(EXPECTED_FIRST_DIGIT[1], 3) == 0.301
    assert round(EXPECTED_FIRST_DIGIT[9], 3) == 0.046
    assert abs(sum(EXPECTED_FIRST_DIGIT.values()) - 1.0) < 1e-9


def test_first_digit_handles_scale_and_sign():
    values = pd.Series([1234, 0.0456, 9.9, 700, -8200])
    assert list(first_digit(values)) == [1, 4, 9, 7, 8]


def test_first_digit_drops_zeros_and_nulls():
    values = pd.Series([0, None, 5.5])
    assert list(first_digit(values)) == [5]


def test_lognormal_data_conforms():
    rng = np.random.default_rng(7)
    result = benford_test(pd.Series(rng.lognormal(6.2, 1.1, 5000)))
    assert result["conformity"] in ("close conformity", "acceptable conformity")


def test_fabricated_data_does_not_conform():
    rng = np.random.default_rng(7)
    # Numbers a person invented, all starting 5-9.
    result = benford_test(pd.Series(rng.uniform(5000, 9999, 5000)))
    assert result["conformity"] == "nonconformity"
    assert result["mad"] > 0.015


def test_small_samples_are_marked_insufficient():
    result = benford_test(pd.Series([123.0, 456.0, 789.0]))
    assert result["sufficient_sample"] is False


def test_empty_input_does_not_raise():
    result = benford_test(pd.Series([], dtype="float64"))
    assert result["n"] == 0
    assert result["conformity"] == "insufficient data"


def test_conformity_bands():
    assert mad_conformity(0.004) == "close conformity"
    assert mad_conformity(0.010) == "acceptable conformity"
    assert mad_conformity(0.014) == "marginal conformity"
    assert mad_conformity(0.400) == "nonconformity"


def test_segmenting_skips_thin_segments(ledger):
    result = segmented_benford(ledger, by="account_code", min_n=300)
    assert (result["n"] >= 300).all()


def test_segmenting_returns_empty_frame_when_nothing_qualifies(ledger):
    result = segmented_benford(ledger, by="account_code", min_n=10**9)
    assert result.empty


def test_segments_tied_on_mad_print_in_label_order():
    """Twenty segments of the same amounts tie on MAD, and one more, last by name, stands above
    them. A one-key sort keeps tied rows in input order up to 16 rows and scrambles them from
    17; the label breaks the tie, and the odd segment shows MAD is still the first key."""
    amounts = [float(v) for v in range(1, 31)]
    labels = [f"s{i:02d}" for i in range(20)]
    df = pd.DataFrame({
        "account_code": [label for label in labels for _ in amounts] + ["zz"] * len(amounts),
        "abs_amount": amounts * len(labels) + [9.0] * len(amounts),
    })
    out = segmented_benford(df, by="account_code", min_n=len(amounts))
    assert out["mad"].nunique() == 2 and out["mad"].iloc[0] > out["mad"].iloc[1]
    assert out["label"].tolist() == ["zz"] + labels


def test_no_band_below_the_floor_and_one_at_it():
    """It gave a band for any n from 1: 297 amounts of a sandbox pull read "nonconformity" on the
    dashboard. The statistics are still computed below the floor; only the reading is withheld."""
    full = benford_test(pd.Series(_ones(MIN_SAMPLE)))
    short = benford_test(pd.Series(_ones(MIN_SAMPLE - 1)))
    assert full["n"] == MIN_SAMPLE and full["conformity"] == "nonconformity"
    assert short["n"] == MIN_SAMPLE - 1 and short["conformity"] == "insufficient data"
    assert short["sufficient_sample"] is False and full["sufficient_sample"] is True
    assert short["mad"] == full["mad"] > 0.015 and short["chi_square"] > 15.507


def test_the_floor_counts_amounts_not_rows():
    """A zero has no leading digit: 300 values with one zero among them are 299 amounts."""
    result = benford_test(pd.Series([0.0] + _ones(MIN_SAMPLE - 1)))
    assert result["n"] == MIN_SAMPLE - 1 and result["conformity"] == "insufficient data"
    assert result["sufficient_sample"] is False  # what every surface keys its note on


def test_a_segment_below_the_floor_gets_no_band_however_it_was_kept():
    """Segments are kept by their rows (``min_n``), so a segment of 300 rows with a zero, or any
    segment once ``min_n`` is lowered, can hold fewer than 300 amounts: no band for those."""
    df = pd.DataFrame({
        "account_code": ["A"] * MIN_SAMPLE + ["B"] * MIN_SAMPLE + ["C"] * 50,
        "abs_amount": [0.0] + _ones(MIN_SAMPLE - 1) + _ones(MIN_SAMPLE) + _ones(50),
    })
    rows = segmented_benford(df, by="account_code").set_index("label")
    assert rows.loc["A", "n"] == MIN_SAMPLE - 1 and rows.loc["A", "conformity"] == "insufficient data"
    assert rows.loc["B", "conformity"] == "nonconformity" and "C" not in rows.index
    rows = segmented_benford(df, by="account_code", min_n=50).set_index("label")
    assert rows.loc["C", "conformity"] == "insufficient data"
    assert rows.loc["B", "conformity"] == "nonconformity"


def test_the_small_sample_note_names_the_floor_and_is_ascii():
    assert SMALL_SAMPLE_NOTE.isascii() and str(MIN_SAMPLE) in SMALL_SAMPLE_NOTE
    assert not any(band in SMALL_SAMPLE_NOTE for band in BANDS)

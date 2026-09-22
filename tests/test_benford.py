import numpy as np
import pandas as pd

from ledgerlens.benford import (
    EXPECTED_FIRST_DIGIT,
    benford_test,
    first_digit,
    mad_conformity,
    segmented_benford,
)


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

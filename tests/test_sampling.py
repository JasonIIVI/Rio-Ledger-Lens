"""Materiality, and the two ways of selecting entries against it.

Kept ASCII like summary's tests: the memo's inert rendering is summary's, and the
characters it must keep inert are built here with chr().
"""

import hashlib
import json
import math
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ledgerlens import sampling
from ledgerlens.sampling import (
    Materiality,
    SamplingError,
    check_parameters,
    collect_memo,
    mus_select,
    population,
    render_memo,
    stratified_select,
)

TODAY = date(2026, 10, 22)


def _ledger(amounts, ids=None, description="entry", years=None):
    """A ledger of two-line entries, entry i of ``amounts[i]`` dollars, as entry_level reads one."""
    rows = []
    for i, amount in enumerate(amounts):
        entry = ids[i] if ids else f"JE-{i:05d}"
        year = years[i] if years else 2024
        for line, (debit, credit) in enumerate(((amount, 0.0), (0.0, amount)), start=1):
            rows.append({
                "entry_id": entry, "line_no": line, "posting_date": pd.Timestamp(f"{year}-01-02"),
                "entered_at": pd.Timestamp(f"{year}-01-02 10:00"), "fiscal_year": year, "period": 1,
                "source": "AP", "created_by": "u1", "description": f"{description} {i}",
                "debit": debit, "credit": credit, "entered_hour": 10,
                "is_weekend": False, "is_holiday": False,
            })
    return pd.DataFrame(rows)


# --- materiality ---------------------------------------------------------------------

def test_materiality_derives_performance_and_clearly_trivial_from_overall():
    m = Materiality("50000")
    assert (m.performance, m.clearly_trivial) == (Decimal("37500.00"), Decimal("2500.00"))
    assert (m.overall_cents, m.performance_cents, m.trivial_cents) == (5_000_000, 3_750_000, 250_000)
    assert m.describe() == ("$50,000.00 overall; performance $37,500.00 (75%); "
                            "clearly trivial $2,500.00 (5%)")
    assert Materiality(50000, "0.6", "0.03").describe() == (
        "$50,000.00 overall; performance $30,000.00 (60%); clearly trivial $1,500.00 (3%)")


def test_derived_figures_are_rounded_down_to_the_cent():
    """The lower figure is the cautious one: a larger sample, more entries examined in full."""
    m = Materiality("1000.01", "0.75", "0.05")
    assert (m.performance, m.clearly_trivial) == (Decimal("750.00"), Decimal("50.00"))
    assert Materiality("0.99", "0.5", "0.02").performance == Decimal("0.49")


def test_materiality_is_read_as_written_not_through_a_float():
    assert Materiality("0.1", "0.9", "0.5").overall == Decimal("0.1")
    assert Materiality(0.1, 0.9, 0.5).overall == Decimal("0.1")  # a float by its repr


def test_numbers_built_by_numpy_are_read_by_their_value():
    """numpy 2 writes a float64's repr as np.float64(...), which Decimal cannot read."""
    m = Materiality(np.float64(50000.0), np.float64(0.75), np.float64(0.05))
    assert (m.overall, m.performance) == (Decimal("50000.0"), Decimal("37500.00"))
    assert Materiality(np.int64(50000)).overall == Decimal(50000)
    s = mus_select(_ledger([10_000.0] * 4), m, confidence=np.float64(0.95), seed=np.int64(3))
    assert s.seed == 3 and s.parameters["confidence"] == 0.95


@pytest.mark.parametrize("args, said", [
    (("0",), "materiality must be above 0"),
    (("-5",), "materiality must be above 0"),
    (("abc",), "materiality must be a number, got 'abc'"),
    (("50,000",), 'write 50000, not "50,000"'),
    (("nan",), "materiality must be a finite number"),
    (("Infinity",), "materiality must be a finite number"),
    ((True,), "materiality must be a number, got True"),
    ((None,), "materiality must be a number, got None"),
    (("1e12",), "is not below 1,000,000,000,000"),
    (("10.001",), "is not a whole number of cents"),
    (("1" * 41,), "has more digits than it needs"),
    (("50000", "0"), "performance share must be above 0 and below 1"),
    (("50000", "1"), "performance share must be above 0 and below 1"),
    (("50000", "1.5"), "performance share must be above 0 and below 1"),
    (("50000", "0.7500001"), "performance share must be given to at most six decimal places"),
    (("50000", "0.75", "0"), "clearly trivial share must be above 0 and below 1"),
    (("50000", "0.75", "1E-999999999"), "clearly trivial share must be given to at most six decimal places"),
    (("50000", "0.75", "0.75"), "clearly trivial share (0.75) must be below the performance share"),
    (("0.01",), "performance materiality (75% of 0.01) is less than a cent"),
    (("0.02",), "clearly trivial (5% of 0.02) is less than a cent"),
])
def test_materiality_that_cannot_be_used_is_refused_with_its_reason(args, said):
    with pytest.raises(SamplingError) as caught:
        Materiality(*args)
    assert said in str(caught.value)


def test_every_problem_with_materiality_is_named_at_once():
    with pytest.raises(SamplingError) as caught:
        Materiality("-1", "2", "0")
    text = str(caught.value)
    assert "materiality must be above 0" in text
    assert "performance share must be above 0 and below 1" in text
    assert "clearly trivial share must be above 0 and below 1" in text


def test_a_bad_performance_share_is_not_compared_with_the_trivial_one():
    with pytest.raises(SamplingError) as caught:
        Materiality("50000", "x", "0.9")
    assert "must be below the performance share" not in str(caught.value)


# --- the population ------------------------------------------------------------------

def test_the_population_is_in_entry_id_order_and_counts_what_it_leaves_out():
    df = _ledger([300.0, 0.0, 100.25, 200.0, -50.0], ids=["JE-C", "JE-Z", "JE-A", "JE-B", "JE-N"])
    entries, facts = population(df.sample(frac=1, random_state=5))
    assert list(entries["entry_id"]) == ["JE-A", "JE-B", "JE-C"]
    assert list(entries["cents"]) == [10025, 20000, 30000]
    assert facts == {"fiscal_year": 2024, "entries_in_year": 5, "sampled_from": 3,
                     "left_out_no_amount": 1, "left_out_negative": 1, "other_years": 0,
                     "book_value": 600.25}


def test_amounts_are_rounded_to_the_cent_not_cut():
    """0.29 * 100 is 28.999999999999996 as a float."""
    entries, _ = population(_ledger([0.29, 1.15]))
    assert list(entries["cents"]) == [29, 115]


def test_an_entrys_description_is_its_first_lines_whatever_the_row_order():
    df = _ledger([100.0, 200.0])
    df["description"] = [f"{side} side {i // 2}" for i, side in enumerate(["debit", "credit"] * 2)]
    entries, _ = population(df.iloc[::-1])
    assert list(entries["description"]) == ["debit side 0", "debit side 1"]


def test_a_ledger_of_several_fiscal_years_needs_the_year_named():
    df = _ledger([100.0, 200.0, 300.0], years=[2024, 2025, 2025])
    with pytest.raises(SamplingError) as caught:
        population(df)
    assert "spans fiscal years 2024, 2025" in str(caught.value) and "--fiscal-year" in str(caught.value)
    entries, facts = population(df, fiscal_year=2025)
    assert list(entries["cents"]) == [20000, 30000]
    assert (facts["fiscal_year"], facts["entries_in_year"], facts["other_years"]) == (2025, 2, 1)
    with pytest.raises(SamplingError, match=r"no entry in fiscal year 2023 \(it holds 2024, 2025\)"):
        population(df, fiscal_year="2023")
    with pytest.raises(SamplingError, match="fiscal year must be a whole number of 1 or more"):
        population(df, fiscal_year="next")


# --- monetary unit sampling ----------------------------------------------------------

def test_mus_size_and_interval_are_the_hand_computed_ones():
    """$1,000,000 of book value, performance $37,500, 95%: n = ceil(1e6 x 2.995732 / 37,500) = 80."""
    s = mus_select(_ledger([10_000.0] * 100), Materiality("50000"), seed=3)
    assert s.parameters["reliability_factor"] == round(-math.log(0.05), 4) == 2.9957
    assert (s.parameters["size"], s.parameters["interval"]) == (80, 12_500.0)
    assert 0 <= s.parameters["start"] < 12_500
    assert s.items["hits"].tolist() == [1] * 80 and len(s.items) == 80  # each $10,000 holds at most one
    assert s.parameters["certain"] == 0


def test_mus_selects_every_entry_at_or_above_the_interval_whatever_the_seed():
    amounts = [100_000.0, 25_000.0, 12_500.0, 13_000.0] + [737.0 + i for i in range(200)]
    df = _ledger(amounts)
    for seed in range(40):
        s = mus_select(df, Materiality("50000"), seed=seed)
        interval = s.parameters["interval"]
        items = s.items.set_index("entry_id")
        for i, amount in enumerate(amounts):
            if amount >= interval:
                assert f"JE-{i:05d}" in items.index, (seed, amount)
        for _, row in items.iterrows():
            assert math.floor(row.amount / interval) <= row.hits <= math.ceil(row.amount / interval)
        assert items["hits"].sum() == s.parameters["size"]
        assert s.parameters["certain"] == sum(a >= interval for a in amounts)


def test_an_entry_of_exactly_the_interval_is_selected_whatever_the_start():
    """$1,000,000 in 80 units: the interval is exactly $12,500, the first entry's amount."""
    df = _ledger([12_500.0] + [10_000.0] * 98 + [7_500.0])
    for seed in range(20):
        s = mus_select(df, Materiality("50000"), seed=seed)
        assert s.parameters["interval"] == 12_500.0
        first = s.items.set_index("entry_id").loc["JE-00000"]
        assert first.hits == 1
        assert first.why == "at or above the sampling interval, so selected whatever the start"
        assert s.parameters["certain"] == 1  # an entry of exactly the interval counts


def test_a_selected_dollar_on_a_boundary_belongs_to_the_entry_that_begins_there(monkeypatch):
    """A start of exactly 0 puts every selected dollar on a boundary: $1,000,000 in 80
    entries of one interval each, so each entry holds exactly one."""

    class Zero:
        def __init__(self, seed):
            pass

        def random(self):
            return 0.0

    monkeypatch.setattr(sampling.random, "Random", Zero)
    s = mus_select(_ledger([12_500.0] * 80), Materiality("50000"), seed=1)
    assert s.parameters["start"] == 0.0 and len(s.items) == 80
    assert s.items["hits"].tolist() == [1] * 80
    assert s.items["first_unit"].tolist() == [12_500.0 * k for k in range(80)]


def test_the_first_unit_is_where_an_entry_is_first_hit_in_the_layout():
    """Cumulative, laid end to end, and floored to the cent: within one interval of the
    entry's start, inside the entry."""
    amounts = [100_000.0, 31.0, 55_000.0] + [600.0 + i for i in range(300)]
    s = mus_select(_ledger(amounts), Materiality("50000"), seed=5)
    interval = s.parameters["interval"]
    starts = dict(zip((f"JE-{i:05d}" for i in range(len(amounts))), _before(amounts)))
    for row in s.items.itertuples():
        low = starts[row.entry_id]
        assert low <= row.first_unit < low + row.amount and row.first_unit - low < interval
        assert abs(row.first_unit * 100 - round(row.first_unit * 100)) < 1e-6  # floored to the cent
    big = s.items.set_index("entry_id").loc["JE-00000"]
    assert big.hits > 1 and big.first_unit <= s.parameters["start"] < big.first_unit + 0.01


def _before(amounts):
    """Where each entry starts when the entries are laid end to end, in dollars."""
    total, out = 0.0, []
    for amount in amounts:
        out.append(total)
        total += amount
    return out


def test_mus_favours_larger_entries_as_intended():
    """Each entry is selected with a chance of about its amount / the interval."""
    df = _ledger([1_000.0] * 50 + [5_000.0] * 50)
    small = big = 0
    for seed in range(200):
        ids = set(mus_select(df, Materiality("50000"), seed=seed).items["entry_id"])
        small += sum(f"JE-{i:05d}" in ids for i in range(50))
        big += sum(f"JE-{i:05d}" in ids for i in range(50, 100))
    assert big > 3 * small > 0


def test_a_sample_of_more_units_than_entries_is_drawn_and_counted():
    """The units fall where the dollars are: one $1,000,000 entry holds nearly all of them."""
    s = mus_select(_ledger([1_000_000.0] + [100.0] * 50), Materiality("50000"), seed=2)
    assert s.parameters["size"] == 81 and s.items["hits"].sum() == 81
    items = s.items.set_index("entry_id")
    assert items.loc["JE-00000", "hits"] >= 80 and len(items) <= 2 and s.parameters["certain"] == 1
    every = mus_select(_ledger([100.0] * 3), Materiality("1"), seed=2)  # 1,199 units, $0.25 apart
    assert every.parameters["size"] == 1199 and len(every.items) == 3 and every.parameters["certain"] == 3


def test_a_confidence_whose_factor_rounds_to_zero_still_selects_one_unit():
    s = mus_select(_ledger([100.0] * 10), Materiality("50000"), confidence="1e-30", seed=1)
    assert (s.parameters["size"], len(s.items)) == (1, 1)


def test_mus_gives_one_selection_for_one_seed_and_any_row_order(ledger):
    m = Materiality("50000")
    first = mus_select(ledger, m, seed=11, fiscal_year=2025)
    again = mus_select(ledger.sample(frac=1, random_state=2), m, seed="11", fiscal_year=2025)
    pd.testing.assert_frame_equal(first.items, again.items)
    others = [set(mus_select(ledger, m, seed=seed, fiscal_year=2025).items["entry_id"]) for seed in (12, 13)]
    assert any(ids != set(first.items["entry_id"]) for ids in others)


def test_a_drawn_seed_is_recorded_and_repeats_the_selection(ledger):
    m = Materiality("50000")
    drawn = mus_select(ledger, m, fiscal_year=2025)
    assert drawn.seed_drawn and isinstance(drawn.seed, int)
    again = mus_select(ledger, m, seed=drawn.seed, fiscal_year=2025)
    assert not again.seed_drawn
    pd.testing.assert_frame_equal(drawn.items, again.items)
    seeds = {mus_select(_ledger([100.0]), m).seed for _ in range(4)}
    assert len(seeds) > 1  # drawn from 2**32, not fixed


def test_mus_on_the_default_ledger_selects_the_same_entries_on_every_python(ledger):
    """Pinned: only Random.random() is drawn and every figure is exact, so 3.9, 3.12 and
    the Linux runners select the same entries."""
    s = mus_select(ledger, Materiality("50000"), seed=7, fiscal_year=2025)
    p = s.parameters
    assert (p["size"], p["entries_selected"], p["certain"], p["certain_from"]) == (193, 185, 7, 12_484.63)
    assert s.population["book_value"] == 2_409_533.22
    digest = hashlib.sha256(",".join(s.items["entry_id"]).encode()).hexdigest()
    assert digest[:16] == "ab3df2681609f236"


@pytest.mark.parametrize("confidence", [0, 1, "1.5", "-0.1", "x", "nan"])
def test_a_confidence_outside_zero_and_one_is_refused(confidence):
    with pytest.raises(SamplingError, match="confidence"):
        mus_select(_ledger([100.0]), Materiality("50000"), confidence=confidence, seed=1)


@pytest.mark.parametrize("seed", [-1, True, "x", "3.5", 1.5, ""])
def test_a_seed_that_is_not_a_whole_number_is_refused(seed):
    with pytest.raises(SamplingError, match="seed must be a whole number of 0 or more"):
        mus_select(_ledger([100.0]), Materiality("50000"), seed=seed)


def test_a_ledger_with_nothing_to_select_is_refused():
    with pytest.raises(SamplingError, match="no entry with a positive amount to select in fiscal year 2024"):
        mus_select(_ledger([0.0, -5.0]), Materiality("50000"), seed=1)
    with pytest.raises(SamplingError, match="no entry with a positive amount"):
        stratified_select(_ledger([0.0]), Materiality("50000"), size=1, seed=1)


@pytest.mark.parametrize("method, kwargs, said", [
    ("mus", {"confidence": "1"}, "confidence must be above 0 and below 1"),
    ("mus", {"seed": "-3"}, "seed must be a whole number of 0 or more"),
    ("mus", {"fiscal_year": "x"}, "fiscal year must be a whole number of 1 or more"),
    ("stratified", {"size": "0"}, "size must be a whole number of 1 or more"),
    ("stratified", {"size": "4", "strata": "21"}, "strata must be a whole number from 1 to 20"),
])
def test_parameters_are_checked_before_any_ledger_is_read(method, kwargs, said):
    with pytest.raises(SamplingError, match=said):
        check_parameters(method, **kwargs)
    check_parameters("mus")
    check_parameters("stratified", size="4", strata="3", seed="7", fiscal_year="2025")


# --- stratified random sampling ------------------------------------------------------

def test_allocation_follows_value_by_largest_remainder_and_never_more_than_a_band_holds():
    assert sampling._allocate(10, [600, 300, 100], [50, 50, 50]) == [6, 3, 1]
    assert sampling._allocate(2, [500, 300, 200], [50, 50, 50]) == [1, 1, 0]  # 1.0, 0.6, 0.4
    assert sampling._allocate(3, [980, 10, 10], [50, 50, 50]) == [3, 0, 0]    # 2.94, 0.03, 0.03
    assert sampling._allocate(6, [900, 50, 50], [2, 10, 10]) == [2, 2, 2]     # capped, the rest shared
    assert sampling._allocate(2, [100, 100, 0], [5, 5, 0]) == [1, 1, 0]       # an empty band gets none
    assert sampling._allocate(1, [100, 100], [5, 5]) == [1, 0]                # a tie goes to the lower band
    assert sampling._allocate(7, [5, 3, 2], [1, 3, 9]) == [1, 3, 3]           # two caps, in turn


def test_stratified_examines_everything_at_performance_and_draws_from_everything_below(ledger):
    m = Materiality("50000")
    s = stratified_select(ledger, m, size=40, seed=7, fiscal_year=2025)
    entries, _ = population(ledger, 2025)
    full = set(entries.loc[entries["cents"] >= m.performance_cents, "entry_id"])
    items = s.items.set_index("entry_id")
    assert full and full == set(items.index[items["band"] == "full"])
    drawn = items[items["band"] != "full"]
    assert len(drawn) == 40 and (drawn["amount"] < float(m.performance)).all()
    bands = s.parameters["bands"]
    assert sum(b["drawn"] for b in bands) == 40
    assert sum(b["entries"] for b in bands) == s.parameters["drawn_from"] == len(entries) - len(full)
    # near-equal value: no band is further from an equal share than one entry's amount
    share = s.parameters["drawn_from_value"] / len(bands)
    largest = entries.loc[entries["cents"] < m.performance_cents, "cents"].max() / 100
    assert all(abs(b["value"] - share) <= largest for b in bands)
    # clearly trivial sets nothing aside: the smallest entries are in the first band
    assert bands[0]["from"] == entries["cents"].min() / 100


def test_an_entry_of_exactly_performance_materiality_is_examined_in_full():
    s = stratified_select(_ledger([37_500.0, 37_499.99, 100.0, 200.0]), Materiality("50000"),
                          size=1, seed=1)
    items = s.items.set_index("entry_id")
    assert items.loc["JE-00000", "band"] == "full"
    assert s.parameters["examined_in_full"] == 1 and s.parameters["drawn_from"] == 3


def test_a_size_of_everything_below_performance_draws_all_of_it():
    s = stratified_select(_ledger([100.0, 200.0, 300.0, 40_000.0]), Materiality("50000"),
                          size="3", seed=1)
    assert len(s.items) == 4 and sorted(s.items["band"]) == ["1", "1", "2", "full"]  # $100 and $200 share a band


def test_stratified_gives_one_selection_for_one_seed_and_any_row_order(ledger):
    m = Materiality("50000")
    first = stratified_select(ledger, m, size=25, seed=4, fiscal_year=2025)
    again = stratified_select(ledger.sample(frac=1, random_state=9), m, size=25, seed=4, fiscal_year=2025)
    pd.testing.assert_frame_equal(first.items, again.items)
    other = stratified_select(ledger, m, size=25, seed=5, fiscal_year=2025)
    assert set(other.items["entry_id"]) != set(first.items["entry_id"])


def test_stratified_on_the_default_ledger_selects_the_same_entries_on_every_python(ledger):
    s = stratified_select(ledger, Materiality("50000"), size=40, seed=7, fiscal_year=2025)
    bands = s.parameters["bands"]
    assert [b["entries"] for b in bands] == [1904, 473, 149]  # about $762,000 of value each
    assert [b["drawn"] for b in bands] == [14, 13, 13]
    assert s.parameters["examined_in_full"] == 2
    digest = hashlib.sha256(",".join(s.items["entry_id"]).encode()).hexdigest()
    assert digest[:16] == "58e3ad82caf947b1"


@pytest.mark.parametrize("size, strata, said", [
    (0, 3, "size must be a whole number of 1 or more"),
    (True, 3, "size must be a whole number of 1 or more"),
    (2.0, 3, "size must be a whole number of 1 or more"),
    ("x", 3, "size must be a whole number of 1 or more"),
    (5, 0, "strata must be a whole number from 1 to 20"),
    (5, 21, "strata must be a whole number from 1 to 20"),
    (4, 3, "size 4 is more than the 3 entries below performance materiality ($37,500.00)"),
])
def test_stratified_parameters_that_cannot_be_used_are_refused(size, strata, said):
    df = _ledger([100.0, 200.0, 300.0, 40_000.0])
    with pytest.raises(SamplingError) as caught:
        stratified_select(df, Materiality("50000"), size=size, strata=strata, seed=1)
    assert said in str(caught.value)


# --- the memo ------------------------------------------------------------------------

def _memo(selection, fmt):
    payload = collect_memo(selection, ledger="data/ledger.csv", ledger_id="csv:abc", today=TODAY)
    return payload, render_memo(payload, fmt)


def test_the_memo_states_every_figure_the_selection_can_be_repeated_from():
    s = mus_select(_ledger([10_000.0] * 100), Materiality("50000"), seed=3)
    payload, text = _memo(s, "text")
    assert payload["caveat"] == sampling.SELECTION_CAVEAT
    assert text.splitlines()[0] == "LedgerLens selection memo, 2026-10-22"
    assert text.index(sampling.SELECTION_CAVEAT[:40]) < text.index("Materiality")
    for part in ("$50,000.00 overall; performance $37,500.00 (75%); clearly trivial $2,500.00 (5%)",
                 "monetary unit sampling", "3 (given)", "2.9957", "80 units",
                 "Sampling interval   $12,500.00", "Population   fiscal year 2024: 100 entries, $1,000,000.00",
                 "every entry of $12,500.00 or more is selected (0 here)"):
        assert part in text, part
    for note in (sampling.MATERIALITY_NOTE, sampling.POPULATION_NOTE, sampling.MUS_NOTE,
                 sampling.INPUT_NOTE):
        assert note in payload["notes"]
    assert sampling.NEGATIVE_NOTE not in payload["notes"]
    assert " ".join(text.split()).count(" ".join(sampling.MUS_NOTE.split())) == 1


def test_the_memo_says_what_the_population_leaves_out():
    df = _ledger([10_000.0] * 4 + [0.0, -20.0, 500.0], years=[2025] * 6 + [2024])
    payload, text = _memo(mus_select(df, Materiality("50000"), seed=1, fiscal_year=2025), "text")
    assert ("Population   fiscal year 2025: 4 entries, $40,000.00; 1 entry of no amount and 1 with a "
            "negative amount left out; 1 entry of other fiscal years not in it") in text
    assert sampling.NEGATIVE_NOTE in payload["notes"]


def test_the_interval_line_names_a_whole_cent_every_entry_of_which_is_selected():
    """$1,005,000 in 81 units: the interval is $12,407.4074..., so $12,407.41 and up is certain."""
    s = mus_select(_ledger([1_000_000.0] + [100.0] * 50), Materiality("50000"), seed=2)
    _, text = _memo(s, "text")
    assert "Sampling interval   $12,407.41" in text and "every entry of $12,407.41 or more" in text
    assert s.parameters["interval"] < 12_407.41 == s.parameters["certain_from"]


def test_a_drawn_seed_says_how_to_repeat_the_selection():
    s = mus_select(_ledger([10_000.0] * 10), Materiality("50000"))
    _, text = _memo(s, "text")
    assert f"{s.seed} (drawn; run again with --seed {s.seed} to repeat this selection)" in text


def test_the_json_memo_is_one_document_and_renders_the_same_twice():
    s = stratified_select(_ledger([100.0 * (i + 1) for i in range(30)] + [40_000.0]),
                          Materiality("50000"), size=5, seed=8)
    payload, text = _memo(s, "json")
    assert json.loads(text) == json.loads(json.dumps(payload))
    assert json.loads(text)["parameters"]["examined_in_full"] == 1
    assert _memo(s, "json")[1] == text and _memo(s, "markdown")[1] == _memo(s, "markdown")[1]
    _, markdown = _memo(s, "markdown")
    assert sampling.STRATIFIED_NOTE in markdown and "| Band 1 |" in markdown
    assert "| examined in full |" in markdown and "| full |" not in markdown


def test_bands_that_hold_no_entry_are_said_to():
    s = stratified_select(_ledger([1.0, 1.0, 30_000.0]), Materiality("50000"), size=1, seed=1)
    _, text = _memo(s, "text")
    assert "from 1 band of near-equal value (3 asked for, 2 holding no entry)" in text


def test_ledger_text_in_the_memo_is_inert():
    """Through summary's renderers: one line in text, a code span in markdown."""
    from ledgerlens.summary import md_code, one_line

    hostile = "Paid **" + "@" + "someone" + " | see [x](http://e.test)" + chr(10) + "next" + chr(0x202E)
    ids = [f"JE-{i} | @team" + chr(10) + "x" for i in range(4)]
    s = mus_select(_ledger([10_000.0] * 4, ids=ids, description=hostile), Materiality("50000"), seed=1)
    assert len(s.items) == 4  # one unit in each: every description is in the memo
    _, text = _memo(s, "text")
    _, markdown = _memo(s, "markdown")
    for i in range(4):
        assert one_line(f"{hostile} {i}") in text and one_line(ids[i]) in text
        assert md_code(f"{hostile} {i}") in markdown and md_code(ids[i]) in markdown
    assert chr(10) + "next" not in text and chr(0x202E) not in text + markdown
    assert "@someone" not in markdown and "@" + chr(0x200B) + "someone" in markdown
    assert "@team" not in markdown


def test_exact_arithmetic_matches_a_count_of_every_selected_dollar():
    """The bounds give the same hits as walking each selected dollar into its entry."""
    amounts = [3_000.0, 12_000.0, 47.5, 8_800.0, 25_000.0, 1.0] * 7
    s = mus_select(_ledger(amounts), Materiality("20000"), seed=9)
    interval = Fraction(round(sum(amounts) * 100), s.parameters["size"])
    start = Fraction(sampling.random.Random(9).random()) * interval
    walk = [0] * len(amounts)
    for k in range(s.parameters["size"]):
        point, low = start + k * interval, 0
        for i, amount in enumerate(amounts):
            if low <= point < low + round(amount * 100):
                walk[i] += 1
                break
            low += round(amount * 100)
    hits = dict(zip(s.items["entry_id"], s.items["hits"]))
    assert [hits.get(f"JE-{i:05d}", 0) for i in range(len(amounts))] == walk


def test_the_module_is_written_in_ascii():
    for path in (Path(sampling.__file__), Path(__file__)):
        assert path.read_text(encoding="utf-8").isascii(), path.name

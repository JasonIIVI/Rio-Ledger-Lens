"""Materiality, and the two ways of selecting entries against it.

Kept ASCII like summary's tests: the memo's inert rendering is summary's, and the
characters it must keep inert are built here with chr().
"""

import hashlib
import json
import math
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ledgerlens import sampling
from ledgerlens.sampling import (
    Materiality,
    SamplingError,
    collect_memo,
    mus_select,
    population,
    render_memo,
    stratified_select,
)

TODAY = date(2026, 10, 22)


def _ledger(amounts, ids=None, description="entry"):
    """A ledger of two-line entries, entry i of ``amounts[i]`` dollars, as entry_level reads one."""
    rows = []
    for i, amount in enumerate(amounts):
        entry = ids[i] if ids else f"JE-{i:05d}"
        for line, (debit, credit) in enumerate(((amount, 0.0), (0.0, amount)), start=1):
            rows.append({
                "entry_id": entry, "line_no": line, "posting_date": pd.Timestamp("2024-01-02"),
                "entered_at": pd.Timestamp("2024-01-02 10:00"), "fiscal_year": 2024, "period": 1,
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
    """The lower figure is the cautious one: a larger sample, fewer entries set aside."""
    m = Materiality("1000.01", "0.75", "0.05")
    assert (m.performance, m.clearly_trivial) == (Decimal("750.00"), Decimal("50.00"))
    assert Materiality("0.99", "0.5", "0.01").performance == Decimal("0.49")


def test_materiality_is_read_as_written_not_through_a_float():
    assert Materiality("0.1", "1", "0.5").overall == Decimal("0.1")
    assert Materiality(0.1, 1, 0.5).overall == Decimal("0.1")  # a float by its repr


@pytest.mark.parametrize("args, said", [
    (("0",), "materiality must be above 0"),
    (("-5",), "materiality must be above 0"),
    (("abc",), "materiality must be a number, got 'abc'"),
    (("50,000",), 'write 50000, not "50,000"'),
    (("nan",), "materiality must be a finite number"),
    (("Infinity",), "materiality must be a finite number"),
    ((True,), "materiality must be a number, got True"),
    (("1e12",), "is not below 1,000,000,000,000"),
    (("10.001",), "is not a whole number of cents"),
    (("1" * 41,), "has more digits than it needs"),
    (("50000", "0"), "performance share must be above 0 and at most 1"),
    (("50000", "1.5"), "performance share must be above 0 and at most 1"),
    (("50000", "0.75", "0"), "clearly trivial share must be above 0"),
    (("50000", "0.75", "0.75"), "clearly trivial share (0.75) must be below the performance share"),
    (("0.01",), "performance materiality (75% of 0.01) is less than a cent"),
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
    assert "performance share must be above 0 and at most 1" in text
    assert "clearly trivial share must be above 0" in text


def test_a_bad_performance_share_is_not_compared_with_the_trivial_one():
    with pytest.raises(SamplingError) as caught:
        Materiality("50000", "x", "0.9")
    assert "must be below the performance share" not in str(caught.value)


# --- the population ------------------------------------------------------------------

def test_the_population_is_in_entry_id_order_and_leaves_out_entries_of_no_amount():
    df = _ledger([300.0, 0.0, 100.25, 200.0], ids=["JE-C", "JE-Z", "JE-A", "JE-B"])
    entries, left_out = population(df.sample(frac=1, random_state=5))
    assert list(entries["entry_id"]) == ["JE-A", "JE-B", "JE-C"]
    assert list(entries["cents"]) == [10025, 20000, 30000]
    assert left_out == 1


# --- monetary unit sampling ----------------------------------------------------------

def test_mus_size_and_interval_are_the_hand_computed_ones():
    """$1,000,000 of book value, performance $37,500, 95%: n = ceil(1e6 x 2.995732 / 37,500) = 80."""
    s = mus_select(_ledger([10_000.0] * 100), Materiality("50000"), seed=3)
    assert s.parameters["reliability_factor"] == round(-math.log(0.05), 4) == 2.9957
    assert (s.parameters["size"], s.parameters["interval"]) == (80, 12_500.0)
    assert 0 <= s.parameters["start"] < 12_500
    assert s.items["hits"].tolist() == [1] * 80 and len(s.items) == 80  # each $10,000 holds at most one


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


def test_an_entry_of_exactly_the_interval_is_selected_whatever_the_start():
    """$1,000,000 in 80 units: the interval is exactly $12,500, the first entry's amount."""
    df = _ledger([12_500.0] + [10_000.0] * 98 + [7_500.0])
    for seed in range(20):
        s = mus_select(df, Materiality("50000"), seed=seed)
        assert s.parameters["interval"] == 12_500.0
        first = s.items.set_index("entry_id").loc["JE-00000"]
        assert first.hits == 1
        assert first.why == "at or above the sampling interval, so selected whatever the start"


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


def test_mus_refuses_only_a_sample_larger_than_the_population():
    """Three entries of $100: at $400 materiality (performance $300) the sample is 3 units,
    at $300 (performance $225) it is 4."""
    df = _ledger([100.0, 100.0, 100.0])
    assert mus_select(df, Materiality("400"), seed=1).parameters["size"] == 3
    with pytest.raises(SamplingError, match="the sample is 4 units, more than the ledger's 3 entries"):
        mus_select(df, Materiality("300"), seed=1)


def test_mus_favours_larger_entries_as_intended():
    """Each entry is selected with a chance of about its amount / the interval."""
    df = _ledger([1_000.0] * 50 + [5_000.0] * 50)
    small = big = 0
    for seed in range(200):
        ids = set(mus_select(df, Materiality("50000"), seed=seed).items["entry_id"])
        small += sum(f"JE-{i:05d}" in ids for i in range(50))
        big += sum(f"JE-{i:05d}" in ids for i in range(50, 100))
    assert big > 3 * small > 0


def test_mus_gives_one_selection_for_one_seed_and_any_row_order(ledger):
    m = Materiality("50000")
    first = mus_select(ledger, m, seed=11)
    again = mus_select(ledger.sample(frac=1, random_state=2), m, seed=11)
    pd.testing.assert_frame_equal(first.items, again.items)
    others = [set(mus_select(ledger, m, seed=seed).items["entry_id"]) for seed in (12, 13)]
    assert any(ids != set(first.items["entry_id"]) for ids in others)


def test_a_drawn_seed_is_recorded_and_repeats_the_selection(ledger):
    m = Materiality("50000")
    drawn = mus_select(ledger, m)
    assert drawn.seed_drawn and isinstance(drawn.seed, int)
    again = mus_select(ledger, m, seed=drawn.seed)
    assert not again.seed_drawn
    pd.testing.assert_frame_equal(drawn.items, again.items)


def test_mus_on_the_default_ledger_selects_the_same_entries_on_every_python(ledger):
    """Pinned: only Random.random() is drawn and every figure is exact, so 3.9, 3.12 and
    the Linux runners select the same entries."""
    s = mus_select(ledger, Materiality("50000"), seed=7)
    assert (s.parameters["size"], s.parameters["entries_selected"]) == (389, 368)
    digest = hashlib.sha256(",".join(s.items["entry_id"]).encode()).hexdigest()
    assert digest[:16] == "ff29dc6b69545733"


@pytest.mark.parametrize("confidence", [0, 1, "1.5", "-0.1", "x", "nan"])
def test_a_confidence_outside_zero_and_one_is_refused(confidence):
    with pytest.raises(SamplingError, match="confidence"):
        mus_select(_ledger([100.0]), Materiality("50000"), confidence=confidence, seed=1)


@pytest.mark.parametrize("seed", [-1, True, "3", 1.5])
def test_a_seed_that_is_not_a_whole_number_is_refused(seed):
    with pytest.raises(SamplingError, match="seed must be a whole number"):
        mus_select(_ledger([100.0]), Materiality("50000"), seed=seed)


def test_mus_refuses_a_sample_larger_than_the_population():
    with pytest.raises(SamplingError, match="more than the ledger's 3 entries"):
        mus_select(_ledger([100.0, 200.0, 300.0]), Materiality("10"), seed=1)


def test_a_ledger_with_nothing_to_select_is_refused():
    with pytest.raises(SamplingError, match="no entry with an amount"):
        mus_select(_ledger([0.0, 0.0]), Materiality("50000"), seed=1)
    with pytest.raises(SamplingError, match="no entry with an amount"):
        stratified_select(_ledger([0.0]), Materiality("50000"), size=1, seed=1)


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
    s = stratified_select(ledger, m, size=40, seed=7)
    entries, _ = population(ledger)
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


def test_stratified_gives_one_selection_for_one_seed_and_any_row_order(ledger):
    m = Materiality("50000")
    first = stratified_select(ledger, m, size=25, seed=4)
    again = stratified_select(ledger.sample(frac=1, random_state=9), m, size=25, seed=4)
    pd.testing.assert_frame_equal(first.items, again.items)
    other = stratified_select(ledger, m, size=25, seed=5)
    assert set(other.items["entry_id"]) != set(first.items["entry_id"])


def test_stratified_on_the_default_ledger_selects_the_same_entries_on_every_python(ledger):
    s = stratified_select(ledger, Materiality("50000"), size=40, seed=7)
    bands = s.parameters["bands"]
    assert [b["entries"] for b in bands] == [3811, 965, 305]  # about $1.53M of value each
    assert [b["drawn"] for b in bands] == [14, 13, 13]
    digest = hashlib.sha256(",".join(s.items["entry_id"]).encode()).hexdigest()
    assert digest[:16] == "dff7166332653e17"


@pytest.mark.parametrize("size, strata, said", [
    (0, 3, "size must be a whole number of 1 or more"),
    (True, 3, "size must be a whole number of 1 or more"),
    (2.0, 3, "size must be a whole number of 1 or more"),
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
                 "Sampling interval   $12,500.00", "100 entries, $1,000,000.00; fiscal years 2024"):
        assert part in text, part
    for note in (sampling.MATERIALITY_NOTE, sampling.MUS_NOTE, sampling.INPUT_NOTE):
        assert note in payload["notes"]
    assert " ".join(text.split()).count(" ".join(sampling.MUS_NOTE.split())) == 1


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


def test_ledger_text_in_the_memo_is_inert():
    """Through summary's renderers: one line in text, a code span in markdown."""
    from ledgerlens.summary import md_code, one_line

    hostile = "Paid **" + "@" + "someone" + " | see [x](http://e.test)" + chr(10) + "next" + chr(0x202E)
    s = mus_select(_ledger([10_000.0] * 4, description=hostile), Materiality("50000"), seed=1)
    assert len(s.items) == 4  # one unit in each: every description is in the memo
    _, text = _memo(s, "text")
    _, markdown = _memo(s, "markdown")
    for i in range(4):
        assert one_line(f"{hostile} {i}") in text
        assert md_code(f"{hostile} {i}") in markdown
    assert chr(10) + "next" not in text and chr(0x202E) not in text + markdown
    assert "@someone" not in markdown and "@" + chr(0x200B) + "someone" in markdown


def test_the_module_is_written_in_ascii():
    for path in (Path(sampling.__file__), Path(__file__)):
        assert path.read_text(encoding="utf-8").isascii(), path.name

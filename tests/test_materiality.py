"""Where an entry's amount stands against the materiality figures.

Kept ASCII like sampling's tests: the figures' text reaches the summary's inert rendering.
"""

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ledgerlens import materiality, sampling
from ledgerlens.ingest import entry_level
from ledgerlens.materiality import (
    BAND_NOTE,
    BANDS,
    FISCAL_YEAR_NOTE,
    OTHER_YEAR,
    Materiality,
    SamplingError,
    band_of_cents,
    bands,
    fiscal_year_for,
    to_cents,
)

M = Materiality("50000")  # 5,000,000 cents; performance 3,750,000; clearly trivial 250,000


def _entries(amounts, years=None):
    """One row per entry, as entry_level gives them: an amount and a fiscal year."""
    return pd.DataFrame({
        "entry_id": [f"JE-{i:05d}" for i in range(len(amounts))],
        "entry_amount": [float(a) for a in amounts],
        "fiscal_year": list(years) if years is not None else [2024] * len(amounts),
    })


def _ledger(amounts, years=None):
    """A ledger of two-line entries, entry i of amounts[i] dollars, as entry_level reads one."""
    rows = []
    for i, amount in enumerate(amounts):
        year = years[i] if years else 2024
        for line, (debit, credit) in enumerate(((amount, 0.0), (0.0, amount)), start=1):
            rows.append({
                "entry_id": f"JE-{i:05d}", "line_no": line, "posting_date": pd.Timestamp(f"{year}-01-02"),
                "entered_at": pd.Timestamp(f"{year}-01-02 10:00"), "fiscal_year": year, "period": 1,
                "source": "AP", "created_by": "u1", "description": f"entry {i}",
                "debit": debit, "credit": credit, "entered_hour": 10,
                "is_weekend": False, "is_holiday": False,
            })
    return pd.DataFrame(rows)


@pytest.mark.parametrize("cents, band", [
    (5_000_001, "material"), (5_000_000, "material"), (4_999_999, "above performance"),
    (3_750_001, "above performance"), (3_750_000, "above performance"),
    (3_749_999, "below performance"), (250_001, "below performance"),
    (250_000, "clearly trivial"), (249_999, "clearly trivial"), (1, "clearly trivial"),
    (0, "no amount"), (-1, "negative"), (-5_000_000, "negative"),
])
def test_a_band_is_at_or_above_each_figure_and_at_or_below_clearly_trivial(cents, band):
    assert band_of_cents(cents, M) == band
    assert band in BANDS


def test_bands_follow_each_entry_and_mark_the_other_years():
    frame = _entries([50000.0, 49999.99, 37500.0, 37499.99, 2500.01, 2500.0, 0.01, 0.0, -1.0],
                     years=[2024] * 8 + [2025])
    expected = ["material", "above performance", "above performance", "below performance",
                "below performance", "clearly trivial", "clearly trivial", "no amount", "negative"]
    assert list(bands(frame, M)) == expected  # no year named: every entry banded
    assert list(bands(frame, M, 2024)) == expected[:-1] + [OTHER_YEAR]
    assert list(bands(frame, M, 2025)) == [OTHER_YEAR] * 8 + ["negative"]
    assert bands(frame, M, 2024).index.equals(frame.index)
    assert list(bands(frame.iloc[:0], M, 2024)) == []


def test_a_text_fiscal_year_column_is_read_as_a_whole_number():
    frame = _entries([10.0, 20.0], years=["2024", "2025"])
    assert list(bands(frame, M, 2025)) == [OTHER_YEAR, "clearly trivial"]
    assert fiscal_year_for(frame, "2025") == 2025


def test_amounts_are_read_as_whole_cents_rounded_as_the_population_reads_them():
    assert list(to_cents(pd.Series([12.346, 7.004, 2500.0, 0.0, -1.0]))) == [1235, 700, 250000, 0, -100]
    ledger = _ledger([12.346, 7.004, 0.0, 2500.0])
    entries, _ = sampling.population(ledger)
    by_id = dict(zip(entries["entry_id"], entries["cents"]))
    level = entry_level(ledger)
    assert by_id == {entry: int(cents) for entry, cents
                     in zip(level["entry_id"], to_cents(level["entry_amount"])) if cents > 0}
    assert list(bands(level, M)) == ["clearly trivial", "clearly trivial", "no amount", "clearly trivial"]


def test_fiscal_year_for_refuses_in_the_populations_words():
    frame = _entries([10.0, 20.0], years=[2024, 2025])
    with pytest.raises(SamplingError) as several:
        fiscal_year_for(frame)
    assert "spans fiscal years 2024, 2025" in str(several.value) and "--fiscal-year" in str(several.value)
    with pytest.raises(SamplingError) as population_said:
        sampling.population(_ledger([10.0, 20.0], years=[2024, 2025]))
    assert str(population_said.value) == str(several.value)
    with pytest.raises(SamplingError) as missing:
        fiscal_year_for(frame, 2023)
    assert str(missing.value) == "the ledger has no entry in fiscal year 2023 (it holds 2024, 2025)"
    with pytest.raises(SamplingError) as not_a_year:
        fiscal_year_for(frame, "+-5")
    assert str(not_a_year.value).startswith("fiscal year must be a whole number of 1 or more")
    assert fiscal_year_for(_entries([1.0])) == 2024
    assert fiscal_year_for(_entries([])) is None


def test_the_memo_says_how_the_fiscal_year_is_read():
    selection = sampling.mus_select(_ledger([100.0, 200.0]), M, seed=1)
    notes = sampling.collect_memo(selection, ledger="x.csv", ledger_id="csv:1")["notes"]
    assert notes.index(FISCAL_YEAR_NOTE) == notes.index(sampling.POPULATION_NOTE) + 1
    assert "calendar year" in FISCAL_YEAR_NOTE
    assert "never part of it" in BAND_NOTE and "no entry is flagged or dropped by it" in BAND_NOTE


def test_the_names_sampling_lends_out_are_the_same_objects():
    assert sampling.Materiality is Materiality and sampling.SamplingError is SamplingError
    assert sampling.DEFAULT_PERFORMANCE_SHARE == Decimal("0.75")
    assert sampling.DEFAULT_TRIVIAL_SHARE == Decimal("0.05")
    assert sampling.CEILING is materiality.CEILING


def test_the_module_is_written_in_ascii():
    for path in (Path(materiality.__file__), Path(__file__)):
        assert path.read_text(encoding="utf-8").isascii(), path.name

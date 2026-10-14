import calendar
import warnings
from datetime import date, timedelta

import pytest

from ledgerlens.generate import generate_ledger
from ledgerlens.schema import AnomalyType, is_us_holiday


def test_generation_is_deterministic():
    a, _ = generate_ledger(end=date(2024, 3, 31), seed=42)
    b, _ = generate_ledger(end=date(2024, 3, 31), seed=42)
    assert a.equals(b)


def test_different_seeds_differ():
    a, _ = generate_ledger(end=date(2024, 3, 31), seed=1)
    b, _ = generate_ledger(end=date(2024, 3, 31), seed=2)
    assert not a.equals(b)


def test_entries_balance_except_injected_exceptions(ledger, labels):
    totals = ledger.groupby("entry_id")[["debit", "credit"]].sum()
    imbalanced = totals[(totals["debit"] - totals["credit"]).abs() > 0.005]
    unbalanced_ids = set(
        labels.loc[labels["anomaly_type"] == AnomalyType.UNBALANCED_ENTRY, "entry_id"]
    )
    # The only entries allowed to be out of balance are the ones we injected.
    assert set(imbalanced.index) == unbalanced_ids


def test_every_archetype_is_injected(labels):
    injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
    assert injected == set(AnomalyType.ALL)


def test_anomaly_rate_is_close_to_requested(labels):
    assert 0.01 <= labels["is_anomaly"].mean() <= 0.025


def test_labels_cover_every_entry(ledger, labels):
    assert set(ledger["entry_id"]) == set(labels["entry_id"])


def test_normal_entries_are_on_business_days(ledger, labels):
    normal_ids = set(labels.loc[~labels["is_anomaly"], "entry_id"])
    normal = ledger[ledger["entry_id"].isin(normal_ids)]
    # Injected weekend/holiday entries are the exception; the baseline is not.
    assert not normal["is_weekend"].any()
    assert not normal["is_holiday"].any()


def test_rejects_impossible_parameters():
    with pytest.raises(ValueError):
        generate_ledger(anomaly_rate=1.5)
    with pytest.raises(ValueError):
        generate_ledger(start=date(2025, 1, 1), end=date(2024, 1, 1))


# --- ranges too short for some archetypes --------------------------------------
# The calendar is worked out here from first principles, not through the
# generator's own helpers, so the expectation cannot share a mistake with them.

def _days(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _lacking(start, end):
    """The archetypes a range has no day for, as this test reads the calendar."""
    days = _days(start, end)
    lacking = set()
    if not any(d.weekday() >= 5 for d in days):
        lacking.add(AnomalyType.WEEKEND_ENTRY)
    if not any(is_us_holiday(d) and d.weekday() < 5 for d in days):
        lacking.add(AnomalyType.HOLIDAY_ENTRY)
    if not any(calendar.monthrange(d.year, d.month)[1] - d.day <= 2 for d in days):
        lacking.add(AnomalyType.PERIOD_END_MANUAL_REVENUE)
    return lacking


def _has_business_day(start, end):
    return any(d.weekday() < 5 and not is_us_holiday(d) for d in _days(start, end))


def _generate_noting_skips(start, end):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lines, labels = generate_ledger(start=start, end=end, entries_per_day=2)
    return lines, labels, [str(w.message) for w in caught if issubclass(w.category, UserWarning)]


@pytest.mark.parametrize("start, end, lacking", [
    # The reported case: New Year's Day and a weekend, but no period end.
    (date(2024, 1, 1), date(2024, 1, 10), {AnomalyType.PERIOD_END_MANUAL_REVENUE}),
    (date(2024, 1, 2), date(2024, 1, 5), {AnomalyType.WEEKEND_ENTRY, AnomalyType.HOLIDAY_ENTRY,
                                          AnomalyType.PERIOD_END_MANUAL_REVENUE}),
    (date(2024, 1, 29), date(2024, 1, 31), {AnomalyType.WEEKEND_ENTRY, AnomalyType.HOLIDAY_ENTRY}),
    # March and April hold no federal holiday: a two-month ledger can lack one.
    (date(2024, 3, 1), date(2024, 4, 30), {AnomalyType.HOLIDAY_ENTRY}),
])
def test_a_short_range_skips_what_it_cannot_place_and_says_so(start, end, lacking):
    assert _lacking(start, end) == lacking  # the cases are what they claim to be
    lines, labels, notes = _generate_noting_skips(start, end)
    injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
    assert injected == set(AnomalyType.ALL) - lacking
    assert set(lines["entry_id"]) == set(labels["entry_id"])
    assert len(notes) == len(lacking)
    for archetype in lacking:
        (note,) = [n for n in notes if archetype in n]
        assert note.startswith(f"{start}..{end} has ")
        assert "\n" not in note


def test_a_range_with_no_business_day_is_refused():
    # Saturday, Sunday and Martin Luther King Jr. Day: no normal activity at all,
    # so every archetype drawn from business days would have nowhere to go.
    with pytest.raises(ValueError, match=r"^2024-01-13\.\.2024-01-15 has no business day"):
        generate_ledger(start=date(2024, 1, 13), end=date(2024, 1, 15))


def test_every_one_and_three_day_range_of_a_year_generates_or_is_refused():
    for offset in range(366):
        start = date(2024, 1, 1) + timedelta(days=offset)
        for end in (start, start + timedelta(days=2)):
            if not _has_business_day(start, end):
                with pytest.raises(ValueError, match="no business day"):
                    generate_ledger(start=start, end=end, entries_per_day=2)
                continue
            _, labels, notes = _generate_noting_skips(start, end)
            injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
            assert injected == set(AnomalyType.ALL) - _lacking(start, end), (start, end)
            assert len(notes) == len(_lacking(start, end)), (start, end)

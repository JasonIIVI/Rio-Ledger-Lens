from datetime import date

from ledgerlens.schema import (
    REQUIRED_COLUMNS,
    AnomalyType,
    is_us_holiday,
    us_federal_holidays,
)


def test_eleven_federal_holidays():
    assert len(us_federal_holidays(2024)) == 11
    assert len(us_federal_holidays(2025)) == 11


def test_known_holiday_dates():
    # Verified against the 2024 federal calendar.
    assert is_us_holiday(date(2024, 11, 28))  # Thanksgiving, 4th Thursday
    assert is_us_holiday(date(2024, 5, 27))   # Memorial Day, last Monday
    assert is_us_holiday(date(2024, 9, 2))    # Labor Day, 1st Monday
    assert is_us_holiday(date(2024, 1, 15))   # MLK, 3rd Monday
    assert is_us_holiday(date(2024, 7, 4))


def test_ordinary_day_is_not_a_holiday():
    assert not is_us_holiday(date(2024, 3, 14))


def test_holidays_move_between_years():
    # Thanksgiving is a floating holiday; 2024 and 2025 must differ.
    assert date(2024, 11, 28) in us_federal_holidays(2024)
    assert date(2025, 11, 27) in us_federal_holidays(2025)


def test_every_archetype_is_named():
    assert len(AnomalyType.ALL) == 11
    assert len(set(AnomalyType.ALL)) == 11


def test_required_columns_are_declared():
    for column in ("entry_id", "debit", "credit", "posting_date", "entered_at"):
        assert column in REQUIRED_COLUMNS

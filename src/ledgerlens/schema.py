"""Column contract for a general ledger extract.

Every module in LedgerLens reads and writes this shape. Keeping it in one
place means a new source system (QuickBooks, NetSuite, a CSV a client
emailed over) only has to be mapped once, in the ingest layer.
"""

from __future__ import annotations

from datetime import date, datetime

# --- ledger lines -----------------------------------------------------------

#: Columns every GL extract must provide, and the pandas dtype we coerce to.
#: ``posting_date`` is when the entry hits the books; ``entered_at`` is when a
#: human (or a system) actually keyed it. Auditors care about the gap between
#: the two, and several journal-entry tests key off ``entered_at`` alone.
REQUIRED_COLUMNS: dict[str, str] = {
    "entry_id": "string",       # journal entry header id, e.g. JE-2024-000123
    "line_no": "int64",         # line within the entry, 1-based
    "posting_date": "datetime64[ns]",
    "entered_at": "datetime64[ns]",
    "fiscal_year": "int64",
    "period": "int64",          # 1-12
    "account_code": "string",
    "account_name": "string",
    "account_type": "string",   # Asset / Liability / Equity / Revenue / Expense
    "description": "string",
    "debit": "float64",
    "credit": "float64",
    "source": "string",         # Manual / AP / AR / Payroll / Bank / System
    "created_by": "string",     # user id who keyed the entry
}

#: Columns LedgerLens derives during ingest. Tests may rely on these existing.
DERIVED_COLUMNS = (
    "amount",        # signed: debit - credit
    "abs_amount",    # magnitude, what most tests actually score on
    "entered_hour",
    "entered_dow",   # 0=Monday .. 6=Sunday
    "is_weekend",
    "is_holiday",
    "posting_lag_days",  # posting_date - entered_at, in days
)

ACCOUNT_TYPES = ("Asset", "Liability", "Equity", "Revenue", "Expense")
SOURCES = ("Manual", "AP", "AR", "Payroll", "Bank", "System")

# --- ground truth labels ----------------------------------------------------

#: The generator writes labels to a *separate* file. Detection code never sees
#: them; only the scoring/evaluation step joins them back. This is the whole
#: reason the project can report precision and recall honestly.
LABEL_COLUMNS: dict[str, str] = {
    "entry_id": "string",
    "is_anomaly": "bool",
    "anomaly_type": "string",
}


class AnomalyType:
    """The archetypes the generator injects, and that the JETs aim to catch.

    Named after what an auditor would call the finding, not after the code
    that produces it - the strings show up in evaluation output and in the
    README, so they should read like an audit workpaper.
    """

    ROUND_AMOUNT = "round_amount"
    WEEKEND_ENTRY = "weekend_entry"
    AFTER_HOURS_ENTRY = "after_hours_entry"
    HOLIDAY_ENTRY = "holiday_entry"
    PERIOD_END_MANUAL_REVENUE = "period_end_manual_revenue"
    JUST_UNDER_THRESHOLD = "just_under_threshold"
    DUPLICATE_ENTRY = "duplicate_entry"
    RARE_ACCOUNT_PAIR = "rare_account_pair"
    DORMANT_ACCOUNT = "dormant_account"
    BENFORD_DRIFT = "benford_drift"
    UNBALANCED_ENTRY = "unbalanced_entry"

    ALL = (
        ROUND_AMOUNT,
        WEEKEND_ENTRY,
        AFTER_HOURS_ENTRY,
        HOLIDAY_ENTRY,
        PERIOD_END_MANUAL_REVENUE,
        JUST_UNDER_THRESHOLD,
        DUPLICATE_ENTRY,
        RARE_ACCOUNT_PAIR,
        DORMANT_ACCOUNT,
        BENFORD_DRIFT,
        UNBALANCED_ENTRY,
    )


# --- US federal holidays ----------------------------------------------------
# Computed rather than hardcoded so the generator works for any year, and
# without pulling in a holiday package for eleven dates.


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth `weekday` of a month. weekday: 0=Mon .. 6=Sun. n is 1-based."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last `weekday` of a month."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    d = date.fromordinal(nxt.toordinal() - 1)
    return date.fromordinal(d.toordinal() - ((d.weekday() - weekday) % 7))


def us_federal_holidays(year: int) -> set:
    """US federal holidays for `year`, as observed dates are not adjusted.

    We deliberately do not shift Saturday holidays to Friday: the test that
    uses this is looking for *entry activity on a day the office is closed*,
    and a weekend test already covers the Saturday case.
    """
    return {
        date(year, 1, 1),                        # New Year's Day
        _nth_weekday(year, 1, 0, 3),             # MLK Jr Day
        _nth_weekday(year, 2, 0, 3),             # Presidents' Day
        _last_weekday(year, 5, 0),               # Memorial Day
        date(year, 6, 19),                       # Juneteenth
        date(year, 7, 4),                        # Independence Day
        _nth_weekday(year, 9, 0, 1),             # Labor Day
        _nth_weekday(year, 10, 0, 2),            # Columbus Day
        date(year, 11, 11),                      # Veterans Day
        _nth_weekday(year, 11, 3, 4),            # Thanksgiving
        date(year, 12, 25),                      # Christmas
    }


def is_us_holiday(when) -> bool:
    """True if `when` (date or datetime) falls on a US federal holiday."""
    d = when.date() if isinstance(when, datetime) else when
    return d in us_federal_holidays(d.year)

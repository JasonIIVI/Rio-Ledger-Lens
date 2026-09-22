"""Synthetic general ledger generator with labelled anomalies.

Why synthetic: a portfolio project cannot ship a real company's ledger, and
public GL datasets are either tiny or unlabelled. Generating the data means we
know the ground truth for every entry, which is what lets LedgerLens report
precision and recall instead of just "it flagged some things".

The design goal is that *normal* entries are genuinely boring - amounts drawn
from a lognormal distribution (which is naturally Benford-conformant), posted
on business days during business hours by a small set of users - so that the
injected anomalies are detectable for the right reasons rather than because
the baseline is sloppy.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .coa import COMMON_PAIRS, RARELY_USED, account_name, account_type
from .schema import AnomalyType, is_us_holiday

# --- people who key entries -------------------------------------------------

USERS: tuple[str, ...] = ("jrivera", "mchen", "aokafor", "dsilva", "controller")

#: Who is allowed to post manual journal entries in the "designed" control
#: environment. Entries by anyone else are not automatically wrong, but the
#: segregation-of-duties test wants to know about them.
MANUAL_JE_APPROVERS: tuple[str, ...] = ("controller", "mchen")

#: Dollar limit above which a manual entry needs a second approval. The
#: just-under-threshold archetype clusters amounts right beneath it.
APPROVAL_THRESHOLD = 10_000.0

BUSINESS_START_HOUR = 8
BUSINESS_END_HOUR = 18


def _business_datetime(rng: random.Random, day: date) -> datetime:
    """A plausible keying timestamp during the working day."""
    hour = rng.randint(BUSINESS_START_HOUR, BUSINESS_END_HOUR - 1)
    return datetime(day.year, day.month, day.day, hour, rng.randint(0, 59), rng.randint(0, 59))


def _is_business_day(d: date) -> bool:
    return d.weekday() < 5 and not is_us_holiday(d)


def _business_days(start: date, end: date) -> list[date]:
    days, cur = [], start
    while cur <= end:
        if _is_business_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _lognormal_amount(rng: np.random.Generator, mean: float = 6.2, sigma: float = 1.1) -> float:
    """Draw a transaction amount.

    A lognormal spread of magnitudes is what makes real ledgers obey Benford's
    Law, so using it here means the Benford test has a legitimate baseline to
    measure against rather than a rigged one.
    """
    return round(float(rng.lognormal(mean, sigma)), 2)


class _EntryBuilder:
    """Accumulates balanced journal entries and their ground-truth labels."""

    def __init__(self, seed: int) -> None:
        self.rows: list[dict] = []
        self.labels: list[dict] = []
        self._seq = 0
        self.rng = random.Random(seed)
        self.nprng = np.random.default_rng(seed)

    def next_entry_id(self, when: date) -> str:
        self._seq += 1
        return f"JE-{when.year}-{self._seq:06d}"

    def add(
        self,
        posting_date: date,
        entered_at: datetime,
        debit_account: str,
        credit_account: str,
        amount: float,
        description: str,
        source: str,
        created_by: str,
        anomaly_type: str | None = None,
        credit_amount: float | None = None,
    ) -> str:
        """Add one two-line balanced entry and return its id.

        `credit_amount` exists only so the unbalanced-entry archetype can
        deliberately break the accounting equation; everything else leaves it
        alone and gets debits == credits.
        """
        entry_id = self.next_entry_id(posting_date)
        credit_value = amount if credit_amount is None else credit_amount
        period = posting_date.month

        for line_no, (acct, dr, cr) in enumerate(
            ((debit_account, amount, 0.0), (credit_account, 0.0, credit_value)), start=1
        ):
            self.rows.append(
                {
                    "entry_id": entry_id,
                    "line_no": line_no,
                    "posting_date": posting_date,
                    "entered_at": entered_at,
                    "fiscal_year": posting_date.year,
                    "period": period,
                    "account_code": acct,
                    "account_name": account_name(acct),
                    "account_type": account_type(acct),
                    "description": description,
                    "debit": dr,
                    "credit": cr,
                    "source": source,
                    "created_by": created_by,
                }
            )

        self.labels.append(
            {
                "entry_id": entry_id,
                "is_anomaly": anomaly_type is not None,
                "anomaly_type": anomaly_type or "",
            }
        )
        return entry_id


# --- normal activity --------------------------------------------------------

_DESCRIPTIONS = {
    ("1200", "4000"): "Invoice {n} - customer {c}",
    ("1000", "1200"): "Payment received - customer {c}",
    ("5000", "1300"): "COGS - invoice {n}",
    ("1300", "2000"): "Inventory PO {n} - vendor {v}",
    ("2000", "1000"): "Vendor payment - {v}",
    ("6000", "2200"): "Payroll accrual - period {n}",
    ("2200", "1010"): "Payroll disbursement - period {n}",
    ("6100", "1000"): "Monthly rent",
    ("6200", "2000"): "Utilities - {v}",
    ("6400", "2000"): "Professional fees - {v}",
    ("6500", "1000"): "Ad spend - {v}",
    ("6600", "1000"): "Office supplies - {v}",
    ("6800", "1510"): "Monthly depreciation",
    ("7000", "1000"): "Interest on note payable",
    ("1000", "4100"): "Service revenue - customer {c}",
}

_VENDORS = ("Acme Paper", "Gulf Freight", "Miami Power", "Nexa Insurance", "Torres CPA",
            "PrintWorks", "BlueRock Media", "Citywide Supply")
_CUSTOMERS = ("Delgado Retail", "Harborview Inc", "Sunbelt Markets", "Praxis Group",
              "Coral Trading", "Vista Foods", "Northline LLC")


def _describe(b: _EntryBuilder, pair: tuple[str, str], n: int) -> str:
    template = _DESCRIPTIONS.get(pair, "Journal entry {n}")
    return template.format(n=n, v=b.rng.choice(_VENDORS), c=b.rng.choice(_CUSTOMERS))


def _source_for(pair: tuple[str, str]) -> str:
    debit, credit = pair
    if pair in (("6000", "2200"), ("2200", "1010")):
        return "Payroll"
    if credit == "2000" or debit == "1300":
        return "AP"
    if debit == "1200" or credit == "4000":
        return "AR"
    if pair == ("6800", "1510"):
        return "System"
    if debit == "1000" or credit == "1000":
        return "Bank"
    return "Manual"


def _generate_normal(b: _EntryBuilder, days: list[date], entries_per_day: int) -> None:
    for day in days:
        n_today = max(1, int(b.nprng.poisson(entries_per_day)))
        for _ in range(n_today):
            pair = b.rng.choice(COMMON_PAIRS)
            amount = _lognormal_amount(b.nprng)
            source = _source_for(pair)
            # Systems post themselves. Manual entries come from an authorised
            # keyer almost always - the occasional exception is what gives the
            # segregation-of-duties test something real to find.
            if source == "System":
                user = "system"
            elif source == "Manual":
                user = (b.rng.choice(MANUAL_JE_APPROVERS) if b.rng.random() < 0.94
                        else b.rng.choice(USERS))
            else:
                user = b.rng.choice(USERS)
            b.add(
                posting_date=day,
                entered_at=_business_datetime(b.rng, day),
                debit_account=pair[0],
                credit_account=pair[1],
                amount=amount,
                description=_describe(b, pair, b.rng.randint(1000, 9999)),
                source=source,
                created_by=user,
            )


# --- anomaly injection ------------------------------------------------------
# Each injector adds entries that a real auditor would want surfaced. They are
# written to be *plausible*, not cartoonish: a round $50,000 transfer is not
# proof of anything, it is a question worth asking.


def _inject_round_amounts(b: _EntryBuilder, days: list[date], n: int) -> None:
    for _ in range(n):
        day = b.rng.choice(days)
        amount = float(b.rng.choice([5_000, 10_000, 25_000, 50_000, 75_000, 100_000]))
        b.add(day, _business_datetime(b.rng, day), "6900", "1000", amount,
              "Adjustment per management", "Manual", b.rng.choice(MANUAL_JE_APPROVERS),
              AnomalyType.ROUND_AMOUNT)


def _inject_weekend(b: _EntryBuilder, start: date, end: date, n: int) -> None:
    weekends = [d for d in _all_days(start, end) if d.weekday() >= 5]
    for _ in range(n):
        day = b.rng.choice(weekends)
        pair = b.rng.choice(COMMON_PAIRS)
        b.add(day, _business_datetime(b.rng, day), pair[0], pair[1],
              _lognormal_amount(b.nprng), "Weekend posting", "Manual",
              b.rng.choice(USERS), AnomalyType.WEEKEND_ENTRY)


def _inject_after_hours(b: _EntryBuilder, days: list[date], n: int) -> None:
    for _ in range(n):
        day = b.rng.choice(days)
        hour = b.rng.choice([0, 1, 2, 3, 4, 22, 23])
        stamp = datetime(day.year, day.month, day.day, hour, b.rng.randint(0, 59))
        pair = b.rng.choice(COMMON_PAIRS)
        b.add(day, stamp, pair[0], pair[1], _lognormal_amount(b.nprng),
              "Late-night adjustment", "Manual", b.rng.choice(USERS),
              AnomalyType.AFTER_HOURS_ENTRY)


def _inject_holiday(b: _EntryBuilder, start: date, end: date, n: int) -> None:
    holidays = [d for d in _all_days(start, end) if is_us_holiday(d) and d.weekday() < 5]
    for _ in range(n):
        day = b.rng.choice(holidays)
        pair = b.rng.choice(COMMON_PAIRS)
        b.add(day, _business_datetime(b.rng, day), pair[0], pair[1],
              _lognormal_amount(b.nprng), "Holiday posting", "Manual",
              b.rng.choice(USERS), AnomalyType.HOLIDAY_ENTRY)


def _inject_period_end_revenue(b: _EntryBuilder, start: date, end: date, n: int) -> None:
    """Manual entries crediting revenue in the last three days of a period.

    This is the classic revenue cut-off / management-override pattern that
    AU-C 240 tells auditors to go looking for.
    """
    candidates = []
    for d in _all_days(start, end):
        last_day = _month_end(d)
        if (last_day - d).days <= 2:
            candidates.append(d)
    for _ in range(n):
        day = b.rng.choice(candidates)
        amount = _lognormal_amount(b.nprng, mean=8.0, sigma=0.6)  # deliberately large
        b.add(day, _business_datetime(b.rng, day), "1200", "4000", amount,
              "Revenue accrual - period close", "Manual",
              b.rng.choice(MANUAL_JE_APPROVERS), AnomalyType.PERIOD_END_MANUAL_REVENUE)


def _inject_just_under_threshold(b: _EntryBuilder, days: list[date], n: int) -> None:
    for _ in range(n):
        day = b.rng.choice(days)
        amount = round(APPROVAL_THRESHOLD - b.rng.uniform(1, 250), 2)
        b.add(day, _business_datetime(b.rng, day), "6400", "2000", amount,
              "Consulting services", "Manual", b.rng.choice(USERS),
              AnomalyType.JUST_UNDER_THRESHOLD)


def _inject_duplicates(b: _EntryBuilder, days: list[date], n: int) -> None:
    for _ in range(n):
        day = b.rng.choice(days)
        pair = b.rng.choice(COMMON_PAIRS)
        amount = _lognormal_amount(b.nprng)
        desc = _describe(b, pair, b.rng.randint(1000, 9999))
        user = b.rng.choice(USERS)
        # The original is legitimate; the re-post a day or two later is not.
        b.add(day, _business_datetime(b.rng, day), pair[0], pair[1], amount, desc,
              _source_for(pair), user)
        dup_day = day + timedelta(days=b.rng.randint(1, 3))
        b.add(dup_day, _business_datetime(b.rng, dup_day), pair[0], pair[1], amount, desc,
              _source_for(pair), user, AnomalyType.DUPLICATE_ENTRY)


def _inject_rare_pairs(b: _EntryBuilder, days: list[date], n: int) -> None:
    odd_pairs = (("4000", "1000"), ("6000", "4000"), ("1500", "4000"),
                 ("3000", "1000"), ("2500", "4100"))
    for _ in range(n):
        day = b.rng.choice(days)
        pair = b.rng.choice(odd_pairs)
        b.add(day, _business_datetime(b.rng, day), pair[0], pair[1],
              _lognormal_amount(b.nprng), "Reclassification", "Manual",
              b.rng.choice(USERS), AnomalyType.RARE_ACCOUNT_PAIR)


def _inject_dormant(b: _EntryBuilder, days: list[date], n: int) -> None:
    for _ in range(n):
        day = b.rng.choice(days[len(days) // 2:])  # only in the back half of the period
        acct = b.rng.choice(RARELY_USED)
        b.add(day, _business_datetime(b.rng, day), acct, "1000",
              _lognormal_amount(b.nprng, mean=7.5), "Account activity", "Manual",
              b.rng.choice(USERS), AnomalyType.DORMANT_ACCOUNT)


def _inject_benford_drift(b: _EntryBuilder, days: list[date], n: int) -> None:
    """A cluster of fabricated amounts whose leading digits skew high.

    Invented numbers tend to start with 5-9 far more often than real ones do,
    which is exactly the signal Benford's Law picks up.
    """
    for _ in range(n):
        day = b.rng.choice(days)
        lead = b.rng.choice([5, 6, 7, 8, 9])
        amount = float(lead) * 1000 + b.rng.uniform(0, 999)
        b.add(day, _business_datetime(b.rng, day), "6700", "2000", round(amount, 2),
              "Repairs - contractor", "Manual", b.rng.choice(USERS),
              AnomalyType.BENFORD_DRIFT)


def _inject_unbalanced(b: _EntryBuilder, days: list[date], n: int) -> None:
    """Entries where debits != credits.

    A real ERP will not let this happen, which is the point: if one shows up
    in an extract, either a control failed or the extract itself is wrong.
    Either way the auditor needs to know before relying on the population.
    """
    for _ in range(n):
        day = b.rng.choice(days)
        amount = _lognormal_amount(b.nprng)
        b.add(day, _business_datetime(b.rng, day), "6900", "1000", amount,
              "Suspense clearing", "Manual", b.rng.choice(USERS),
              AnomalyType.UNBALANCED_ENTRY,
              credit_amount=round(amount - b.rng.uniform(1, 500), 2))


# --- date helpers -----------------------------------------------------------


def _all_days(start: date, end: date) -> list[date]:
    days, cur = [], start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _month_end(d: date) -> date:
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


# --- public API -------------------------------------------------------------

#: Relative weight of each archetype in the injected population. Roughly
#: mirrors how often each pattern shows up in practice - duplicates and
#: round numbers are common, unbalanced entries are rare.
ANOMALY_MIX: dict[str, float] = {
    AnomalyType.ROUND_AMOUNT: 0.14,
    AnomalyType.WEEKEND_ENTRY: 0.13,
    AnomalyType.AFTER_HOURS_ENTRY: 0.12,
    AnomalyType.HOLIDAY_ENTRY: 0.06,
    AnomalyType.PERIOD_END_MANUAL_REVENUE: 0.11,
    AnomalyType.JUST_UNDER_THRESHOLD: 0.10,
    AnomalyType.DUPLICATE_ENTRY: 0.13,
    AnomalyType.RARE_ACCOUNT_PAIR: 0.09,
    AnomalyType.DORMANT_ACCOUNT: 0.05,
    AnomalyType.BENFORD_DRIFT: 0.05,
    AnomalyType.UNBALANCED_ENTRY: 0.02,
}

_INJECTORS = {
    AnomalyType.ROUND_AMOUNT: lambda b, d, s, e, n: _inject_round_amounts(b, d, n),
    AnomalyType.WEEKEND_ENTRY: lambda b, d, s, e, n: _inject_weekend(b, s, e, n),
    AnomalyType.AFTER_HOURS_ENTRY: lambda b, d, s, e, n: _inject_after_hours(b, d, n),
    AnomalyType.HOLIDAY_ENTRY: lambda b, d, s, e, n: _inject_holiday(b, s, e, n),
    AnomalyType.PERIOD_END_MANUAL_REVENUE: lambda b, d, s, e, n: _inject_period_end_revenue(b, s, e, n),
    AnomalyType.JUST_UNDER_THRESHOLD: lambda b, d, s, e, n: _inject_just_under_threshold(b, d, n),
    AnomalyType.DUPLICATE_ENTRY: lambda b, d, s, e, n: _inject_duplicates(b, d, n),
    AnomalyType.RARE_ACCOUNT_PAIR: lambda b, d, s, e, n: _inject_rare_pairs(b, d, n),
    AnomalyType.DORMANT_ACCOUNT: lambda b, d, s, e, n: _inject_dormant(b, d, n),
    AnomalyType.BENFORD_DRIFT: lambda b, d, s, e, n: _inject_benford_drift(b, d, n),
    AnomalyType.UNBALANCED_ENTRY: lambda b, d, s, e, n: _inject_unbalanced(b, d, n),
}


def generate_ledger(
    start: date = date(2024, 1, 1),
    end: date = date(2025, 12, 31),
    entries_per_day: int = 10,
    anomaly_rate: float = 0.015,
    seed: int = 20260922,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate a labelled synthetic general ledger.

    Returns ``(lines, labels)``. ``lines`` is the GL extract in the shape
    defined by :mod:`ledgerlens.schema`; ``labels`` is one row per entry with
    the ground truth, kept separate so detection code cannot cheat.
    """
    if not 0 <= anomaly_rate < 1:
        raise ValueError("anomaly_rate must be in [0, 1)")
    if end < start:
        raise ValueError("end must not precede start")

    b = _EntryBuilder(seed)
    business = _business_days(start, end)
    _generate_normal(b, business, entries_per_day)

    normal_count = len(b.labels)
    total_anomalies = max(len(ANOMALY_MIX), int(round(normal_count * anomaly_rate)))

    for archetype, weight in ANOMALY_MIX.items():
        n = max(1, int(round(total_anomalies * weight)))
        _INJECTORS[archetype](b, business, start, end, n)

    lines = pd.DataFrame(b.rows)
    labels = pd.DataFrame(b.labels)

    # Shuffle so injected entries are not all clustered at the end of the file,
    # then restore a natural posting order.
    lines = lines.sort_values(["posting_date", "entry_id", "line_no"]).reset_index(drop=True)
    labels = labels.sort_values("entry_id").reset_index(drop=True)

    lines["posting_date"] = pd.to_datetime(lines["posting_date"])
    lines["entered_at"] = pd.to_datetime(lines["entered_at"])
    return lines, labels

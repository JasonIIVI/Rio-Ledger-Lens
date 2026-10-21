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

import bisect
import random
import warnings
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .coa import COMMON_PAIRS, RARELY_USED, account_name, account_type
from .schema import AnomalyType, HeldOutType, is_us_holiday

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

#: The default ledger's seed: the generator's default, and `ledgerlens generate`'s.
DEFAULT_SEED = 20260922

#: The held-out ledger's seed, written down in the commit that adds the held-out code and
#: passed to the generator for the first time only once that code is frozen (the
#: `held-out-freeze` tag): nothing is tuned on what it gives. The tests use other seeds,
#: and fail if anything generates with this one.
HELD_OUT_SEED = 20261019


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


def _fill(rng: random.Random, pair: tuple[str, str], n: int) -> str:
    template = _DESCRIPTIONS.get(pair, "Journal entry {n}")
    return template.format(n=n, v=rng.choice(_VENDORS), c=rng.choice(_CUSTOMERS))


def _describe(b: _EntryBuilder, pair: tuple[str, str], n: int) -> str:
    return _fill(b.rng, pair, n)


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


def _inject_weekend(b: _EntryBuilder, weekends: list[date], n: int) -> None:
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


def _inject_holiday(b: _EntryBuilder, holidays: list[date], n: int) -> None:
    for _ in range(n):
        day = b.rng.choice(holidays)
        pair = b.rng.choice(COMMON_PAIRS)
        b.add(day, _business_datetime(b.rng, day), pair[0], pair[1],
              _lognormal_amount(b.nprng), "Holiday posting", "Manual",
              b.rng.choice(USERS), AnomalyType.HOLIDAY_ENTRY)


def _inject_period_end_revenue(b: _EntryBuilder, candidates: list[date], n: int) -> None:
    """Manual entries crediting revenue in the last three days of a period.

    This is the classic revenue cut-off / management-override pattern that
    AU-C 240 tells auditors to go looking for.
    """
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


def _weekend_days(start: date, end: date) -> list[date]:
    return [d for d in _all_days(start, end) if d.weekday() >= 5]


def _weekday_holidays(start: date, end: date) -> list[date]:
    return [d for d in _all_days(start, end) if is_us_holiday(d) and d.weekday() < 5]


def _period_end_days(start: date, end: date) -> list[date]:
    """The last three days of each month, weekends included."""
    return [d for d in _all_days(start, end) if (_month_end(d) - d).days <= 2]


# --- held-out archetypes -------------------------------------------------------
# Patterns no test describes, injected only into the opt-in held-out ledger
# (`generate_ledger(held_out=True)`). Each is built to be rule-silent: a business
# day and hour, a common pair whose source is neither Manual (JET-05 and JET-12
# read only manual entries) nor System, an amount carrying cents (JET-01 reads
# whole thousands) and outside the 3% beneath the approval limit (JET-06),
# balanced. That silences JET-01 to JET-06, JET-10 and JET-12 by construction;
# JET-07, 08, 09 and 11 read the population, so they are checked on development
# seeds instead (on a ledger of a few dozen entries JET-08 can fire on one). The
# model tier is the one measured on them. They draw from their own random streams
# and take their ids after every other entry, so the rest of the ledger is the one
# the same seed gives without them.

#: The common pairs a held-out entry may use.
_HELD_OUT_PAIRS: tuple[tuple[str, str], ...] = tuple(
    p for p in COMMON_PAIRS if _source_for(p) not in ("Manual", "System"))

#: Users the ledger has never seen: a temp, a service account, a second admin.
_UNFAMILIAR_USERS: tuple[str, ...] = ("temp01", "temp02", "svc_import", "admin2")

#: A real vendor's name, a letter or a mark away.
_NEAR_MISS_VENDORS: tuple[str, ...] = (
    "Acme Papers", "Gulf Fraight", "Print Works", "Torres C.P.A.", "Citywide Suply")

#: JET-06 looks 3% beneath the limit; no held-out amount lands there.
_JUST_UNDER = APPROVAL_THRESHOLD * 0.97

#: How many of each held-out archetype: entries, or groups of entries for the three
#: built from several (3 parts per split purchase, 2 legs per round trip, 5 payments
#: per drip), 43 entries in all. Fixed, not scaled by `anomaly_rate`.
HELD_OUT_MIX: dict[str, int] = {
    HeldOutType.TOKEN_AMOUNT: 8,
    HeldOutType.UNFAMILIAR_USER: 8,
    HeldOutType.SPLIT_PURCHASE: 3,
    HeldOutType.ROUND_TRIP: 4,
    HeldOutType.SHELL_VENDOR_DRIP: 2,
}


class _HeldOutStreams:
    """The held-out archetypes' own random streams, seeded apart from the main pair."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(f"ledgerlens-held-out:{seed}")
        self.nprng = np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(1,)))


def _with_cents(rng: random.Random, low: float, high: float) -> float:
    """An amount in [low, high] that is not a whole dollar."""
    while True:
        amount = round(rng.uniform(low, high), 2)
        if amount % 1 and not _JUST_UNDER <= amount < APPROVAL_THRESHOLD:
            return amount


def _held_out_entry(b: _EntryBuilder, s: _HeldOutStreams, day: date, pair: tuple[str, str],
                    amount: float, description: str, user: str, archetype: str) -> None:
    b.add(day, _business_datetime(s.rng, day), pair[0], pair[1], amount, description,
          _source_for(pair), user, archetype)


def _inject_token_amounts(b: _EntryBuilder, s: _HeldOutStreams, business: list[date], n: int) -> None:
    """A dollar or two on an ordinary pair: a test posting, or a probe of what goes through."""
    for _ in range(n):
        day, pair = s.rng.choice(business), s.rng.choice(_HELD_OUT_PAIRS)
        _held_out_entry(b, s, day, pair, _with_cents(s.rng, 0.50, 2.99),
                        _fill(s.rng, pair, s.rng.randint(1000, 9999)), s.rng.choice(USERS),
                        HeldOutType.TOKEN_AMOUNT)


def _inject_unfamiliar_users(b: _EntryBuilder, s: _HeldOutStreams, business: list[date], n: int) -> None:
    """Ordinary entries keyed by someone the ledger has never seen."""
    for _ in range(n):
        day, pair = s.rng.choice(business), s.rng.choice(_HELD_OUT_PAIRS)
        _held_out_entry(b, s, day, pair, _with_cents(s.rng, 40.0, 2_500.0),
                        _fill(s.rng, pair, s.rng.randint(1000, 9999)),
                        s.rng.choice(_UNFAMILIAR_USERS), HeldOutType.UNFAMILIAR_USER)


def _inject_split_purchases(b: _EntryBuilder, s: _HeldOutStreams, business: list[date], n: int) -> None:
    """One vendor, one day, three purchases that together pass the approval limit."""
    pair = ("1300", "2000")
    for _ in range(n):
        day, vendor, user = s.rng.choice(business), s.rng.choice(_VENDORS), s.rng.choice(USERS)
        total = int(round(_with_cents(s.rng, APPROVAL_THRESHOLD + 50, APPROVAL_THRESHOLD + 800) * 100))
        while True:  # three distinct parts, each carrying cents, adding up to the total exactly
            first = s.rng.randint(total * 31 // 100, total * 35 // 100)
            second = s.rng.randint(total * 31 // 100, total * 35 // 100)
            parts = (first, second, total - first - second)
            if len(set(parts)) == 3 and all(p % 100 for p in parts):
                break
        for cents in parts:
            _held_out_entry(b, s, day, pair, cents / 100,
                            f"Inventory PO {s.rng.randint(1000, 9999)} - vendor {vendor}", user,
                            HeldOutType.SPLIT_PURCHASE)


def _inject_round_trips(b: _EntryBuilder, s: _HeldOutStreams, business: list[date], n: int) -> None:
    """Cash revenue in, and the same amount out as ad spend three business days later."""
    for _ in range(n):
        i = s.rng.randrange(len(business) - 3)
        amount = _with_cents(s.rng, 1_500.0, 6_000.0)
        _held_out_entry(b, s, business[i], ("1000", "4100"), amount,
                        f"Service revenue - customer {s.rng.choice(_CUSTOMERS)}",
                        s.rng.choice(USERS), HeldOutType.ROUND_TRIP)
        _held_out_entry(b, s, business[i + 3], ("6500", "1000"), amount,
                        f"Ad spend - {s.rng.choice(_VENDORS)}", s.rng.choice(USERS),
                        HeldOutType.ROUND_TRIP)


def _drip_days(business: list[date], start: date) -> list[date] | None:
    """The first business day on or after each fortnight from ``start``, five of them."""
    days = []
    for k in range(5):
        i = bisect.bisect_left(business, start + timedelta(days=14 * k))
        if i == len(business):
            return None
        days.append(business[i])
    return days


def _inject_vendor_drips(b: _EntryBuilder, s: _HeldOutStreams, business: list[date], n: int) -> None:
    """A few hundred dollars every two weeks to a vendor whose name is a near miss."""
    pair = ("6600", "1000")
    starts = [d for d in business if _drip_days(business, d) is not None]
    for vendor in s.rng.sample(_NEAR_MISS_VENDORS, n):  # one vendor per drip
        user = s.rng.choice(USERS)
        for day in _drip_days(business, s.rng.choice(starts)):
            _held_out_entry(b, s, day, pair, _with_cents(s.rng, 150.0, 450.0),
                            f"Office supplies - {vendor}", user, HeldOutType.SHELL_VENDOR_DRIP)


_HELD_OUT_INJECTORS = {
    HeldOutType.TOKEN_AMOUNT: _inject_token_amounts,
    HeldOutType.UNFAMILIAR_USER: _inject_unfamiliar_users,
    HeldOutType.SPLIT_PURCHASE: _inject_split_purchases,
    HeldOutType.ROUND_TRIP: _inject_round_trips,
    HeldOutType.SHELL_VENDOR_DRIP: _inject_vendor_drips,
}

#: What a short range can lack for a held-out archetype; the other three need only
#: one business day, and a range with none is refused.
_HELD_OUT_NEEDS = {
    HeldOutType.ROUND_TRIP: (lambda business: len(business) > 3,
                             "no business day three business days before another"),
    HeldOutType.SHELL_VENDOR_DRIP: (
        lambda business: any(_drip_days(business, d) is not None for d in business),
        "no room for five payments a fortnight apart"),
}


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

#: Each injector draws its days from the list it is given: the business days,
#: unless the archetype is in `_CALENDAR_DAYS`.
_INJECTORS = {
    AnomalyType.ROUND_AMOUNT: _inject_round_amounts,
    AnomalyType.WEEKEND_ENTRY: _inject_weekend,
    AnomalyType.AFTER_HOURS_ENTRY: _inject_after_hours,
    AnomalyType.HOLIDAY_ENTRY: _inject_holiday,
    AnomalyType.PERIOD_END_MANUAL_REVENUE: _inject_period_end_revenue,
    AnomalyType.JUST_UNDER_THRESHOLD: _inject_just_under_threshold,
    AnomalyType.DUPLICATE_ENTRY: _inject_duplicates,
    AnomalyType.RARE_ACCOUNT_PAIR: _inject_rare_pairs,
    AnomalyType.DORMANT_ACCOUNT: _inject_dormant,
    AnomalyType.BENFORD_DRIFT: _inject_benford_drift,
    AnomalyType.UNBALANCED_ENTRY: _inject_unbalanced,
}

#: The archetypes that land on days other than business days: the days each is
#: drawn from, and what a range with none of them lacks. A range with no
#: business day is refused outright, so these are the only archetypes a short
#: range can leave with nowhere to go.
_CALENDAR_DAYS = {
    AnomalyType.WEEKEND_ENTRY: (_weekend_days, "no Saturday or Sunday"),
    AnomalyType.HOLIDAY_ENTRY: (_weekday_holidays, "no federal holiday on a weekday"),
    AnomalyType.PERIOD_END_MANUAL_REVENUE: (_period_end_days,
                                            "none of the last three days of a month"),
}


class ArchetypeSkipped(UserWarning):
    """A range has no day an archetype can land on, so the ledger is generated without it."""


def generate_ledger(
    start: date = date(2024, 1, 1),
    end: date = date(2025, 12, 31),
    entries_per_day: int = 10,
    anomaly_rate: float = 0.015,
    seed: int = DEFAULT_SEED,
    held_out: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate a labelled synthetic general ledger.

    Returns ``(lines, labels)``. ``lines`` is the GL extract in the shape
    defined by :mod:`ledgerlens.schema`; ``labels`` is one row per entry with
    the ground truth, kept separate so detection code cannot cheat.

    A short range may hold no weekend, no weekday federal holiday or none of
    a month's last three days. The archetype that lands there is then left
    out, with an :class:`ArchetypeSkipped` warning naming it, and the other
    archetypes keep their counts. A range with no business day at all is
    refused with a ``ValueError``.

    ``held_out`` adds the archetypes no test describes (:class:`HeldOutType`,
    ``HELD_OUT_MIX``), drawn from their own streams and numbered after every
    other entry, so the rest of the ledger is the one ``held_out=False`` gives. A
    range too short for one of them leaves it out the same way.
    """
    if not 0 <= anomaly_rate < 1:
        raise ValueError("anomaly_rate must be in [0, 1)")
    if end < start:
        raise ValueError(f"end {end} precedes start {start}")
    business = _business_days(start, end)
    if not business:
        # No normal activity, and nowhere for the eight archetypes drawn from
        # business days: the ledger would be nothing but exceptions, and every
        # number measured on it would mean nothing.
        raise ValueError(f"{start}..{end} has no business day (each day is a weekend or a "
                         "federal holiday), so there is no normal activity to generate")

    b = _EntryBuilder(seed)
    _generate_normal(b, business, entries_per_day)

    normal_count = len(b.labels)
    total_anomalies = max(len(ANOMALY_MIX), int(round(normal_count * anomaly_rate)))

    for archetype, weight in ANOMALY_MIX.items():
        days = business
        if archetype in _CALENDAR_DAYS:
            where, lacking = _CALENDAR_DAYS[archetype]
            days = where(start, end)
            if not days:
                warnings.warn(f"{start}..{end} has {lacking}, so no {archetype} anomaly was "
                              "injected", ArchetypeSkipped, stacklevel=2)
                continue
        n = max(1, int(round(total_anomalies * weight)))
        _INJECTORS[archetype](b, days, n)

    if held_out:
        streams = _HeldOutStreams(seed)
        for archetype, n in HELD_OUT_MIX.items():
            if archetype in _HELD_OUT_NEEDS:
                fits, lacking = _HELD_OUT_NEEDS[archetype]
                if not fits(business):
                    warnings.warn(f"{start}..{end} has {lacking}, so no {archetype} anomaly was "
                                  "injected", ArchetypeSkipped, stacklevel=2)
                    continue
            _HELD_OUT_INJECTORS[archetype](b, streams, business, n)

    lines = pd.DataFrame(b.rows)
    labels = pd.DataFrame(b.labels)

    # Shuffle so injected entries are not all clustered at the end of the file,
    # then restore a natural posting order.
    lines = lines.sort_values(["posting_date", "entry_id", "line_no"]).reset_index(drop=True)
    labels = labels.sort_values("entry_id").reset_index(drop=True)

    lines["posting_date"] = pd.to_datetime(lines["posting_date"])
    lines["entered_at"] = pd.to_datetime(lines["entered_at"])
    return lines, labels

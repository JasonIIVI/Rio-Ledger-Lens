"""Journal Entry Tests (JETs).

Deterministic, explainable tests of the kind an audit team runs over a full
population of journal entries. They are the first tier of LedgerLens: cheap,
transparent, and defensible in a workpaper. The ML layer (week 2) exists to
catch what these rules cannot describe, and the LLM layer (week 3) exists to
explain what both tiers produce.

Every test returns the same shape - one row per flagged entry with a plain
language ``reason`` - so the composite score can treat them uniformly and a
reviewer can read any single flag without reading the code.

A flag is a question, not a finding. The wording of every ``reason`` is
chosen with that in mind.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .generate import APPROVAL_THRESHOLD, MANUAL_JE_APPROVERS
from .ingest import entry_level

FLAG_COLUMNS = ("entry_id", "test_id", "test_name", "severity", "reason")

# Severity drives the composite score and the order a reviewer works the
# queue. High = a control may have failed; Medium = unusual, explain it;
# Low = worth a glance in aggregate.
HIGH, MEDIUM, LOW = "High", "Medium", "Low"


def _flags(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(FLAG_COLUMNS))
    return pd.DataFrame(rows, columns=list(FLAG_COLUMNS))


def _entry_pairs(df: pd.DataFrame) -> pd.DataFrame:
    """One row per entry with its debit and credit account codes."""
    debits = (
        df[df["debit"] > 0]
        .groupby("entry_id")["account_code"]
        .agg(lambda s: "|".join(sorted(set(s))))
        .rename("debit_accounts")
    )
    credits = (
        df[df["credit"] > 0]
        .groupby("entry_id")["account_code"]
        .agg(lambda s: "|".join(sorted(set(s))))
        .rename("credit_accounts")
    )
    pairs = pd.concat([debits, credits], axis=1).reset_index()
    pairs["pair"] = pairs["debit_accounts"].fillna("") + ">" + pairs["credit_accounts"].fillna("")
    return pairs


# --- the tests --------------------------------------------------------------


def jet_round_amount(df: pd.DataFrame, min_amount: float = 1000.0) -> pd.DataFrame:
    """Entries for a suspiciously round value.

    Real invoices carry cents. Round thousands usually mean an estimate, an
    accrual, or a number somebody chose - all legitimate, all worth naming.
    """
    e = entry_level(df)
    hit = e[(e["entry_amount"] >= min_amount) & (e["entry_amount"] % 1000 == 0)]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-01",
            "test_name": "Round-dollar amount",
            "severity": MEDIUM,
            "reason": f"Entry total is exactly ${r.entry_amount:,.0f} - a round thousand. Confirm the "
                      "basis for the amount and whether it is an estimate or accrual."
                      ,
        }
        for r in hit.itertuples()
    ])


def jet_weekend_entry(df: pd.DataFrame) -> pd.DataFrame:
    """Entries keyed on a Saturday or Sunday."""
    e = entry_level(df)
    hit = e[e["is_weekend"]]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-02",
            "test_name": "Weekend posting",
            "severity": MEDIUM,
            "reason": "Keyed on {} (a weekend) by {}. Ask why the entry could not wait "
                      "for a business day.".format(r.entered_at.strftime("%A %Y-%m-%d"), r.created_by),
        }
        for r in hit.itertuples()
    ])


def jet_after_hours(df: pd.DataFrame, start_hour: int = 6, end_hour: int = 20) -> pd.DataFrame:
    """Entries keyed outside normal working hours."""
    e = entry_level(df)
    hit = e[(e["entered_hour"] < start_hour) | (e["entered_hour"] >= end_hour)]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-03",
            "test_name": "After-hours posting",
            "severity": MEDIUM,
            "reason": "Keyed at {} by {}, outside the {:02d}:00-{:02d}:00 window. "
                      "Corroborate with the user's normal working pattern."
                      .format(r.entered_at.strftime("%H:%M on %Y-%m-%d"), r.created_by,
                              start_hour, end_hour),
        }
        for r in hit.itertuples()
    ])


def jet_holiday_entry(df: pd.DataFrame) -> pd.DataFrame:
    """Entries keyed on a US federal holiday."""
    e = entry_level(df)
    hit = e[e["is_holiday"] & ~e["is_weekend"]]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-04",
            "test_name": "Holiday posting",
            "severity": MEDIUM,
            "reason": "Keyed on {}, a US federal holiday, by {}."
                      .format(r.entered_at.strftime("%Y-%m-%d"), r.created_by),
        }
        for r in hit.itertuples()
    ])


def jet_period_end_manual_revenue(df: pd.DataFrame, days: int = 3) -> pd.DataFrame:
    """Manual entries crediting revenue in the closing days of a period.

    AU-C 240 singles this pattern out: it is where revenue recognition gets
    stretched to hit a number.
    """
    rev = df[(df["account_type"] == "Revenue") & (df["credit"] > 0) & (df["source"] == "Manual")]
    if rev.empty:
        return _flags([])

    month_end = rev["posting_date"] + pd.offsets.MonthEnd(0)
    days_to_close = (month_end - rev["posting_date"]).dt.days
    hit = rev[days_to_close < days]

    seen, rows = set(), []
    for r in hit.itertuples():
        if r.entry_id in seen:
            continue
        seen.add(r.entry_id)
        rows.append({
            "entry_id": r.entry_id,
            "test_id": "JET-05",
            "test_name": "Period-end manual revenue",
            "severity": HIGH,
            "reason": "Manual credit of ${:,.2f} to {} on {}, within {} days of period close. "
                      "Obtain support and test cut-off."
                      .format(r.credit, r.account_name, r.posting_date.strftime("%Y-%m-%d"), days),
        })
    return _flags(rows)


def jet_just_under_threshold(
    df: pd.DataFrame, threshold: float = APPROVAL_THRESHOLD, window: float = 0.03
) -> pd.DataFrame:
    """Amounts sitting just beneath an approval limit.

    One entry under the limit is nothing. A pattern of them is someone
    steering around a control.
    """
    e = entry_level(df)
    lower = threshold * (1 - window)
    hit = e[(e["entry_amount"] >= lower) & (e["entry_amount"] < threshold)]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-06",
            "test_name": "Just under approval threshold",
            "severity": HIGH,
            "reason": f"${r.entry_amount:,.2f} falls within {window:.0%} beneath the ${threshold:,.0f} approval threshold. "
                      "Review for split or structured transactions."
                      ,
        }
        for r in hit.itertuples()
    ])


def jet_duplicate_entries(df: pd.DataFrame, days: int = 7) -> pd.DataFrame:
    """Near-identical entries posted within a short window.

    Matches on amount, account pair and description - the fingerprint of a
    double-posted invoice.
    """
    e = entry_level(df).merge(_entry_pairs(df), on="entry_id", how="left")
    e = e.sort_values("posting_date")

    rows = []
    key = ["entry_amount", "pair", "description"]
    for _, group in e.groupby(key, dropna=False):
        if len(group) < 2:
            continue
        ordered = group.sort_values("posting_date")
        first = ordered.iloc[0]
        for r in ordered.iloc[1:].itertuples():
            gap = (r.posting_date - first["posting_date"]).days
            if 0 <= gap <= days:
                rows.append({
                    "entry_id": r.entry_id,
                    "test_id": "JET-07",
                    "test_name": "Possible duplicate entry",
                    "severity": HIGH,
                    "reason": "Same amount (${:,.2f}), accounts and description as {} posted "
                              "{} day(s) earlier. Confirm this is not a double posting."
                              .format(r.entry_amount, first["entry_id"], gap),
                })
    return _flags(rows)


def jet_rare_account_pair(df: pd.DataFrame, max_occurrences: int = 3) -> pd.DataFrame:
    """Account combinations that almost never occur in this ledger.

    The baseline is learned from the population itself, so the test adapts to
    whatever the entity actually does rather than to a hardcoded expectation.
    """
    pairs = _entry_pairs(df)
    counts = pairs["pair"].value_counts()
    rare = counts[counts <= max_occurrences].index
    hit = pairs[pairs["pair"].isin(rare)]

    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-08",
            "test_name": "Rare account combination",
            "severity": MEDIUM,
            "reason": f"Debit {r.debit_accounts} / credit {r.credit_accounts} occurs only {int(counts[r.pair])} time(s) in the population. "
                      "Understand the business purpose."
                      ,
        }
        for r in hit.itertuples()
    ])


def jet_dormant_account(df: pd.DataFrame, dormant_days: int = 120) -> pd.DataFrame:
    """Activity in an account that had been quiet for a long stretch."""
    rows = []
    for account, chunk in df.groupby("account_code", observed=True):
        ordered = chunk.sort_values("posting_date")
        gaps = ordered["posting_date"].diff().dt.days
        woken = ordered[gaps > dormant_days]
        for r in woken.itertuples():
            rows.append({
                "entry_id": r.entry_id,
                "test_id": "JET-09",
                "test_name": "Dormant account activity",
                "severity": MEDIUM,
                "reason": f"Account {account} ({r.account_name}) had no activity for {gaps.loc[r.Index] if r.Index in gaps.index else dormant_days:.0f} days before this entry."
                          ,
            })
    return _flags(rows)


def jet_unbalanced_entry(df: pd.DataFrame, tolerance: float = 0.005) -> pd.DataFrame:
    """Entries whose debits do not equal their credits.

    A conforming ERP cannot produce these. If one appears, either a control
    failed or the extract is incomplete - and the population cannot be relied
    on until that is resolved.
    """
    e = entry_level(df)
    hit = e[e["imbalance"].abs() > tolerance]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-10",
            "test_name": "Unbalanced entry",
            "severity": HIGH,
            "reason": f"Debits ${r.total_debit:,.2f} do not equal credits ${r.total_credit:,.2f} (difference ${r.imbalance:,.2f}). "
                      "Resolve before relying on the population."
                      ,
        }
        for r in hit.itertuples()
    ])


def jet_large_value_outlier(df: pd.DataFrame, z_threshold: float = 3.5) -> pd.DataFrame:
    """Entries far larger than that account normally sees.

    Two deliberate choices here. First, a median/MAD robust z-score rather
    than mean/standard-deviation, because a handful of large entries would
    otherwise inflate the very baseline being used to judge them. Second, the
    score is computed on log10(amount): transaction values are lognormal, so
    scoring raw dollars flags the entire natural right tail of the
    distribution and buries the reviewer in false positives.

    The default threshold of 3.5 was picked empirically against the labelled
    ledger rather than chosen for roundness - see docs/tuning.md. At 3.0 the
    test returns 19 flags of which 7 are real; at 3.5, 8 flags of which 6 are
    real; at 5.0 it returns nothing at all.
    """
    rows = []
    for _account, chunk in df.groupby("account_code", observed=True):
        amounts = chunk["abs_amount"]
        amounts = amounts[amounts > 0]
        if len(amounts) < 30:
            continue
        logged = np.log10(amounts)
        median = logged.median()
        mad = (logged - median).abs().median()
        if mad == 0:
            continue
        robust_z = 0.6745 * (logged - median) / mad
        for idx, z in robust_z.items():
            if z > z_threshold:
                r = chunk.loc[idx]
                rows.append({
                    "entry_id": r["entry_id"],
                    "test_id": "JET-11",
                    "test_name": "Large value outlier",
                    "severity": MEDIUM,
                    "reason": "${:,.2f} in {} is {:.1f} robust standard deviations above the "
                              "account median of ${:,.2f} (measured on a log scale, since "
                              "transaction values are lognormal)."
                              .format(r["abs_amount"], r["account_name"], z, 10 ** median),
                })
    return _flags(rows)


def jet_unapproved_manual_entry(
    df: pd.DataFrame, approvers: tuple[str, ...] = MANUAL_JE_APPROVERS
) -> pd.DataFrame:
    """Manual journal entries keyed by someone outside the approved list.

    A segregation-of-duties test: who is allowed to touch the ledger directly?
    """
    e = entry_level(df)
    hit = e[(e["source"] == "Manual") & (~e["created_by"].isin(approvers))]
    return _flags([
        {
            "entry_id": r.entry_id,
            "test_id": "JET-12",
            "test_name": "Manual entry outside approver list",
            "severity": LOW,
            "reason": "Manual entry keyed by {}, who is not in the approved list ({}). "
                      "Confirm delegated authority."
                      .format(r.created_by, ", ".join(approvers)),
        }
        for r in hit.itertuples()
    ])


# --- registry ---------------------------------------------------------------

TestFn = Callable[[pd.DataFrame], pd.DataFrame]

REGISTRY: dict[str, tuple[str, TestFn]] = {
    "JET-01": ("Round-dollar amount", jet_round_amount),
    "JET-02": ("Weekend posting", jet_weekend_entry),
    "JET-03": ("After-hours posting", jet_after_hours),
    "JET-04": ("Holiday posting", jet_holiday_entry),
    "JET-05": ("Period-end manual revenue", jet_period_end_manual_revenue),
    "JET-06": ("Just under approval threshold", jet_just_under_threshold),
    "JET-07": ("Possible duplicate entry", jet_duplicate_entries),
    "JET-08": ("Rare account combination", jet_rare_account_pair),
    "JET-09": ("Dormant account activity", jet_dormant_account),
    "JET-10": ("Unbalanced entry", jet_unbalanced_entry),
    "JET-11": ("Large value outlier", jet_large_value_outlier),
    "JET-12": ("Manual entry outside approver list", jet_unapproved_manual_entry),
}

SEVERITY_WEIGHT = {HIGH: 3.0, MEDIUM: 2.0, LOW: 1.0}


def run_all(df: pd.DataFrame, only: list[str] | None = None) -> pd.DataFrame:
    """Run every registered test (or just `only`) and stack the flags."""
    selected = only or list(REGISTRY)
    frames = []
    for test_id in selected:
        if test_id not in REGISTRY:
            raise KeyError(f"unknown test id: {test_id}")
        _, fn = REGISTRY[test_id]
        frames.append(fn(df))
    if not frames:
        return _flags([])
    return pd.concat(frames, ignore_index=True)


def score_entries(df: pd.DataFrame, flags: pd.DataFrame) -> pd.DataFrame:
    """Roll flags up into one risk-scored row per entry.

    The score is a severity-weighted count of distinct tests that fired. It is
    deliberately simple and readable: a reviewer can reconstruct it by hand,
    which matters more at this tier than squeezing out accuracy. The ML layer
    in week 2 contributes a second, separate score rather than muddying this
    one.
    """
    e = entry_level(df)
    if flags.empty:
        e["risk_score"] = 0.0
        e["n_flags"] = 0
        e["tests_fired"] = ""
        e["reasons"] = ""
        return e.sort_values("entry_id").reset_index(drop=True)

    weights = flags["severity"].map(SEVERITY_WEIGHT).fillna(1.0)
    scored = flags.assign(weight=weights)

    agg = scored.groupby("entry_id").agg(
        risk_score=("weight", "sum"),
        n_flags=("test_id", "nunique"),
        tests_fired=("test_id", lambda s: ", ".join(sorted(set(s)))),
        reasons=("reason", lambda s: " | ".join(s)),
    ).reset_index()

    out = e.merge(agg, on="entry_id", how="left")
    out["risk_score"] = out["risk_score"].fillna(0.0)
    out["n_flags"] = out["n_flags"].fillna(0).astype(int)
    out["tests_fired"] = out["tests_fired"].fillna("")
    out["reasons"] = out["reasons"].fillna("")
    return out.sort_values(["risk_score", "entry_amount"], ascending=False).reset_index(drop=True)

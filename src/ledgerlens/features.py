"""Turn journal entries into a numeric feature matrix for the model tier.

The rule tier asks specific questions. This asks a vaguer one: how unusual is
this entry compared with everything else in the population? To answer that,
every entry has to become a row of numbers.

Two rules shaped what is in here:

1. **No feature may encode a rule's answer.** If a column said "posted on a
   weekend", the model would simply relearn JET-02 and the two tiers would
   stop being independent. Day-of-week is encoded cyclically instead, so the
   model can discover that weekends are unusual without being told.
2. **Frequency, not identity.** Account codes and user ids are categorical
   with no meaningful order. What matters is how *common* a value is, so they
   become frequency encodings.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .ingest import entry_level

#: Column order is fixed so a fitted model always sees the same matrix.
FEATURE_COLUMNS = (
    "log_amount",
    "n_lines",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "days_to_period_end",
    "posting_lag_days",
    "account_pair_freq",
    "user_freq",
    "source_freq",
    "debit_account_freq",
    "credit_account_freq",
    "amount_z_in_account",
    "round_1000_remainder",
    "digit_entropy",
)


def _freq_encode(series: pd.Series) -> pd.Series:
    """Map each value to how often it occurs, as a proportion of the whole."""
    counts = series.value_counts(normalize=True)
    return series.map(counts).astype(float).fillna(0.0)


def _cyclical(values: pd.Series, period: int):
    """Encode a cyclical quantity so that 23:00 and 00:00 are neighbours."""
    radians = 2 * np.pi * values.astype(float) / period
    return np.sin(radians), np.cos(radians)


def _digit_entropy(amounts: pd.Series) -> pd.Series:
    """Shannon entropy of the digits in each amount.

    Invented numbers are often unnaturally repetitive (5,555.00) or unnaturally
    flat. This gives the model a handle on that without hardcoding what a
    suspicious amount looks like.
    """
    def entropy(value: float) -> float:
        digits = [c for c in f"{abs(value):.2f}" if c.isdigit()]
        if not digits:
            return 0.0
        counts = pd.Series(digits).value_counts(normalize=True)
        return float(-(counts * np.log2(counts)).sum())

    return amounts.map(entropy)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the entry-level feature matrix.

    Returns a frame indexed by ``entry_id`` with exactly ``FEATURE_COLUMNS``.
    """
    entries = entry_level(df)

    # --- per-entry account structure -------------------------------------
    debits = (
        df[df["debit"] > 0].groupby("entry_id")["account_code"]
        .agg(lambda s: "|".join(sorted(set(s)))).rename("debit_accounts")
    )
    credits = (
        df[df["credit"] > 0].groupby("entry_id")["account_code"]
        .agg(lambda s: "|".join(sorted(set(s)))).rename("credit_accounts")
    )
    structure = pd.concat([debits, credits], axis=1).reset_index()
    structure["account_pair"] = (
        structure["debit_accounts"].fillna("") + ">" + structure["credit_accounts"].fillna("")
    )

    work = entries.merge(structure, on="entry_id", how="left")

    # --- amount relative to the account it landed in ----------------------
    # Robust z-score in log space, matching JET-11's reasoning: transaction
    # values are lognormal, so raw dollars would make every large-but-normal
    # entry look extreme.
    line_amounts = df[df["abs_amount"] > 0].copy()
    line_amounts["log_amount"] = np.log10(line_amounts["abs_amount"])
    stats = line_amounts.groupby("account_code")["log_amount"].agg(["median", "count"])
    mad = (
        line_amounts.assign(
            dev=(line_amounts["log_amount"]
                 - line_amounts["account_code"].map(stats["median"])).abs()
        )
        .groupby("account_code")["dev"].median()
    )
    per_entry_account = df.sort_values("abs_amount", ascending=False).groupby("entry_id").first()
    entry_account = per_entry_account["account_code"]
    entry_log_amount = np.log10(entries.set_index("entry_id")["entry_amount"].clip(lower=0.01))

    med = entry_account.map(stats["median"])
    dev = entry_account.map(mad).replace(0, np.nan)
    amount_z = (0.6745 * (entry_log_amount - med) / dev).fillna(0.0)

    # --- assemble ---------------------------------------------------------
    out = pd.DataFrame(index=work["entry_id"])
    out.index.name = "entry_id"

    out["log_amount"] = np.log10(work["entry_amount"].clip(lower=0.01)).values
    out["n_lines"] = work["n_lines"].astype(float).values

    hour_sin, hour_cos = _cyclical(work["entered_hour"], 24)
    out["hour_sin"] = hour_sin.values
    out["hour_cos"] = hour_cos.values

    dow = work["entered_at"].dt.dayofweek
    dow_sin, dow_cos = _cyclical(dow, 7)
    out["dow_sin"] = dow_sin.values
    out["dow_cos"] = dow_cos.values

    month_end = work["posting_date"] + pd.offsets.MonthEnd(0)
    out["days_to_period_end"] = (month_end - work["posting_date"]).dt.days.astype(float).values

    lag = (work["posting_date"].dt.normalize() - work["entered_at"].dt.normalize()).dt.days
    out["posting_lag_days"] = lag.astype(float).values

    out["account_pair_freq"] = _freq_encode(work["account_pair"]).values
    out["user_freq"] = _freq_encode(work["created_by"]).values
    out["source_freq"] = _freq_encode(work["source"]).values
    out["debit_account_freq"] = _freq_encode(work["debit_accounts"].fillna("")).values
    out["credit_account_freq"] = _freq_encode(work["credit_accounts"].fillna("")).values

    out["amount_z_in_account"] = amount_z.reindex(out.index).fillna(0.0).values
    out["round_1000_remainder"] = (work["entry_amount"] % 1000).values
    out["digit_entropy"] = _digit_entropy(work["entry_amount"]).values

    return out[list(FEATURE_COLUMNS)].astype(float)

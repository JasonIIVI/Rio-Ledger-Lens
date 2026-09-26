"""Load a GL extract, validate it against the schema, derive working columns.

Anything that reads a ledger goes through here, so the tests downstream can
assume the derived columns exist and the dtypes are right. When the
QuickBooks connector lands it maps QBO's field names into this shape and
hands off to :func:`prepare`.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .schema import DERIVED_COLUMNS, REQUIRED_COLUMNS, is_us_holiday


class SchemaError(ValueError):
    """Raised when an extract is missing columns the tests depend on."""


def validate(df: pd.DataFrame) -> None:
    """Raise :class:`SchemaError` if required columns are absent."""
    missing: list[str] = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise SchemaError(
            "GL extract is missing required column(s): {}. Expected: {}".format(
                ", ".join(sorted(missing)), ", ".join(REQUIRED_COLUMNS)
            )
        )


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Validate, coerce dtypes, and add the derived columns tests rely on."""
    validate(df)
    out = df.copy()

    out["posting_date"] = pd.to_datetime(out["posting_date"])
    out["entered_at"] = pd.to_datetime(out["entered_at"])
    for col in ("debit", "credit"):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    for col in ("line_no", "fiscal_year", "period"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("int64")
    for col in ("entry_id", "account_code", "account_name", "account_type",
                "description", "source", "created_by"):
        out[col] = out[col].astype("string")

    out["amount"] = out["debit"] - out["credit"]
    out["abs_amount"] = out["amount"].abs()
    out["entered_hour"] = out["entered_at"].dt.hour
    out["entered_dow"] = out["entered_at"].dt.dayofweek
    out["is_weekend"] = out["entered_dow"] >= 5
    out["is_holiday"] = out["entered_at"].dt.date.map(is_us_holiday)
    out["posting_lag_days"] = (
        out["posting_date"].dt.normalize() - out["entered_at"].dt.normalize()
    ).dt.days

    missing_derived = [c for c in DERIVED_COLUMNS if c not in out.columns]
    if missing_derived:  # pragma: no cover - guards against future edits
        raise SchemaError("failed to derive: {}".format(", ".join(missing_derived)))
    return out


def load_csv(path: str | Path) -> pd.DataFrame:
    """Read a GL CSV from disk and prepare it.

    Text columns are read as text: an account code such as 0100 must come
    back as the string it is, not as the integer 100.
    """
    text = {column: "string" for column, kind in REQUIRED_COLUMNS.items() if kind == "string"}
    df = pd.read_csv(path, dtype=text)
    return prepare(df)


def load_labels(path: str | Path) -> pd.DataFrame:
    """Read the ground-truth label file produced by the generator."""
    df = pd.read_csv(path)
    df["is_anomaly"] = df["is_anomaly"].astype(bool)
    df["anomaly_type"] = df["anomaly_type"].fillna("").astype("string")
    return df


def entry_level(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse GL lines to one row per journal entry.

    Several tests reason about the entry as a whole (is it balanced, what is
    its total value, which accounts does it touch) rather than about
    individual lines.
    """
    grouped = df.groupby("entry_id", as_index=False).agg(
        posting_date=("posting_date", "first"),
        entered_at=("entered_at", "first"),
        fiscal_year=("fiscal_year", "first"),
        period=("period", "first"),
        source=("source", "first"),
        created_by=("created_by", "first"),
        description=("description", "first"),
        total_debit=("debit", "sum"),
        total_credit=("credit", "sum"),
        n_lines=("line_no", "count"),
        entered_hour=("entered_hour", "first"),
        is_weekend=("is_weekend", "first"),
        is_holiday=("is_holiday", "first"),
    )
    grouped["entry_amount"] = grouped[["total_debit", "total_credit"]].max(axis=1)
    grouped["imbalance"] = (grouped["total_debit"] - grouped["total_credit"]).round(2)
    return grouped

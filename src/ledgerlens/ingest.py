"""Load a GL extract, validate it against the schema, derive working columns.

Anything that reads a ledger goes through here, so the tests downstream can
assume the derived columns exist and the dtypes are right. When the
QuickBooks connector lands it maps QBO's field names into this shape and
hands off to :func:`prepare`.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from .schema import DERIVED_COLUMNS, REQUIRED_COLUMNS, is_us_holiday

#: Written beside a ledger by a source that knows what the ledger is (a
#: QuickBooks pull): the review store then keys its rows by that identity
#: rather than by the CSV's digest, which a re-pull would change.
IDENTITY_SUFFIX = ".identity.json"
_QBO_ID = re.compile(r"^qbo:[A-Za-z0-9][A-Za-z0-9_.-]*$")

_AMOUNT_COLUMNS = ("debit", "credit")
_DATE_COLUMNS = ("posting_date", "entered_at")
_INT_COLUMNS = ("line_no", "fiscal_year", "period")


class SchemaError(ValueError):
    """Raised when an extract is missing columns the tests depend on."""


class IdentityError(ValueError):
    """An identity sidecar exists but cannot be trusted."""


def _canonical(column: str, value):
    """One value as the digest sees it, the same from memory and from a CSV."""
    if pd.isna(value):
        return None  # NA, NaT, NaN and (below) an empty string are one token
    if column in _AMOUNT_COLUMNS:
        text = f"{float(value):.2f}"  # a whole-dollar int64 column hashes like a float one
        return "0.00" if text == "-0.00" else text
    if column in _DATE_COLUMNS:
        return pd.Timestamp(value).isoformat()
    if column in _INT_COLUMNS:
        return int(value)
    return str(value) or None


def ledger_digest(lines: pd.DataFrame) -> str:
    """sha256 of a prepared ledger's content in a canonical form.

    The required columns only, each row as a JSON list (so a ``|`` or a
    newline inside a description cannot move a field boundary), amounts to
    the cent, dates in ISO form, rows sorted as text (so row order and
    duplicate keys do not matter). Not the CSV's bytes: pandas writes floats
    differently across versions, and the same ledger has to hash the same on
    every Python CI runs. Every eval row records it, and it is half of a
    ledger's identity in the review store (see :func:`ledger_identity`), so
    a change to this form is a schema-level event: it renames every ledger.
    """
    columns = list(REQUIRED_COLUMNS)
    rows = sorted(
        json.dumps([_canonical(c, v) for c, v in zip(columns, row)],
                   ensure_ascii=False, separators=(",", ":"))
        for row in lines[columns].itertuples(index=False, name=None)
    )
    digest = hashlib.sha256(json.dumps(columns).encode("utf-8") + b"\n")
    for row in rows:
        digest.update(row.encode("utf-8") + b"\n")
    return digest.hexdigest()


def identity_path(ledger_path: str | Path) -> Path:
    """Where a ledger's identity sidecar sits: ``data/ledger.identity.json`` beside the CSV."""
    return Path(ledger_path).with_suffix(IDENTITY_SUFFIX)


def ledger_identity(lines: pd.DataFrame, path: str | Path | None = None) -> str:
    """The key the review store files this ledger's notes and decisions under.

    ``csv:<sha256>`` of the prepared frame's canonical form, or the id named
    by a sidecar beside the file (``qbo:<realm_id>``, written by a
    QuickBooks pull, stable across re-pulls). A sidecar that exists but is
    malformed is an error, never a fallback: filing a QuickBooks ledger's
    review under a CSV digest would split its history in two.
    """
    if path is not None:
        sidecar = identity_path(path)
        if sidecar.exists():
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise IdentityError(f"{sidecar} cannot be read as JSON ({exc})") from exc
            ledger_id = payload.get("ledger_id") if isinstance(payload, dict) else None
            if not isinstance(ledger_id, str) or not _QBO_ID.match(ledger_id):
                raise IdentityError(
                    f'{sidecar} must hold {{"ledger_id": "qbo:<realm_id>"}}, got {ledger_id!r}'
                )
            return ledger_id
    return "csv:" + ledger_digest(lines)


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

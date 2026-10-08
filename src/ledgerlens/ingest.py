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

import numpy as np
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
#: Text a test keys on. A blank here was never an error: a blank account code was a
#: TypeError in the rules, a blank entry id dropped its line from the entry, and a blank
#: account type, source or user quietly took an entry out of JET-05 or JET-12. Account
#: names and descriptions are only shown, so they may be blank, and a blank amount is 0.
_KEYED_TEXT = ("entry_id", "account_code", "account_type", "source", "created_by")
#: A time of day followed by an offset ("Z", "+05:30", "-0700"). Every time here is
#: naive, the company's own clock; one with an offset cannot be put on that clock without
#: knowing the company's zone, and mixed with naive ones it was a TypeError in the load.
_OFFSET = re.compile(r"\d:\d\d(?::\d\d(?:\.\d+)?)?\s*(?:Z|[+-]\d\d:?\d\d)$", re.IGNORECASE)


class SchemaError(ValueError):
    """Raised when an extract is missing columns the tests depend on."""


class IdentityError(ValueError):
    """An identity sidecar exists but cannot be trusted."""


def plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def quote(value: object) -> str:
    """A value from a file as a message quotes it: ``ascii``, cut to a line's worth.

    Whoever wrote the file chose this text, and the message is shown in a terminal,
    a CI log and the dashboard. ``ascii`` escapes every character outside ASCII, the
    control and invisible ones among them, so it shows what the eye would miss: a
    stray space, a variation selector, an id read as a number beside the same id read
    as text. ``repr`` would keep an invisible character Python calls printable, and
    print a code point assigned after this Python's Unicode tables one way and a newer
    Python's another.
    """
    text = ascii(value)
    if isinstance(value, str) and (len(value) > 48 or len(text) > 60):
        # Cut the text, then quote it: the quote closes, no escape is cut in half, and the
        # length says that two values alike this far are not being called the same. The cut
        # is by what the escaped text takes up, not by a count of characters: ascii()
        # writes a Latin-1 letter as four, a CJK character as six and an emoji as ten.
        prefix = value[:48]
        while prefix and len(ascii(prefix)) > 50:
            prefix = prefix[:-1]
        return f"{ascii(prefix)}... ({len(value)} characters)"
    return text


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
            if not isinstance(ledger_id, str) or not _QBO_ID.fullmatch(ledger_id):
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


def _blank(raw: pd.Series) -> np.ndarray:
    """Missing, or text that is only whitespace."""
    spaces = raw.astype("string").str.strip().eq("").fillna(False)
    return (raw.isna() | spaces).to_numpy(dtype=bool)


def _rows(column: str, what: str, mask: np.ndarray, raw: pd.Series | None = None) -> str:
    """One problem, in data rows rather than file lines: pandas skips a blank line and a
    quoted cell can span two, so a line number would send the reader to a line that is fine."""
    first = int(mask.argmax())
    text = "{} {} on {}, the first is data row {}".format(
        column, what, plural(int(mask.sum()), "row", "rows"), first + 1)
    # Quoted as text whatever it was read as: pandas reads "inf" and "1.5" as floats.
    return text if raw is None else f"{text} ({quote(str(raw.iloc[first]))})"


def _read_cells(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Each date, whole-number and amount column parsed, or a SchemaError naming every
    column that holds a cell no test could use, with its count and first row.

    The parse is the one :func:`prepare` always made, with failures turned into NaT and
    NaN so they can be counted rather than raised one at a time (or, for an amount,
    quietly made 0.00), so a ledger with nothing wrong parses exactly as before.
    """
    parsed: dict[str, pd.Series] = {}
    problems: list[str] = []
    for column in REQUIRED_COLUMNS:  # schema order, so the message reads like the file
        raw = df[column]
        blank = _blank(raw)
        if column in _DATE_COLUMNS:
            offset = np.zeros(len(raw), dtype=bool)
            if not pd.api.types.is_datetime64_any_dtype(raw):
                offset = raw.map(lambda v: isinstance(v, str) and bool(_OFFSET.search(v.strip())))
                offset = offset.to_numpy(dtype=bool)
            values = pd.to_datetime(raw.mask(blank | offset), errors="coerce")
            if not pd.api.types.is_datetime64_dtype(values):  # aware, or a mix of zones
                offset = ~blank
                values = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
            unreadable = values.isna().to_numpy() & ~blank & ~offset
            if blank.any():
                problems.append(_rows(column, "is blank", blank))
            if offset.any():
                problems.append(_rows(column, "carries a time zone offset", offset, raw))
            if unreadable.any():
                problems.append(_rows(column, "is not a readable date", unreadable, raw))
            parsed[column] = values
        elif column in _INT_COLUMNS or column in _AMOUNT_COLUMNS:
            values = pd.to_numeric(raw.mask(blank), errors="coerce")
            number = values.to_numpy(dtype=float, na_value=np.nan)
            readable = np.isfinite(number)
            if column in _INT_COLUMNS:
                whole = readable & (np.where(readable, number, 0.0) % 1 == 0)
                if blank.any():
                    problems.append(_rows(column, "is blank", blank))
                if (~blank & ~whole).any():
                    problems.append(_rows(column, "is not a whole number", ~blank & ~whole, raw))
                parsed[column] = values
            else:  # a blank amount is a zero; "abc", "1,234.00" and "inf" are not
                if (~blank & ~readable).any():
                    problems.append(_rows(column, "is not a number", ~blank & ~readable, raw))
                parsed[column] = values.fillna(0.0)
        elif column in _KEYED_TEXT and blank.any():
            problems.append(_rows(column, "is blank", blank))
    if problems:
        raise SchemaError("cells no test can read: " + "; ".join(problems))
    return parsed


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Validate, coerce dtypes, and add the derived columns tests rely on."""
    validate(df)
    parsed = _read_cells(df)
    out = df.copy()

    out["posting_date"] = parsed["posting_date"]
    out["entered_at"] = parsed["entered_at"]
    for col in ("debit", "credit"):
        out[col] = parsed[col]
    for col in ("line_no", "fiscal_year", "period"):
        out[col] = parsed[col].astype("int64")
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


#: What a label's is_anomaly cell may hold. "1.0" and "0.0" are what pandas writes for a
#: boolean column that once held a blank; anything else is refused rather than guessed.
_TRUTH = {"true": True, "false": False, "1": True, "0": False, "1.0": True, "0.0": False}


def load_labels(path: str | Path) -> pd.DataFrame:
    """Read the ground-truth label file produced by the generator."""
    # Ids as text, like load_csv: a numeric-looking id must not lose its zeros.
    df = pd.read_csv(path, dtype={"entry_id": "string", "is_anomaly": "string"})
    # Read strictly. astype(bool) made a blank cell, "no" and "false " all True, and
    # every such row then counted as an injected anomaly.
    truth = df["is_anomaly"].str.strip().str.lower().map(_TRUTH)
    unread = truth.isna()
    if unread.any():
        # Counted in data rows, not file lines: pandas skips a blank line, and a quoted
        # cell can span two, so a line number would send the reader to a line that is fine.
        first = int(unread.to_numpy().argmax()) + 1
        raise ValueError(
            f"is_anomaly must be true/false or 1/0 on every row; {int(unread.sum())} row(s) are "
            f"blank or something else, the first is data row {first}")
    df["is_anomaly"] = truth.astype(bool)
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

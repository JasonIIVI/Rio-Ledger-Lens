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
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.tseries.api import guess_datetime_format

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
#: TypeError in the rules, a blank entry id dropped its line from the entry, a blank
#: account type or source quietly took an entry out of JET-05 or JET-12, and a blank user
#: put a manual entry into JET-12 as keyed by <NA>. Account names are only shown, and a
#: blank description is a value JET-07 matches like any other, so both may be blank; a
#: blank amount is 0.
_KEYED_TEXT = ("entry_id", "account_code", "account_type", "source", "created_by")
#: A time of day followed by a zone: "Z", a name ("UTC", "EST", "CEST"), an offset ("+05:30",
#: "-0700", an hour alone as PostgreSQL writes "-08"), or a name and an offset ("UTC+1"); after
#: an AM or PM too, and before a year, as Java writes one ("Mon Jan 01 09:23:30 CET 2024").
#: Every time here is naive, the company's own clock; one with a zone cannot be put on that
#: clock without knowing the company's zone. With a zone on every row it was a TypeError in
#: the load, beside naive ones a pandas format error; and pandas 2 reads the machine's own
#: zone name (EST on a New York machine) and drops any other, so a name is caught here, where
#: this pattern sees it, before pandas does. AM and PM are two letters and are not names, and
#: a weekday or HRS after the time is not one; any other word of three to five letters there
#: is taken for a zone, so a word this list does not know is refused in a line, never dropped
#: unsaid. A name written some other way (ET, "EST.", "Mon EST", ", EST") pandas keeps as text
#: in the format it guesses, and a format that keeps a word is not taken (_format_of).
_NOT_ZONES = ("MON", "TUE", "TUES", "WED", "WEDS", "THU", "THUR", "THURS", "FRI", "SAT", "SUN", "HRS")
_OFFSET = re.compile(r"\d:\d\d(?::\d\d(?:\.\d+)?)?\s*(?:[AP]\.?M\.?\s*)?"
                     r"(?!(?:" + "|".join(_NOT_ZONES) + r")\b)"
                     r"(?:Z|[A-Z]{3,5}|(?:UTC|GMT)?\s*[+-]\d\d?(?::?\d\d)?)(?:\s+\d{4})?$",
                     re.IGNORECASE)
#: am, pm, a.m., p.m., a. m., or a or p alone, after a time: written " AM" or " PM" before the
#: parse. pandas writes a lower-case or dotted one into the format it guesses as text beside a
#: 24-hour clock ("2:05 pm" read in "%H:%M pm" is 02:05), and pandas 2 takes the M of "P.M."
#: for a zone.
_MERIDIEM = re.compile(r"(?<=\d)\s*([AaPp])\.?\s?[Mm]?\.?(?![A-Za-z])")
#: A time of day, with its seconds and their fraction: where a two-digit year is not.
_TIME = re.compile(r"\d{1,2}:\d\d(?::\d\d(?:[.,]\d+)?)?")
#: Two digits standing alone: where a two-digit year can be, once the time is set aside.
_TWO_DIGITS = re.compile(r"(?<!\d)\d\d(?!\d)")
#: A word of three letters or more: a month or a weekday, which pandas guesses a format from only
#: as strftime writes it (Jan), though it reads any case (JAN, jan).
_LETTERS = re.compile(r"[A-Za-z]{3,}")
#: AM or PM ending a cell, with any spaces after it.
_LAST_MERIDIEM = re.compile(r" [AP]M(\s*)$")
#: What a format may keep as text between its fields: "at" before the time, the h of 09h15.
_JOINS = re.compile(r" at |(?<=%H)h(?=%M)")
#: A letter a guessed format keeps as text, once its directives, ISO's T and the joins are set aside.
_WORD = re.compile(r"[A-Za-z]")
#: Words pandas reads as the clock: a ledger dated "today" would be dated the day it was read.
_CLOCK_WORDS = frozenset({"now", "today"})
#: A date written as a number, read by its count of digits. Any other count is refused, not
#: guessed: read as a number it was nanoseconds since 1970, and read as text pandas took
#: six-digit YYMMDD dates for days spread over 2002 to 2031.
_DIGIT_FORMATS = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}


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


def _is_number(value: object) -> bool:
    """A number in a date column, whatever holds it: an int64 column, a float one (a blank
    elsewhere), Python ints in an object column, or ASCII digits as text (one cell of another
    kind makes a CSV column text, and pandas then guessed six digits as DDMMYY)."""
    if isinstance(value, str):
        text = value.strip()
        return text.isascii() and text.isdigit()
    return (isinstance(value, (int, float, np.integer, np.floating))
            and not isinstance(value, (bool, np.bool_)) and not pd.isna(value))


def _spelled(numbers: pd.Series) -> pd.Series:
    """Numbers in a date column as the dates their digits spell (see ``_DIGIT_FORMATS``),
    NaT where they spell none."""
    def digits(value) -> str:
        if isinstance(value, str):
            return value.strip()  # as written: a leading zero is a digit, as it is not in a number
        number = float(value)
        return str(int(number)) if np.isfinite(number) and number.is_integer() and number >= 0 else ""
    text = pd.Series([digits(v) for v in numbers], index=numbers.index, dtype=object)
    out = pd.Series(pd.NaT, index=numbers.index, dtype="datetime64[ns]")
    for size, fmt in _DIGIT_FORMATS.items():
        group = (text.str.len() == size).to_numpy()
        if group.any():
            out.iloc[group] = pd.to_datetime(text[group], format=fmt, errors="coerce").to_numpy()
    return out


def _has_zone(value: object) -> bool:
    """A time with a zone: text ending in one (see ``_OFFSET``), or a datetime built with one.
    pandas coerces a column of naive datetimes with one zoned value among them into NaT for
    hundreds of rows rather than raising, so the zone is looked for before the parse."""
    if isinstance(value, str):
        return bool(_OFFSET.search(value.strip()))
    return getattr(value, "tzinfo", None) is not None


def _dates(cells: pd.Series, **how) -> pd.Series | None:
    """``to_datetime`` with failures as NaT, or None for cells in several zones (pandas 2
    returns objects for those, with a warning; pandas 3 raises). pandas' warnings are kept from
    the user: its guess warns about dayfirst=False on 16/01/2024, which no one can pass here."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            values = pd.to_datetime(cells, errors="coerce", **how)
    # AttributeError: pandas 2 on a zone name it half reads (the machine's own, beside naive
    # times) in format="mixed"; _OFFSET now catches those first, and this stays as a backstop
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None
    return values if pd.api.types.is_datetime64_any_dtype(values) else None


def _guess(cell: str, dayfirst: bool = False) -> str | None:
    """pandas' guess at a cell's format, without its warnings (that it dropped a zone name, or
    read a cell day first with dayfirst=False, which nobody loading a ledger can pass)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return guess_datetime_format(cell, dayfirst=dayfirst)


def _guess_any_case(cell: str, dayfirst: bool) -> str | None:
    """pandas' guess, or its guess with each word written Jan, Mon: it takes a format from a
    month or a weekday only as strftime writes it, and reads any case once given one."""
    fmt = _guess(cell, dayfirst)
    if fmt is None:
        titled = _LETTERS.sub(lambda m: m.group().title(), cell)
        fmt = _guess(titled, dayfirst) if titled != cell else None
    return fmt


def _short_year(cell: str, dayfirst: bool) -> str | None:
    """The format of a date whose year is two digits (02/01/24, 02-Jan-24), for which pandas
    guesses none: the last two digits standing alone outside the time of day are widened to a
    year and guessed, and the format names them as two digits again."""
    found = list(_TWO_DIGITS.finditer(_TIME.sub(lambda m: "#" * len(m.group()), cell)))
    if not found:
        return None
    two = found[-1]
    wide = cell[:two.start()] + "20" + two.group() + cell[two.end():]
    fmt = _guess_any_case(wide, dayfirst)
    return fmt.replace("%Y", "%y") if fmt is not None and fmt.count("%Y") == 1 else None


def _format_of(cell: str, dayfirst: bool = False) -> str | None:
    """The one format a date column is read in, taken from its first cell: pandas' guess, or
    where pandas guesses none, its guess with the AM or PM set aside (it tries a 24-hour clock
    on "2:05 PM" and on 12 AM) or with a two-digit year widened, the format then naming them as
    written; a month or weekday in capitals is guessed as Jan or Mon would be. None where no
    format can be taken, or the one guessed reads a zone (pandas 2 guesses %Z for the machine's
    own) or keeps a word of the cell as text ("%H:%M:%S ET", "Sun %m/%d/%Y", "%b %dnd, %Y"):
    such a column is refused, where pandas would read it cell by cell, choosing day or month
    first for each cell and dating a bare time by the day it was read."""
    fmt = _guess_any_case(cell, dayfirst)
    if fmt is None:
        end = _LAST_MERIDIEM.search(cell)
        body, meridiem = (cell[:end.start()], " %p" + end.group(1)) if end else (cell, "")
        fmt = _guess_any_case(body, dayfirst) or _short_year(body, dayfirst)
        if fmt is not None and meridiem:
            fmt = fmt.replace("%H", "%I") + meridiem if "%H" in fmt else None
    if (fmt is None or "%Z" in fmt or "%z" in fmt
            or _WORD.search(re.sub(r"%[A-Za-z]", "", _JOINS.sub("", fmt)).replace("T", ""))):
        return None
    return fmt


def _day_first(column: str, cells: pd.Series, skip: np.ndarray, at: int, fmt: str | None) -> list[str]:
    """When every cell reads day first, the first (say 02/01/2024) was most likely misread: say so."""
    day_first = _format_of(str(cells.iloc[at]), dayfirst=True)
    if day_first and day_first != fmt:
        whole = _dates(cells[~skip], format=day_first)
        if whole is not None and whole.notna().all():
            return [f"{column}: every cell reads as {day_first} (day first): write dates as YYYY-MM-DD"]
    return []


def _another_format(column: str, raw: pd.Series, cells: pd.Series, skip: np.ndarray,
                    readable: np.ndarray, at: int, fmt: str | None) -> list[str]:
    """Cells that are dates, but not in the format the column is read in, the one taken from
    data row ``at``: named against that row, with the format, and the day-first hint."""
    read_as = f", read as {fmt}" if fmt else ""
    found = [_rows(column, f"is in a different date format from data row {at + 1} ({quote(str(raw.iloc[at]))}{read_as})",
                   readable, raw)]
    return found + _day_first(column, cells, skip, at, fmt)


def _read_dates(column: str, raw: pd.Series, blank: np.ndarray) -> tuple[pd.Series, list[str]]:
    """One date column read in the one format of its first text cell, and what in it no test
    could read (see _read_cells). pandas takes that format itself where it can, so a ledger it
    reads reads exactly as before; where it cannot (a two-digit year, an afternoon time, 12 AM),
    it is given the format _format_of takes, and is never left to read the column cell by cell.
    The cell as written is what a message quotes."""
    # the cells as Python objects, made once: pandas 3 holds a CSV's text in Arrow, and every
    # Python loop over the column itself pays to convert each cell again
    objs = raw.to_numpy(dtype=object)
    text = np.fromiter((isinstance(v, str) for v in objs), dtype=bool, count=len(objs))
    number = np.array([_is_number(v) for v in objs], dtype=bool)
    clock = np.array([isinstance(v, str) and v.strip().lower() in _CLOCK_WORDS for v in objs], dtype=bool)

    # the cell the format comes from: the first text cell that is not blank, a number, a word for
    # the clock or a time with a zone (only the cells before it are checked for a zone here)
    candidates = np.flatnonzero(text & ~blank & ~number & ~clock)

    def lead(cells) -> int | None:
        return next((int(i) for i in candidates if not _has_zone(cells[i])), None)

    cells, final = raw, objs
    at = lead(objs)
    # am or pm is written AM or PM when the cell the format comes from has one written otherwise
    # (pandas reads AM, PM, am and pm alike once given %p); zones are then looked for in the
    # rewritten cells ("9:15 p. m. EST"), which the rewrite cannot make a number or a clock word
    meridiem = None if at is None else _MERIDIEM.search(objs[at])
    if meridiem and meridiem.group() not in (" AM", " PM"):
        final = [_MERIDIEM.sub(lambda m: f" {m.group(1).upper()}M", v) if isinstance(v, str) else v for v in objs]
        cells = pd.Series(final, index=raw.index, dtype=object)
        at = lead(final)
    offset = np.array([_has_zone(v) for v in final], dtype=bool)
    skip = blank | number | offset | clock
    unreadable = clock
    found: list[str] = []
    values = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]")
    fmt = None if at is None else _format_of(cells.iloc[at])
    if at is not None and fmt is None:
        found.append(f"{column} is in no date format that can be read from data row {at + 1} "
                     f"({quote(str(raw.iloc[at]))}): write dates as YYYY-MM-DD")
        found.extend(_day_first(column, cells, skip, at, None))
    elif (~skip).any():
        first = int((~skip).argmax())
        own = at == first and _guess(cells.iloc[at]) == fmt
        parsed = _dates(cells.mask(skip), format=None if own else fmt)
        if fmt and "%B" in fmt and parsed is not None and parsed.isna().any():
            # May is %b and %B alike, and pandas takes %B: a Jan, Feb ... column with May in its
            # first cell is read with %b if that reads more of it
            short = _dates(cells.mask(skip), format=fmt.replace("%B", "%b"))
            if short is not None and short.isna().sum() < parsed.isna().sum():
                parsed, fmt = short, fmt.replace("%B", "%b")
        if parsed is None or isinstance(parsed.dtype, pd.DatetimeTZDtype):
            # a zone pandas read that the checks above did not see, beside naive times or
            # other zones: one problem, not a count of rows
            found.append(f"{column} mixes times with and without a time zone, or in several zones; "
                         f"the first is data row {first + 1} ({quote(str(raw.iloc[first]))})")
        else:
            values = parsed
            failed = values.isna().to_numpy() & ~skip
            if failed.any():
                # a cell written another way fails with the unreadable ones: tell them apart
                alone = _dates(cells[failed], format="mixed")
                readable = np.zeros(len(raw), dtype=bool)
                readable[failed] = False if alone is None else alone.notna().to_numpy()
                if readable.any():
                    found.extend(_another_format(column, raw, cells, skip, readable, first if at is None else at,
                                                 fmt))
                unreadable = unreadable | (failed & ~readable)
    problems = [_rows(column, "is blank", blank)] if blank.any() else []
    if offset.any():
        problems.append(_rows(column, "carries a time zone offset", offset, raw))
    problems.extend(found)
    if unreadable.any():
        problems.append(_rows(column, "is not a readable date", unreadable, raw))
    if number.any():
        spelled = _spelled(cells[number])
        bad = np.zeros(len(raw), dtype=bool)
        bad[number] = spelled.isna().to_numpy()
        if bad.any():
            problems.append(_rows(column, "is a number, not a date (write YYYY-MM-DD)", bad, raw))
        values = values.copy()
        values.iloc[np.flatnonzero(number & ~bad)] = spelled[~spelled.isna()].to_numpy()
    return values, problems


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
            parsed[column], found = _read_dates(column, raw, blank)
            problems.extend(found)
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
    # Only an empty field is missing. pandas' default list made "NA" (a user's initials) a
    # blank cell, and Excel's "#N/A" in an amount a quiet 0.00.
    df = pd.read_csv(path, dtype=text, keep_default_na=False, na_values=[""])
    return prepare(df)


#: What a label's is_anomaly cell may hold. "1.0" and "0.0" are what pandas writes for a
#: boolean column that once held a blank; anything else is refused rather than guessed.
_TRUTH = {"true": True, "false": False, "1": True, "0": False, "1.0": True, "0.0": False}


def load_labels(path: str | Path) -> pd.DataFrame:
    """Read the ground-truth label file produced by the generator."""
    # Ids as text, like load_csv: a numeric-looking id must not lose its zeros.
    # and only an empty field is missing, as load_csv reads it: an entry id "NA" is an id
    df = pd.read_csv(path, dtype={"entry_id": "string", "is_anomaly": "string"},
                     keep_default_na=False, na_values=[""])
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

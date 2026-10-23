"""Materiality, and where an entry's amount stands against it.

``Materiality`` holds the auditor's judgement of how large a misstatement has to be to
matter: an overall figure, the share of it the work is planned against (performance
materiality), and the share below which a misstatement would be clearly trivial (the
framework is AU-C 320 and 450). The overall figure is given for each run, never read from a
file and never defaulted; the two shares default to 75% and 5%. None of them is tuned
against labels (rule 7): this module applies them and does not choose them.

An entry's materiality band says where its amount stands against the three figures. It is
a label beside the risk score and never part of it (rule 3): nothing here ranks, scores,
flags or drops an entry. Materiality is set for one year's financial statements, so a band
is given for one fiscal year's entries, and an entry of another year is marked as such.

This module imports nothing from ``summary``: ``sampling`` imports ``summary`` for its inert
text, so the figures had to sit below both for ``summary``, the workpaper and the dashboard
to reach them.
"""

from __future__ import annotations

import numbers
import operator
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from fractions import Fraction

import pandas as pd

from .ingest import quote

DEFAULT_PERFORMANCE_SHARE = Decimal("0.75")
DEFAULT_TRIVIAL_SHARE = Decimal("0.05")
#: As for an approval limit (``controls.LIMIT_CEILING``): no materiality is this large, and
#: below it a figure in whole cents has at most 14 digits, which a float holds exactly.
CEILING = Decimal(10) ** 12
_CENT = Decimal("0.01")
#: A share is a percentage to four places at most; finer is a typing slip, and a share
#: written 1E-999990 would print about a million zeros as a percentage.
_SHARE_STEP = Decimal("0.000001")
#: A number given on the command line, not a document.
_MAX_DIGITS = 40

class SamplingError(ValueError):
    """Materiality, or a sampling parameter, that cannot be used."""

def _decimal(value: object, name: str) -> tuple[Decimal | None, str | None]:
    """``(value as a Decimal, None)``, or ``(None, why it is not a finite number)``.

    Read as written: a string is parsed by Decimal, never through a float, so 0.1 is
    0.1. A number built in code is read by its value: an integer exactly, a float (numpy's
    float64 among them, whose repr in numpy 2 is ``np.float64(...)``) by the repr of its
    float, and a float narrower than that (numpy's float32 or float16) by its own text, so
    a float32 0.05 is 0.05 and not the 0.05000000074505806 a float holds of it.
    """
    if isinstance(value, bool):
        return None, f"{name} must be a number, got {value!r}"
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, numbers.Integral):
        number = Decimal(operator.index(value))
    elif isinstance(value, numbers.Real):
        narrow = getattr(getattr(value, "dtype", None), "itemsize", 8) < 8
        try:
            number = Decimal(str(value) if narrow else repr(float(value)))
        except OverflowError:  # a Fraction too large for a float
            return None, f"{name} {quote(str(value))} has more digits than it needs"
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > _MAX_DIGITS:
            return None, f"{name} {quote(value)} has more digits than it needs"
        try:
            number = Decimal(text)
        except InvalidOperation:
            hint = ' (write 50000, not "50,000")' if "," in text else ""
            return None, f"{name} must be a number, got {quote(value)}{hint}"
    else:
        return None, f"{name} must be a number, got {quote(value)}"
    if not number.is_finite():
        return None, f"{name} must be a finite number, got {quote(str(value))}"
    return number, None

def _whole(value: object, name: str, low: int, high: int | None = None) -> int:
    """``value`` as a whole number from ``low`` (to ``high``), or a SamplingError.

    A string is read as the command line passes it, so a bad --size or --seed is one
    ``error:`` line like every other refusal, not argparse's usage text.
    """
    number = None
    if isinstance(value, numbers.Integral) and not isinstance(value, bool):
        number = operator.index(value)
    elif isinstance(value, str):
        text = value.strip()
        if 0 < len(text) <= 20 and text.lstrip("+-").isdigit():
            try:
                number = int(text)
            except ValueError:  # isdigit passes "+-5", a superscript 2 and a circled 1; int does not
                number = None
    span = f"of {low} or more" if high is None else f"from {low} to {high}"
    if number is None or number < low or (high is not None and number > high):
        raise SamplingError(f"{name} must be a whole number {span}, got {quote(value)}")
    return number

def percent(share: Decimal) -> str:
    return f"{(share * 100).normalize():f}%"


def dollars(cents: int | Fraction) -> str:
    """Whole cents as dollars; an exact fraction of a cent rounded to the cent for display."""
    return f"${float(Fraction(cents)) / 100:,.2f}"


def _share_problem(share: Decimal, name: str, below: Decimal | None) -> str | None:
    if share <= 0 or share >= 1:
        return f"{name} must be above 0 and below 1, got {share}"
    if share != share.quantize(_SHARE_STEP):
        return f"{name} must be given to at most six decimal places, got {share}"
    if below is not None and share >= below:
        return f"{name} ({share}) must be below the performance share ({below})"
    return None

@dataclass(frozen=True)
class Materiality:
    """Overall materiality, and the two figures derived from it.

    ``performance`` (the share of overall the work is planned against, below overall as
    AU-C 320 has it) and ``clearly_trivial`` (the share below which a misstatement would be
    clearly trivial) are rounded down to the cent: the lower figure is the cautious one, a
    larger sample and more entries examined in full. Every problem with the three is named
    in one ``SamplingError``.
    """

    overall: Decimal
    performance_share: Decimal = DEFAULT_PERFORMANCE_SHARE
    trivial_share: Decimal = DEFAULT_TRIVIAL_SHARE

    def __post_init__(self) -> None:
        problems = []
        overall, why = _decimal(self.overall, "materiality")
        if why:
            problems.append(why)
        elif overall <= 0:
            problems.append(f"materiality must be above 0, got {overall}")
        elif overall >= CEILING:
            problems.append(f"materiality {overall} is not below 1,000,000,000,000")
        elif overall != overall.quantize(_CENT):
            # compared, not computed: below the ceiling the quantized value is exact
            problems.append(f"materiality {overall} is not a whole number of cents")
        performance, why = _decimal(self.performance_share, "performance share")
        why = why or _share_problem(performance, "performance share", None)
        performance_ok = why is None
        if why:
            problems.append(why)
        trivial, why = _decimal(self.trivial_share, "clearly trivial share")
        why = why or _share_problem(trivial, "clearly trivial share",
                                    performance if performance_ok else None)
        if why:
            problems.append(why)
        if not problems:
            object.__setattr__(self, "overall", overall)
            object.__setattr__(self, "performance_share", performance)
            object.__setattr__(self, "trivial_share", trivial)
            for figure, cents, share in (("performance materiality", self.performance_cents, performance),
                                         ("clearly trivial", self.trivial_cents, trivial)):
                if cents < 1:
                    problems.append(f"{figure} ({percent(share)} of {overall}) is less than a cent")
        if problems:
            raise SamplingError("; ".join(problems))

    def _share(self, share: Decimal) -> Decimal:
        with localcontext() as ctx:
            # exact: a whole-cent figure under the ceiling has 14 digits and a share at most 40,
            # so their product is never rounded before it is floored to the cent
            ctx.prec = 60
            return (self.overall * share).quantize(_CENT, rounding=ROUND_FLOOR)

    @property
    def performance(self) -> Decimal:
        return self._share(self.performance_share)

    @property
    def clearly_trivial(self) -> Decimal:
        return self._share(self.trivial_share)

    @property
    def overall_cents(self) -> int:
        return int(self.overall * 100)

    @property
    def performance_cents(self) -> int:
        return int(self.performance * 100)

    @property
    def trivial_cents(self) -> int:
        return int(self.clearly_trivial * 100)

    def describe(self) -> str:
        return (f"{dollars(self.overall_cents)} overall; "
                f"performance {dollars(self.performance_cents)} ({percent(self.performance_share)}); "
                f"clearly trivial {dollars(self.trivial_cents)} ({percent(self.trivial_share)})")

    def as_dict(self) -> dict:
        return {
            "overall": float(self.overall),
            "performance_share": float(self.performance_share),
            "performance": float(self.performance),
            "trivial_share": float(self.trivial_share),
            "clearly_trivial": float(self.clearly_trivial),
        }


#: Where an entry's amount stands against the three figures, at or above the figure named
#: (as the stratified selection reads performance materiality), and the two cases that hold
#: no dollar at all.
BANDS = ("material", "above performance", "below performance", "clearly trivial",
         "no amount", "negative")
#: An entry of another fiscal year than the one the figures are set for, whatever its amount.
OTHER_YEAR = "other year"

BAND_NOTE = (
    "An entry's materiality band says where its amount stands against the figures given: "
    "material (at or above overall materiality), above performance (at or above performance "
    "materiality), below performance (above clearly trivial), clearly trivial (at or below "
    "it), no amount, negative, and other year for an entry outside the fiscal year the "
    "figures are set for. It is beside the risk score and never part of it: nothing is "
    "ranked or scored by it, and no entry is flagged or dropped by it."
)
FISCAL_YEAR_NOTE = (
    "Materiality is set for one year's financial statements, so the fiscal year is one: the "
    "ledger's fiscal_year column, read as a whole number. A QuickBooks pull fills it with "
    "each posting date's calendar year, so for a company whose fiscal year ends in another "
    "month it is a calendar year."
)


def to_cents(amounts: pd.Series) -> pd.Series:
    """Amounts as whole cents, rounded (never truncated), as the population counts them."""
    return (amounts * 100).round().astype("int64")


def fiscal_year_for(entries: pd.DataFrame, fiscal_year: object = None) -> int | None:
    """The one fiscal year ``entries`` are banded or sampled in, or a SamplingError.

    A ledger of one fiscal year is that year; a ledger of several needs ``fiscal_year``,
    because materiality is set for one year's financial statements. The year is the
    ``fiscal_year`` column read as a whole number. None for a ledger with no entry.
    """
    years = sorted({int(year) for year in entries["fiscal_year"]})
    if fiscal_year is None:
        if len(years) > 1:
            raise SamplingError(
                "the ledger spans fiscal years {}, and materiality is set for one year's financial "
                "statements: name the year (--fiscal-year)".format(", ".join(map(str, years))))
        return years[0] if years else None
    year = _whole(fiscal_year, "fiscal year", 1)
    if year not in years:
        raise SamplingError("the ledger has no entry in fiscal year {} (it holds {})".format(
            year, ", ".join(map(str, years)) or "no entries"))
    return year


def count_by_band(banded: pd.Series) -> dict[str, int]:
    """How many entries fall in each band: every band, in one fixed order, zeros included."""
    return {band: int((banded == band).sum()) for band in (*BANDS, OTHER_YEAR)}


def band_of_cents(cents: int, materiality: Materiality) -> str:
    """The band of one amount in whole cents."""
    if cents < 0:
        return "negative"
    if cents == 0:
        return "no amount"
    if cents >= materiality.overall_cents:
        return "material"
    if cents >= materiality.performance_cents:
        return "above performance"
    if cents > materiality.trivial_cents:
        return "below performance"
    return "clearly trivial"


def bands(entries: pd.DataFrame, materiality: Materiality,
          fiscal_year: int | None = None) -> pd.Series:
    """Each entry's band, indexed like ``entries`` (one row per entry, as ``entry_level``
    gives them: ``entry_amount`` and ``fiscal_year``).

    With ``fiscal_year`` given, an entry of another year is ``other year`` whatever its
    amount: the figures were not set for it.
    """
    cents = to_cents(entries["entry_amount"])
    out = pd.Series([band_of_cents(int(c), materiality) for c in cents],
                    index=entries.index, dtype=object)
    if fiscal_year is not None:
        out[entries["fiscal_year"].map(int) != fiscal_year] = OTHER_YEAR
    return out

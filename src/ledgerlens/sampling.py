"""Materiality, and a sample selected against it: which entries to examine, and why each.

Ranking by risk says which entries look odd, not which are big enough to matter.
``Materiality`` holds the auditor's judgement of that: an overall figure, the share of
it the work is planned against (performance materiality), and the share below which a
misstatement would be clearly trivial (the framework is AU-C 320 and 450). It is given
for each run, never read from a file and never defaulted, and never tuned against
labels (rule 7): this module applies it and does not choose it.

Two ways of selecting entries, both of the kind AU-C 530 describes (nothing here claims
to meet it):

- Monetary unit sampling. Every dollar of the population is a sampling unit, so an
  entry is selected in proportion to its amount, and every entry at or above the
  sampling interval is selected whatever the random start.
- Stratified random sampling. Every entry at or above performance materiality is
  examined in full, and a seeded random draw is taken from every other entry, laid out
  in bands of near-equal value. Clearly trivial sets no entry aside: it governs which
  misstatements are accumulated (AU-C 450), and thousands of small entries can add up
  to far more than materiality.

A selection is what to examine, not a finding (rule 6), and labels are never read here
(rule 2). A selection is reproduced by its seed on every Python this runs on: amounts
are whole cents, sizes and intervals are exact, and the only draws are
``random.Random(seed).random()``, the one sequence Python promises not to change.
"""

from __future__ import annotations

import bisect
import itertools
import json
import platform
import random
import secrets
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext
from fractions import Fraction

import pandas as pd

from . import __version__
from .ingest import entry_level, quote
from .summary import md_code, one_line

FORMATS = ("text", "markdown", "json")
METHODS = ("mus", "stratified")
METHOD_NAMES = {"mus": "monetary unit sampling", "stratified": "stratified random sampling"}
DEFAULT_PERFORMANCE_SHARE = Decimal("0.75")
DEFAULT_TRIVIAL_SHARE = Decimal("0.05")
DEFAULT_CONFIDENCE = Decimal("0.95")
DEFAULT_STRATA = 3
MAX_STRATA = 20
#: As for an approval limit (``controls.LIMIT_CEILING``): no materiality is this large, and
#: below it a figure in whole cents has at most 14 digits, which a float holds exactly.
CEILING = Decimal(10) ** 12
_CENT = Decimal("0.01")
#: A number given on the command line, not a document.
_MAX_DIGITS = 40

SELECTION_CAVEAT = (
    "A selection is what to examine, not a finding: nothing here says an entry is misstated, "
    "and an entry left out is not cleared by being left out."
)
MATERIALITY_NOTE = (
    "Materiality is the auditor's judgement, given on the command line: this tool applies it "
    "and does not choose it."
)
MUS_NOTE = (
    "The sample size assumes no misstatement is expected in the population. If some is "
    "expected, the sample needs to be larger than this one."
)
STRATIFIED_NOTE = (
    "Entries at or above performance materiality are examined in full and are not part of the "
    "random draw; the draw covers every other entry, in bands of near-equal value."
)
INPUT_NOTE = (
    "The selection reads the ledger's amounts only: no test's flags, no model score, no "
    "controls file and no labels."
)


class SamplingError(ValueError):
    """Materiality, or a sampling parameter, that cannot be used."""


def _decimal(value: object, name: str) -> tuple[Decimal | None, str | None]:
    """``(value as a Decimal, None)``, or ``(None, why it is not a finite number)``.

    Read as written: a string is parsed by Decimal, never through a float, so 0.1 is
    0.1; a float built in code is read by its repr for the same reason.
    """
    if isinstance(value, bool):
        return None, f"{name} must be a number, got {value!r}"
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
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


def _percent(share: Decimal) -> str:
    return f"{(share * 100).normalize():f}%"


def _dollars(cents: int | Fraction) -> str:
    """Whole cents as dollars; an exact fraction of a cent rounded to the cent for display."""
    return f"${float(Fraction(cents)) / 100:,.2f}"


@dataclass(frozen=True)
class Materiality:
    """Overall materiality, and the two figures derived from it.

    ``performance`` (the share of overall the work is planned against) and
    ``clearly_trivial`` (the share below which a misstatement would be clearly trivial)
    are rounded down to the cent: the lower figure is the cautious one, a larger sample
    and fewer entries set aside. Every problem with the three is named in one
    ``SamplingError``.
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
        performance_ok = why is None and 0 < performance <= 1
        if why:
            problems.append(why)
        elif not performance_ok:
            problems.append(f"performance share must be above 0 and at most 1, got {performance}")
        trivial, why = _decimal(self.trivial_share, "clearly trivial share")
        if why:
            problems.append(why)
        elif trivial <= 0:
            problems.append(f"clearly trivial share must be above 0, got {trivial}")
        elif performance_ok and trivial >= performance:
            problems.append(f"clearly trivial share ({trivial}) must be below the performance "
                            f"share ({performance})")
        if not problems:
            object.__setattr__(self, "overall", overall)
            object.__setattr__(self, "performance_share", performance)
            object.__setattr__(self, "trivial_share", trivial)
            if self.performance_cents < 1:
                problems.append(f"performance materiality ({_percent(performance)} of {overall}) "
                                "is less than a cent")
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
        return (f"{_dollars(self.overall_cents)} overall; "
                f"performance {_dollars(self.performance_cents)} ({_percent(self.performance_share)}); "
                f"clearly trivial {_dollars(self.trivial_cents)} ({_percent(self.trivial_share)})")

    def as_dict(self) -> dict:
        return {
            "overall": float(self.overall),
            "performance_share": float(self.performance_share),
            "performance": float(self.performance),
            "trivial_share": float(self.trivial_share),
            "clearly_trivial": float(self.clearly_trivial),
        }


@dataclass
class Selection:
    """What was selected, how, and from what.

    ``items`` has one row per selected entry. ``parameters`` holds every figure the method
    used, JSON-ready, so the memo can state how the selection can be repeated.
    """

    method: str
    seed: int
    seed_drawn: bool
    materiality: Materiality
    population: dict
    parameters: dict
    items: pd.DataFrame = field(repr=False)


def population(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """The entries a sample is drawn from, in ``entry_id`` order, and how many were left out.

    An entry's amount is ``entry_level``'s, the larger of its debits and its credits, in
    whole cents. An entry with no positive amount holds no dollar to select, so it is left
    out and counted. The order makes a selection independent of the order of the ledger's
    rows; ``entry_id`` is unique per entry, so it is the whole order.
    """
    entries = entry_level(df)
    cents = (entries["entry_amount"] * 100).round().astype("int64")
    entries = entries.assign(cents=cents)
    kept = entries[entries["cents"] > 0].sort_values("entry_id").reset_index(drop=True)
    kept = kept.assign(
        posting_date=pd.to_datetime(kept["posting_date"]).dt.strftime("%Y-%m-%d"),
        description=kept["description"].map(one_line),
    )
    return kept[["entry_id", "posting_date", "fiscal_year", "description", "cents"]], len(entries) - len(kept)


def _seed(seed: object) -> tuple[int, bool]:
    if seed is None:
        return secrets.randbelow(2 ** 32), True
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise SamplingError(f"seed must be a whole number of 0 or more, got {quote(seed)}")
    return seed, False


def _population_facts(entries: pd.DataFrame, left_out: int) -> dict:
    return {
        "entries": len(entries) + left_out,
        "sampled_from": len(entries),
        "left_out_no_amount": left_out,
        "book_value": int(entries["cents"].sum()) / 100,
        "fiscal_years": sorted(int(year) for year in entries["fiscal_year"].unique()),
    }


def mus_select(df: pd.DataFrame, materiality: Materiality, confidence: object = DEFAULT_CONFIDENCE,
               seed: int | None = None) -> Selection:
    """Monetary unit sampling, sized for no expected misstatement.

    The reliability factor is the Poisson zero-error factor, -ln(1 - confidence) (3.00 at
    95%). The sample size n is book value x factor / performance materiality, rounded up,
    and the sampling interval J is book value / n, both exact. A random start s in [0, J)
    selects the dollars s, s + J, s + 2J, ...; each falls in one entry, laid end to end
    in ``entry_id`` order. An entry of J or more always holds one.
    """
    level, why = _decimal(confidence, "confidence")
    if why:
        raise SamplingError(why)
    if not 0 < level < 1:
        raise SamplingError(f"confidence must be above 0 and below 1, got {level}")
    seed, drawn = _seed(seed)
    entries, left_out = population(df)
    if entries.empty:
        raise SamplingError("the ledger has no entry with an amount to select")
    cents = [int(c) for c in entries["cents"]]
    book = sum(cents)
    with localcontext() as ctx:
        ctx.prec = 28
        factor = -(Decimal(1) - level).ln()
        size = int((Decimal(book) * factor / Decimal(materiality.performance_cents))
                   .to_integral_value(rounding=ROUND_CEILING))
    size = max(size, 1)
    if size > len(cents):
        raise SamplingError(
            f"at this materiality the sample is {size:,} units, more than the ledger's {len(cents):,} "
            "entries with an amount: a sample that size is every entry, and examining every entry "
            "is not sampling")
    interval = Fraction(book, size)
    start = Fraction(random.Random(seed).random()) * interval
    upper = list(itertools.accumulate(cents))  # entry i holds the dollars [upper[i-1], upper[i])
    hits: Counter = Counter()
    first: dict = {}
    for k in range(size):
        point = start + k * interval
        i = bisect.bisect_right(upper, point)
        hits[i] += 1
        first.setdefault(i, point)
    rows = []
    for i in sorted(hits):
        row = entries.iloc[i]
        rows.append({
            "entry_id": row["entry_id"],
            "posting_date": row["posting_date"],
            "amount": cents[i] / 100,
            "hits": hits[i],
            "selected_dollar": round(float(first[i]) / 100, 2),
            "why": ("at or above the sampling interval, so selected whatever the start"
                    if cents[i] >= interval else "a selected dollar falls in it"),
            "description": row["description"],
        })
    return Selection(
        method="mus", seed=seed, seed_drawn=drawn, materiality=materiality,
        population=_population_facts(entries, left_out),
        parameters={
            "confidence": float(level),
            "reliability_factor": round(float(factor), 4),
            "size": size,
            "interval": float(interval) / 100,
            "start": float(start) / 100,
            "entries_selected": len(rows),
        },
        items=pd.DataFrame(rows, columns=["entry_id", "posting_date", "amount", "hits",
                                          "selected_dollar", "why", "description"]),
    )


def _allocate(size: int, values: list[int], counts: list[int]) -> list[int]:
    """``size`` draws shared among bands in proportion to their value, by largest remainder.

    Each band is given the whole part of its share and the draws left over go to the
    largest fractional parts, the lower band on a tie. A band whose share is as large as
    what it holds is given all of it, and the rest is shared again among the others.
    """
    alloc = [0] * len(values)
    left = size
    while left > 0:
        open_ = [b for b, count in enumerate(counts) if alloc[b] < count]
        weight = sum(values[b] for b in open_)
        share = {b: Fraction(left * values[b], weight) for b in open_}
        full = [b for b in open_ if share[b] >= counts[b] - alloc[b]]
        if full:
            for b in full:
                left -= counts[b] - alloc[b]
                alloc[b] = counts[b]
            continue
        whole = {b: int(share[b]) for b in open_}
        rest = left - sum(whole.values())
        for b in sorted(open_, key=lambda b: (whole[b] - share[b], b))[:rest]:
            whole[b] += 1
        for b in open_:
            alloc[b] += whole[b]
        left = 0
    return alloc


def stratified_select(df: pd.DataFrame, materiality: Materiality, size: int,
                      seed: int | None = None, strata: int = DEFAULT_STRATA) -> Selection:
    """Every entry at or above performance materiality, and a seeded draw from the rest.

    The rest (every entry below performance materiality) is laid out in amount order,
    ``entry_id`` breaking ties, and cut into ``strata`` bands of near-equal value: an entry
    falls in the band its first cent does. ``size`` entries are drawn from it, shared among
    the bands in proportion to their value (``_allocate``). Within a band the entries with the smallest seeded keys are
    drawn, ``entry_id`` breaking ties; the keys are drawn in the rest's order, so one seed
    always gives one selection.
    """
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise SamplingError(f"size must be a whole number of 1 or more, got {quote(size)}")
    if isinstance(strata, bool) or not isinstance(strata, int) or not 1 <= strata <= MAX_STRATA:
        raise SamplingError(f"strata must be a whole number from 1 to {MAX_STRATA}, got {quote(strata)}")
    seed, drawn = _seed(seed)
    entries, left_out = population(df)
    if entries.empty:
        raise SamplingError("the ledger has no entry with an amount to select")
    pm = materiality.performance_cents
    full = entries[entries["cents"] >= pm]
    middle = (entries[entries["cents"] < pm]
              .sort_values(["cents", "entry_id"]).reset_index(drop=True))
    if size > len(middle):
        raise SamplingError(
            f"size {size:,} is more than the {len(middle):,} entries below performance "
            f"materiality ({_dollars(pm)})")
    cents = [int(c) for c in middle["cents"]]
    total = sum(cents)
    before = [0, *itertools.accumulate(cents)][:-1]  # the value laid out ahead of each entry
    band = [spent * strata // total for spent in before]
    values = [sum(c for c, k in zip(cents, band) if k == b) for b in range(strata)]
    counts = [band.count(b) for b in range(strata)]
    alloc = _allocate(size, values, counts)
    rng = random.Random(seed)
    keys = [rng.random() for _ in cents]
    drawn_rows = []
    for b in range(strata):
        members = sorted((keys[i], middle.at[i, "entry_id"], i) for i in range(len(cents)) if band[i] == b)
        drawn_rows += [(b, i) for _, _, i in members[:alloc[b]]]
    rows = [{
        "entry_id": row.entry_id, "posting_date": row.posting_date, "amount": int(row.cents) / 100,
        "band": "full", "why": "at or above performance materiality: examined in full",
        "description": row.description,
    } for row in full.itertuples()]
    for b, i in sorted(drawn_rows, key=lambda pair: (pair[0], middle.at[pair[1], "entry_id"])):
        rows.append({
            "entry_id": middle.at[i, "entry_id"], "posting_date": middle.at[i, "posting_date"],
            "amount": cents[i] / 100, "band": str(b + 1),
            "why": f"drawn at random from band {b + 1} of {strata}",
            "description": middle.at[i, "description"],
        })
    bands = []
    for b in range(strata):
        members = [c for c, k in zip(cents, band) if k == b]
        bands.append({
            "band": b + 1,
            "entries": len(members),
            "value": sum(members) / 100,
            "from": min(members) / 100 if members else None,
            "to": max(members) / 100 if members else None,
            "drawn": alloc[b],
        })
    return Selection(
        method="stratified", seed=seed, seed_drawn=drawn, materiality=materiality,
        population=_population_facts(entries, left_out),
        parameters={
            "size": size,
            "strata": strata,
            "bands": bands,
            "examined_in_full": len(full),
            "examined_in_full_value": int(full["cents"].sum()) / 100,
            "drawn_from": len(middle),
            "drawn_from_value": total / 100,
            "entries_selected": len(rows),
        },
        items=pd.DataFrame(rows, columns=["entry_id", "posting_date", "amount", "band", "why",
                                          "description"]),
    )


def collect_memo(selection: Selection, *, ledger: str | None, ledger_id: str,
                 today: date | None = None) -> dict:
    """The selection memo as one JSON-ready payload; the renderers below read only this."""
    notes = [MATERIALITY_NOTE, MUS_NOTE if selection.method == "mus" else STRATIFIED_NOTE, INPUT_NOTE]
    return {
        "run_date": (today or date.today()).isoformat(),
        "caveat": SELECTION_CAVEAT,
        "ledger": ledger,
        "ledger_id": ledger_id,
        "population": selection.population,
        "materiality": selection.materiality.as_dict(),
        "method": selection.method,
        "seed": selection.seed,
        "seed_drawn": selection.seed_drawn,
        "parameters": selection.parameters,
        "selected": [{key: (value.item() if hasattr(value, "item") else value)
                      for key, value in row.items()}
                     for row in selection.items.to_dict(orient="records")],
        "notes": notes,
        "versions": {"ledgerlens": __version__, "python": platform.python_version()},
    }


def _materiality_line(m: dict) -> str:
    return "${:,.2f} overall; performance ${:,.2f} ({}); clearly trivial ${:,.2f} ({})".format(
        m["overall"], m["performance"], _percent(Decimal(repr(m["performance_share"]))),
        m["clearly_trivial"], _percent(Decimal(repr(m["trivial_share"]))))


def _population_line(p: dict) -> str:
    years = ", ".join(str(y) for y in p["fiscal_years"]) or "none"
    line = "{:,} entries, ${:,.2f}; fiscal years {}".format(p["sampled_from"], p["book_value"], years)
    if p["left_out_no_amount"]:
        line += "; {:,} entries of no amount left out".format(p["left_out_no_amount"])
    return line


def _seed_line(payload: dict) -> str:
    if payload["seed_drawn"]:
        return "{} (drawn; run again with --seed {} to repeat this selection)".format(
            payload["seed"], payload["seed"])
    return "{} (given)".format(payload["seed"])


def _method_rows(payload: dict) -> list[tuple[str, str]]:
    p = payload["parameters"]
    if payload["method"] == "mus":
        return [
            ("Confidence", _percent(Decimal(repr(p["confidence"])))),
            ("Reliability factor", "{:.4f} (-ln(1 - confidence): no misstatement expected)".format(
                p["reliability_factor"])),
            ("Sample size", "{:,} units (book value x factor / performance materiality, rounded up)"
             .format(p["size"])),
            ("Sampling interval", "${:,.2f} (book value / sample size)".format(p["interval"])),
            ("Random start", "${:,.2f} (in the first interval)".format(p["start"])),
            ("Selected", "{:,} entries; an entry can hold several units, and every entry of "
             "${:,.2f} or more is selected".format(p["entries_selected"], p["interval"])),
        ]
    rows = [
        ("Examined in full", "{:,} entries, ${:,.2f} (at or above performance materiality)".format(
            p["examined_in_full"], p["examined_in_full_value"])),
        ("Drawn", "{:,} of the {:,} entries below it (${:,.2f}), from {} band(s) of near-equal "
         "value".format(p["size"], p["drawn_from"], p["drawn_from_value"], p["strata"])),
    ]
    for b in p["bands"]:
        span = ("${:,.2f} to ${:,.2f}".format(b["from"], b["to"]) if b["entries"] else "no entries")
        rows.append((f"Band {b['band']}", "{} ({:,} entries, ${:,.2f}): {:,} drawn".format(
            span, b["entries"], b["value"], b["drawn"])))
    rows.append(("Selected", "{:,} entries".format(p["entries_selected"])))
    return rows


def _detail(payload: dict, row: dict) -> str:
    return (f"{row['hits']} unit(s), first at ${row['selected_dollar']:,.2f}"
            if payload["method"] == "mus" else f"band {row['band']}")


def render_text(payload: dict) -> str:
    out = [
        "LedgerLens selection memo, {}".format(payload["run_date"]),
        "",
        *textwrap.wrap(payload["caveat"], 96),
        "",
        "Ledger       {}".format(one_line(payload["ledger"]) or "(a frame, not a file)"),
        "Identity     {}".format(one_line(payload["ledger_id"])),
        "Population   " + _population_line(payload["population"]),
        "Materiality  " + _materiality_line(payload["materiality"]),
        "Method       " + METHOD_NAMES[payload["method"]],
        "Seed         " + _seed_line(payload),
        "",
    ]
    out += [f"  {label:<20}{value}" for label, value in _method_rows(payload)]
    out += ["", "Selected entries"]
    rows = payload["selected"]
    width = max((len(one_line(r["entry_id"])) for r in rows), default=0)
    for r in rows:
        out.append("  {:<{}}  {}  ${:>14,.2f}  {}  {}".format(
            one_line(r["entry_id"]), width, one_line(r["posting_date"]), r["amount"],
            _detail(payload, r), one_line(r["description"])))
        out.append("  {:<{}}  {}".format("", width, r["why"]))
    if not rows:
        out.append("  none")
    out += ["", "Notes"]
    for note in payload["notes"]:
        out += textwrap.wrap(note, 96, initial_indent="  - ", subsequent_indent="    ")
    return "\n".join(out)


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]], right: tuple[int, ...] = ()) -> list[str]:
    rule = ["---:" if i in right else "---" for i in range(len(header))]
    return ["| " + " | ".join(cells) + " |" for cells in (header, tuple(rule), *rows)]


def render_markdown(payload: dict) -> str:
    """Markdown for a reviewer's file. Every ledger-derived string goes through ``md_code``."""
    out = [
        "## LedgerLens selection memo, {}".format(payload["run_date"]),
        "",
        "> " + payload["caveat"],
        "",
        *_table(("", ""), [
            ("Ledger", "{} ({})".format(md_code(payload["ledger"]) or "a frame, not a file",
                                        md_code(payload["ledger_id"]))),
            ("Population", _population_line(payload["population"])),
            ("Materiality", _materiality_line(payload["materiality"])),
            ("Method", METHOD_NAMES[payload["method"]]),
            ("Seed", _seed_line(payload)),
            *_method_rows(payload),
        ]),
        "",
        "### Selected entries",
        "",
    ]
    if payload["selected"]:
        out += _table(
            ("Entry", "Posted", "Amount", "Units" if payload["method"] == "mus" else "Band", "Why",
             "Description"),
            [(md_code(r["entry_id"]), md_code(r["posting_date"]), "{:,.2f}".format(r["amount"]),
              _detail(payload, r), r["why"], md_code(r["description"]))
             for r in payload["selected"]],
            right=(2,),
        )
    else:
        out.append("No entry was selected.")
    out += ["", "### Notes", ""] + [f"- {note}" for note in payload["notes"]]
    return "\n".join(out)


def render_memo(payload: dict, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(payload, indent=2)
    if fmt == "markdown":
        return render_markdown(payload)
    if fmt == "text":
        return render_text(payload)
    raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")

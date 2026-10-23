"""Materiality, and a sample selected against it: which entries to examine, and why each.

Ranking by risk says which entries look odd, not which are big enough to matter.
``Materiality`` (kept in ``materiality``, with each entry's band against it) holds the
auditor's judgement of that: an overall figure, the share of
it the work is planned against (performance materiality), and the share below which a
misstatement would be clearly trivial (the framework is AU-C 320 and 450). The overall
figure is given for each run, never read from a file and never defaulted; the two shares
default to 75% and 5%. None of them is tuned against labels (rule 7): this module applies
them and does not choose them.

Materiality is set for one year's financial statements, so the population is one fiscal
year: every entry of that year with a positive amount. Two ways of selecting from it,
both of the kind AU-C 530 describes (nothing here claims to meet it):

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

import itertools
import json
import platform
import random
import secrets
import textwrap
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, Decimal, localcontext
from fractions import Fraction

import pandas as pd

from . import __version__
from .ingest import entry_level
from .materiality import (
    CEILING as CEILING,
)
from .materiality import (
    DEFAULT_PERFORMANCE_SHARE as DEFAULT_PERFORMANCE_SHARE,
)
from .materiality import (
    DEFAULT_TRIVIAL_SHARE as DEFAULT_TRIVIAL_SHARE,
)
from .materiality import (
    FISCAL_YEAR_NOTE,
    Materiality,
    SamplingError,
    _decimal,
    _whole,
    fiscal_year_for,
    to_cents,
)
from .materiality import (
    dollars as _dollars,
)
from .materiality import (
    percent as _percent,
)
from .summary import md_code, one_line

FORMATS = ("text", "markdown", "json")
METHODS = ("mus", "stratified")
METHOD_NAMES = {"mus": "monetary unit sampling", "stratified": "stratified random sampling"}
DEFAULT_CONFIDENCE = Decimal("0.95")
DEFAULT_STRATA = 3
MAX_STRATA = 20

SELECTION_CAVEAT = (
    "A selection is what to examine, not a finding: nothing here says an entry is misstated, "
    "and an entry left out is not cleared by being left out."
)
MATERIALITY_NOTE = (
    "Materiality is the auditor's judgement, given on the command line: this tool applies it "
    "and does not choose it. The shares default to 75% (performance) and 5% (clearly trivial); "
    "performance materiality is commonly set at 50% to 75% of overall, and a lower share gives "
    "a larger sample."
)
POPULATION_NOTE = (
    "The population is every entry of one fiscal year with a positive amount, in every account "
    "and from every source: a journal-entry population, not one tied to an account balance or "
    "an assertion."
)
NEGATIVE_NOTE = (
    "Entries with a negative amount hold no dollar to select, so they are not in this "
    "population and need a procedure of their own."
)
MUS_NOTE = (
    "Tolerable misstatement is taken to be performance materiality, and the sample size assumes "
    "no misstatement is expected in the population. If some is expected, the sample needs to be "
    "larger than this one."
)
STRATIFIED_NOTE = (
    "Entries at or above performance materiality are examined in full and are not part of the "
    "random draw; the draw covers every other entry, in bands of near-equal value."
)
INPUT_NOTE = (
    "The selection is made from the ledger's entries alone, by their ids, fiscal years and "
    "amounts, and lists each with its posting date and description (its first line's, by line "
    "number). It reads no test's flags, no model score, no controls file and no labels."
)


def _confidence(value: object) -> Decimal:
    level, why = _decimal(value, "confidence")
    if why:
        raise SamplingError(why)
    if not 0 < level < 1:
        raise SamplingError(f"confidence must be above 0 and below 1, got {level}")
    return level


def _seed(seed: object) -> tuple[int, bool]:
    if seed is None:
        return secrets.randbelow(2 ** 32), True
    return _whole(seed, "seed", 0), False


def check_parameters(method: str, *, confidence: object = None, size: object = None,
                     strata: object = None, seed: object = None, fiscal_year: object = None) -> None:
    """Raise SamplingError for a parameter the method cannot use, before any ledger is read."""
    if method == "mus":
        _confidence(DEFAULT_CONFIDENCE if confidence is None else confidence)
    else:
        _whole(size, "size", 1)
        _whole(DEFAULT_STRATA if strata is None else strata, "strata", 1, MAX_STRATA)
    if seed is not None:
        _seed(seed)
    if fiscal_year is not None:
        _whole(fiscal_year, "fiscal year", 1)


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


def population(df: pd.DataFrame, fiscal_year: object = None) -> tuple[pd.DataFrame, dict]:
    """The entries of one fiscal year a sample is drawn from, in ``entry_id`` order, and what
    the population leaves out.

    A ledger of one fiscal year is that year; a ledger of several needs ``fiscal_year``,
    because materiality is set for one year's financial statements. An entry's amount is
    ``entry_level``'s, the larger of its debits and its credits, in whole cents. Its
    description is its first line's in ``line_no`` order, a missing one passed over: lines
    are put in that order first, so it does not depend on the order of the rows, except
    between lines that share a line number, which keep the ledger's order. The year is the
    ``fiscal_year`` column read as a whole number. An entry of no amount, or of a negative one, holds
    no dollar to select: it is left out and counted. ``entry_id`` is unique per entry, so
    it is the whole order.
    """
    entries = entry_level(df.sort_values(["entry_id", "line_no"], kind="mergesort"))
    fiscal = entries["fiscal_year"].map(int)
    year = fiscal_year_for(entries, fiscal_year)
    in_year = entries if year is None else entries[fiscal == year]
    in_year = in_year.assign(cents=to_cents(in_year["entry_amount"]))
    kept = in_year[in_year["cents"] > 0].sort_values("entry_id").reset_index(drop=True)
    kept = kept.assign(
        posting_date=pd.to_datetime(kept["posting_date"]).dt.strftime("%Y-%m-%d"),
        description=kept["description"].map(lambda text: "" if pd.isna(text) else one_line(text)),
    )
    facts = {
        "fiscal_year": year,
        "entries_in_year": len(in_year),
        "sampled_from": len(kept),
        "left_out_no_amount": int((in_year["cents"] == 0).sum()),
        "left_out_negative": int((in_year["cents"] < 0).sum()),
        "other_years": len(entries) - len(in_year),
        "book_value": int(kept["cents"].sum()) / 100,
    }
    return kept[["entry_id", "posting_date", "fiscal_year", "description", "cents"]], facts


def _nothing_to_select(facts: dict) -> SamplingError:
    year = f" in fiscal year {facts['fiscal_year']}" if facts["fiscal_year"] is not None else ""
    return SamplingError(f"the ledger has no entry with a positive amount to select{year}")


def _ceil(value: Fraction) -> int:
    return -((-value) // 1)


def mus_select(df: pd.DataFrame, materiality: Materiality, confidence: object = DEFAULT_CONFIDENCE,
               seed: object = None, fiscal_year: object = None) -> Selection:
    """Monetary unit sampling, sized for no expected misstatement.

    The reliability factor is the Poisson zero-error factor, -ln(1 - confidence) (3.00 at
    95%), and tolerable misstatement is taken to be performance materiality. The sample
    size n is book value x factor / performance materiality, rounded up, and the sampling
    interval J is book value / n, both exact. A random start s in [0, J) selects the dollars
    s, s + J, ..., s + (n - 1)J of the entries laid end to end in ``entry_id`` order, each
    entry holding the dollars [its start, its end). An entry of J or more always holds one;
    each entry's count is computed from its bounds, so the work is one pass over the
    entries however large n is.
    """
    level = _confidence(confidence)
    seed, drawn = _seed(seed)
    entries, facts = population(df, fiscal_year)
    if entries.empty:
        raise _nothing_to_select(facts)
    cents = [int(c) for c in entries["cents"]]
    book = sum(cents)
    with localcontext() as ctx:
        ctx.prec = 28
        factor = -(Decimal(1) - level).ln()
        size = int((Decimal(book) * factor / Decimal(materiality.performance_cents))
                   .to_integral_value(rounding=ROUND_CEILING))
    size = max(size, 1)  # a confidence so near 0 that its factor rounds to 0 still selects a unit
    interval = Fraction(book, size)
    start = Fraction(random.Random(seed).random()) * interval
    rows, certain, low = [], 0, 0
    for i, amount in enumerate(cents):
        high = low + amount
        first = max(0, _ceil((low - start) / interval))
        hits = min(size, _ceil((high - start) / interval)) - first
        if hits > 0:
            row = entries.iloc[i]
            at = start + first * interval  # in cents, laid end to end; floored to the cent below
            certain += amount >= interval
            rows.append({
                "entry_id": row["entry_id"],
                "posting_date": row["posting_date"],
                "amount": amount / 100,
                "hits": hits,
                "first_unit": int(at) / 100,
                "why": ("at or above the sampling interval, so selected whatever the start"
                        if amount >= interval else "a selected dollar falls in it"),
                "description": row["description"],
            })
        low = high
    return Selection(
        method="mus", seed=seed, seed_drawn=drawn, materiality=materiality, population=facts,
        parameters={
            "confidence": float(level),
            "reliability_factor": round(float(factor), 4),
            "size": size,
            "interval": float(interval) / 100,
            "certain_from": _ceil(interval) / 100,  # whole cents: every entry this large holds a unit
            "start": float(start) / 100,
            "entries_selected": len(rows),
            "certain": certain,
        },
        items=pd.DataFrame(rows, columns=["entry_id", "posting_date", "amount", "hits",
                                          "first_unit", "why", "description"]),
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


def stratified_select(df: pd.DataFrame, materiality: Materiality, size: object,
                      seed: object = None, strata: object = DEFAULT_STRATA,
                      fiscal_year: object = None) -> Selection:
    """Every entry at or above performance materiality, and a seeded draw from the rest.

    The rest (every entry below performance materiality) is laid out in amount order,
    ``entry_id`` breaking ties, and cut into ``strata`` bands of near-equal value: an entry
    falls in the band its first cent does. ``size`` entries are drawn from it, shared among
    the bands in proportion to their value (``_allocate``). Within a band the entries with
    the smallest seeded keys are drawn, ``entry_id`` breaking ties; the keys are drawn in
    the rest's order, so one seed always gives one selection.
    """
    size = _whole(size, "size", 1)
    strata = _whole(strata, "strata", 1, MAX_STRATA)
    seed, drawn = _seed(seed)
    entries, facts = population(df, fiscal_year)
    if entries.empty:
        raise _nothing_to_select(facts)
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
        method="stratified", seed=seed, seed_drawn=drawn, materiality=materiality, population=facts,
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
    notes = [MATERIALITY_NOTE, POPULATION_NOTE, FISCAL_YEAR_NOTE]
    if selection.population["left_out_negative"]:
        notes.append(NEGATIVE_NOTE)
    notes += [MUS_NOTE if selection.method == "mus" else STRATIFIED_NOTE, INPUT_NOTE]
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


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _materiality_line(m: dict) -> str:
    return "${:,.2f} overall; performance ${:,.2f} ({}); clearly trivial ${:,.2f} ({})".format(
        m["overall"], m["performance"], _percent(Decimal(repr(m["performance_share"]))),
        m["clearly_trivial"], _percent(Decimal(repr(m["trivial_share"]))))


def _population_line(p: dict) -> str:
    year = "no fiscal year" if p["fiscal_year"] is None else f"fiscal year {p['fiscal_year']}"
    line = "{}: {}, ${:,.2f}".format(year, _count(p["sampled_from"], "entry", "entries"),
                                     p["book_value"])
    left = []
    if p["left_out_no_amount"]:
        left.append(_count(p["left_out_no_amount"], "entry", "entries") + " of no amount")
    if p["left_out_negative"]:
        left.append(f"{p['left_out_negative']:,} with a negative amount")
    if left:
        line += "; " + " and ".join(left) + " left out"
    if p["other_years"]:
        line += "; {} of other fiscal years not in it".format(
            _count(p["other_years"], "entry", "entries"))
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
            ("Sample size", "{} (book value x factor / performance materiality, rounded up)"
             .format(_count(p["size"], "unit", "units"))),
            ("Sampling interval", "${:,.2f} (book value / sample size)".format(p["interval"])),
            ("Random start", "${:,.2f} (in the first interval)".format(p["start"])),
            ("Selected", "{}; an entry can hold several units, and every entry of ${:,.2f} or more "
             "is selected ({:,} here)".format(_count(p["entries_selected"], "entry", "entries"),
                                              p["certain_from"], p["certain"])),
        ]
    holding = sum(1 for b in p["bands"] if b["entries"])
    bands = _count(holding, "band", "bands") + " of near-equal value"
    if holding < p["strata"]:
        bands += f" ({p['strata']} asked for, {p['strata'] - holding} holding no entry)"
    rows = [
        ("Examined in full", "{}, ${:,.2f} (at or above performance materiality)".format(
            _count(p["examined_in_full"], "entry", "entries"), p["examined_in_full_value"])),
        ("Drawn", "{:,} of the {} below it (${:,.2f}), from {}".format(
            p["size"], _count(p["drawn_from"], "entry", "entries"), p["drawn_from_value"], bands)),
    ]
    for b in p["bands"]:
        span = ("${:,.2f} to ${:,.2f}".format(b["from"], b["to"]) if b["entries"] else "no entries")
        rows.append((f"Band {b['band']}", "{} ({}, ${:,.2f}): {:,} drawn".format(
            span, _count(b["entries"], "entry", "entries"), b["value"], b["drawn"])))
    rows.append(("Selected", _count(p["entries_selected"], "entry", "entries")))
    return rows


def _detail(payload: dict, row: dict) -> str:
    if payload["method"] == "mus":
        return "{}, the first at cumulative ${:,.2f}".format(
            _count(row["hits"], "unit", "units"), row["first_unit"])
    return "examined in full" if row["band"] == "full" else f"band {row['band']}"


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

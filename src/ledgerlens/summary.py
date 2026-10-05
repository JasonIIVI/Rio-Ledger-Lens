"""One page on a scored ledger: what was tested, what fired, how review stands.

The other commands each print a part of this. ``summary`` gathers it once, as
text for a terminal, JSON for a script, or markdown the weekly workflow posts
as a GitHub Issue. Three things follow from that last use:

- Nothing here scores anything. The numbers are the ones ``LedgerContext`` and
  ``evaluate`` already compute; labels are joined only inside ``evaluate``
  (rule 2), and the two tiers are reported under separate headings (rule 3).
- A caveat travels in the payload beside what it qualifies, so no format can
  show a number without it.
- The markdown is posted where anyone can read it and where a workflow answers
  mentions. Every string that came from the ledger is rendered as inert code
  (see :func:`md_code`), and no format carries note text or a reviewer's name.
"""

from __future__ import annotations

import json
import platform
import re
import textwrap
from dataclasses import asdict
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import sklearn

from . import __version__, evaluate, jets
from .connectors.qbo import SMALL_LEDGER
from .ledger_context import CAVEAT, ENTRY_COLUMNS, LedgerContext, records
from .narrative_eval import DEFAULT_LEDGER_SHA256
from .review import DECISIONS
from .schema import AnomalyType

FORMATS = ("text", "markdown", "json")

SEEDED_NOTE = (
    "This is the generator's default ledger. It is seeded, so these numbers move only when the "
    "code or one of its dependencies changes: a regression watch, not new data."
)
TUNING_NOTE = (
    "This is not the generator's default ledger. The tests' thresholds were tuned against that "
    "one (docs/tuning.md); here they are a starting point, not a configuration."
)
NO_LABELS_NOTE = "No labels were given, so detection quality was not measured for this ledger."
SMALL_NOTE = (
    "{entries} entries is a small population; the model tier and Benford analysis need far "
    "more to say anything."
)
NO_REVIEW_DB_NOTE = "The review database named with --db does not exist; review progress is not shown."


def _entry(row: dict) -> dict:
    """A top-exception row without what a summary must never carry: the note and who decided."""
    entry = {column: row.get(column) for column in ENTRY_COLUMNS}
    entry["posting_date"] = str(entry["posting_date"] or "")[:10]  # a date; never re-parsed
    if "decision" in row:  # only when a review database was read
        entry["decision"] = _decision_kind(row["decision"])
    return entry


def _decision_kind(value: object) -> str | None:
    """A decision as one of the kinds this version records, or "unrecognised".

    The store's own constraint lives in the file's DDL, and a review database is
    a file somebody else may have made: whatever text it holds in that column
    has no place on a page written to be posted.
    """
    if value is None:
        return None
    return value if value in DECISIONS else "unrecognised"


def _definitional(anomaly_type: str) -> bool | None:
    """Whether the generator injects this archetype by the rule that detects it.

    None for a type the generator does not know: nothing can be claimed about it.
    """
    if anomaly_type not in AnomalyType.ALL:
        return None
    return anomaly_type not in evaluate.NON_CIRCULAR_ARCHETYPES


def collect(
    context: LedgerContext,
    labels: pd.DataFrame | None = None,
    top: int = 10,
    ledger: str | None = None,
    today: date | None = None,
) -> dict:
    """Everything the renderers show, as plain JSON types.

    ``labels`` must already have passed ``evaluate.check_labels`` for this
    ledger. ``ledger`` is the path as the caller typed it, never resolved: a
    summary may be posted in public and has no business naming a home
    directory. ``today`` is for tests; a run is dated in UTC.
    """
    context.load()
    base = context.summary()
    report = context.model_report
    payload = {
        "run_date": (today or datetime.now(timezone.utc).date()).isoformat(),
        # What a scheduled run can see change that a push to main cannot.
        "versions": {
            "ledgerlens": __version__,
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "scikit-learn": sklearn.__version__,
        },
        "ledger": None if ledger is None else str(ledger),
        "ledger_id": context.ledger_id,
        "default_ledger": context.ledger_id == "csv:" + DEFAULT_LEDGER_SHA256,
        "entries": base["entries"],
        "lines": base["lines"],
        "fiscal_years": base["fiscal_years"],
        # Rule tier: an entry is flagged when at least one test fired on it.
        "flagged": base["flagged"],
        "flag_rate": base["flag_rate"],
        "flags_raised": base["flags_raised"],
        "flags_by_test": base["flags_by_test"],
        "tier_agreement": base["tier_agreement"],
        "model_tier": {
            **asdict(report),
            "top_pct": context.model_top_pct,
            "flagged": int(context.combined["model_flag"].sum()),
        },
        "top_exceptions": [_entry(r) for r in context.top_exceptions(limit=top)["entries"]],
    }

    if context.review_db is not None:
        status = base["review"]
        by_decision: dict[str, int] = {}
        for name in (*DECISIONS, "unrecognised"):
            count = sum(n for kind, n in status["by_decision"].items() if _decision_kind(kind) == name)
            if count:
                by_decision[name] = count
        payload["review"] = {
            "review_db": status["review_db"],
            "exists": status["exists"],
            "flagged": status["flagged"],
            # Every entry with a decision under this ledger, flagged now or not: a
            # QuickBooks ledger keeps its identity across pulls, so some may belong to
            # another period, and a change to a test can unflag a decided entry.
            "decided": status["decided"],
            "decided_flagged": status["flagged"] - status["outstanding"],
            "outstanding": status["outstanding"],
            "by_decision": by_decision,
            "narratives": status["narratives"],
            # A count: which other companies a file holds is not this ledger's to print.
            "other_ledgers": len(status["other_ledgers"]),
        }

    if labels is not None:
        by_archetype = records(evaluate.recall_by_archetype(context.flags, labels))
        for row in by_archetype:
            row["definitional"] = _definitional(row["anomaly_type"])
        # Measured archetypes first, then by name: recall ties have no order of their own.
        by_archetype.sort(key=lambda r: (r["definitional"] is not False, r["anomaly_type"]))
        payload["detection"] = {
            "tier": "rule",
            "metrics": evaluate.evaluate(context.flags, labels, context.lines["entry_id"].unique()),
            "by_archetype": by_archetype,
        }
        payload["detection_caveat"] = evaluate.DETECTION_CAVEAT

    notes = [SEEDED_NOTE if payload["default_ledger"] else TUNING_NOTE]
    if labels is None:
        notes.append(NO_LABELS_NOTE)
    if payload["entries"] < SMALL_LEDGER:
        notes.append(SMALL_NOTE.format(entries=payload["entries"]))
    if "review" in payload and not payload["review"]["exists"]:
        notes.append(NO_REVIEW_DB_NOTE)
    payload["notes"] = notes
    payload["caveat"] = CAVEAT
    return payload


# --- rendering ---------------------------------------------------------------
# The renderers are pure over the payload: the same dict always gives the same
# text, and nothing below reads the ledger, the labels or the clock.

#: Characters that draw nothing or reorder what is drawn: C0/C1 controls, zero-width
#: characters, bidi embeddings, overrides and isolates, the byte-order mark.
_UNSEEN = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")


def _one_line(value: object) -> str:
    """``value`` as one line of visible text; None and NaN as nothing."""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    return _UNSEEN.sub("", " ".join(str(value).split()))


def md_code(value: object) -> str:
    """``value`` as a markdown code span that renders nothing but its own text.

    Ledger text is data. Inside a code span GitHub treats nothing as markup, a
    link, a reference, HTML, an emoji or maths, and a mention notifies nobody.
    Two things a code span does not cover are handled here. A pipe ends a table
    cell even inside one, so the text is split around each pipe and the pipe
    written as an entity between the spans. And a workflow can match on the raw
    body rather than on what is drawn, so a zero-width space after every ``@``
    keeps any ``@name`` in the ledger from appearing in the body at all.
    """
    text = _one_line(value).replace("@", "@\u200b")
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    fence = "`" * (longest + 1)

    def span(part: str) -> str:
        if not part:
            return ""
        # A span's text may not begin or end with a backtick; one space each side is dropped.
        pad = " " if part[0] == "`" or part[-1] == "`" else ""
        return fence + pad + part + pad + fence

    return "&#124;".join(span(part) for part in text.split("|"))


def _ratio(value: float, defined: bool, why: str) -> str:
    return f"{value:.3f}" if defined else f"undefined ({why})"


def _metric_rows(metrics: dict) -> list[tuple[str, str]]:
    """The rule tier's measured quality, with a ratio over nothing said to be undefined."""
    flagged, truth = metrics["flagged"], metrics["true_anomalies"]
    return [
        ("Population", "{:,} entries".format(metrics["population"])),
        ("Injected anomalies", f"{truth:,}"),
        ("Flagged", "{:,} ({:.2%} of the population)".format(flagged, metrics["flag_rate"])),
        ("True positives", "{:,}".format(metrics["true_positives"])),
        ("False positives", "{:,}".format(metrics["false_positives"])),
        ("False negatives", "{:,}".format(metrics["false_negatives"])),
        ("Precision", _ratio(metrics["precision"], flagged > 0, "nothing was flagged")),
        ("Recall", _ratio(metrics["recall"], truth > 0, "the labels mark no anomaly")),
        ("F1", _ratio(metrics["f1"], flagged > 0 and truth > 0, "needs both of the above")),
    ]


def _read_as(definitional: bool | None) -> str:
    if definitional is None:
        return "not a generator archetype"
    return "definitional" if definitional else "measured"


def _years(payload: dict) -> str:
    return ", ".join(str(year) for year in payload["fiscal_years"]) or "none"


def _model_sentence(model: dict) -> str:
    return (
        "The Isolation Forest flags the top {:.1%} of entries by rank: {:,} of {:,}. That count "
        "is set by the budget, not by anything found.".format(
            model["top_pct"], model["flagged"], model["n_entries"])
    )


def _features_sentence(model: dict) -> str:
    text = "{} features supplied, {} used".format(model["n_features_in"], model["n_features_used"])
    if model["dropped_constant"]:
        text += "; dropped as constant: " + ", ".join(model["dropped_constant"])
    return text + "."


def _review_sentences(review: dict) -> list[str]:
    lines = [
        "Entries with a note: {:,}. Decided: {:,} of {:,} flagged entries. Outstanding: {:,}."
        .format(review["narratives"], review["decided_flagged"], review["flagged"],
                review["outstanding"])
    ]
    if review["by_decision"]:
        kinds = ", ".join(f"{name} {count:,}" for name, count in review["by_decision"].items())
        line = f"Latest decision on each decided entry: {kinds}."
        elsewhere = review["decided"] - review["decided_flagged"]
        if elsewhere > 0:
            line += " {:,} of those entries {} not flagged in this run.".format(
                elsewhere, "is" if elsewhere == 1 else "are")
        lines.append(line)
    if review["other_ledgers"]:
        lines.append("The file also holds rows for {:,} other ledger(s), not counted here."
                     .format(review["other_ledgers"]))
    return lines


def render_text(payload: dict) -> str:
    wrap = textwrap.TextWrapper(width=96, initial_indent="  ", subsequent_indent="  ")
    model, out = payload["model_tier"], []
    out += [
        "LedgerLens summary, {}".format(payload["run_date"]),
        "",
        *textwrap.wrap(payload["caveat"], 96),
        "",
        "Ledger      {}".format(_one_line(payload["ledger"]) or "(a frame, not a file)"),
        "Identity    {}".format(_one_line(payload["ledger_id"])),
        "Population  {:,} entries / {:,} lines; fiscal years {}".format(
            payload["entries"], payload["lines"], _years(payload)),
        "",
        "Rule tier",
        "  {:,} of {:,} entries flagged ({:.2%}); {:,} flags raised (an entry can raise several)"
        .format(payload["flagged"], payload["entries"], payload["flag_rate"],
                payload["flags_raised"]),
    ]
    for test_id, count in payload["flags_by_test"].items():
        name = jets.REGISTRY[test_id][0] if test_id in jets.REGISTRY else ""
        out.append(f"  {_one_line(test_id)}  {name:<38}{count:>7,}")
    out += ["", "Model tier (scored separately; never blended with the rule tier)"]
    out += wrap.wrap(_model_sentence(model)) + wrap.wrap(_features_sentence(model))
    out += ["", "Tier agreement (counts, not quality)"]
    out += [f"  {name:<12}{count:>8,}" for name, count in payload["tier_agreement"].items()]

    if "detection" in payload:
        out += ["", "Detection against the labels (rule tier only)"]
        out += wrap.wrap(payload["detection_caveat"])
        out += [f"  {label:<20}{value}" for label, value in _metric_rows(payload["detection"]["metrics"])]
        out.append("  Recall by archetype")
        for row in payload["detection"]["by_archetype"]:
            caught = "{:,} of {:,}".format(row["caught"], row["n"])
            out.append("    {:<28}{:>12}  {:.3f}  {}".format(
                _one_line(row["anomaly_type"]), caught, row["recall"],
                _read_as(row["definitional"])))

    top = payload["top_exceptions"]
    out += ["", f"Top {len(top)} by rule score (the model score only breaks ties)"]
    width = max((len(_one_line(e["entry_id"])) for e in top), default=0)
    for e in top:
        out.append("  {:<{}}  {}  rule {:>4.1f}  model {:.3f}  {:<10}  {}  ${:,.2f}".format(
            _one_line(e["entry_id"]), width, _one_line(e["posting_date"]), e["risk_score"],
            e["model_score"], _one_line(e["agreement"]), _one_line(e["tests_fired"]),
            e["entry_amount"]))
    if not top:
        out.append("  no entry was flagged")

    if "review" in payload and payload["review"]["exists"]:
        out += ["", "Review ({})".format(_one_line(payload["review"]["review_db"]))]
        for sentence in _review_sentences(payload["review"]):
            out += wrap.wrap(sentence)
    out += ["", "Notes"]
    for note in payload["notes"]:
        out += textwrap.wrap(note, 96, initial_indent="  - ", subsequent_indent="    ")
    return "\n".join(out)


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]], right: tuple[int, ...] = ()) -> list[str]:
    rule = ["---:" if i in right else "---" for i in range(len(header))]
    return ["| " + " | ".join(cells) + " |" for cells in (header, tuple(rule), *rows)]


def render_markdown(payload: dict) -> str:
    """Markdown for a GitHub Issue. Every ledger-derived string goes through ``md_code``."""
    model = payload["model_tier"]
    versions = ", ".join(f"{name} {md_code(version)}" for name, version in payload["versions"].items())
    out = [
        "## LedgerLens summary, {}".format(payload["run_date"]),
        "",
        "> " + payload["caveat"],
        "",
        *_table(("", ""), [
            ("Ledger", "{} ({})".format(md_code(payload["ledger"]) or "a frame, not a file",
                                        md_code(payload["ledger_id"]))),
            ("Population", "{:,} entries, {:,} lines; fiscal years {}".format(
                payload["entries"], payload["lines"], _years(payload))),
            ("Flagged by the rule tier", "{:,} entries ({:.2%})".format(
                payload["flagged"], payload["flag_rate"])),
            ("Flags raised", "{:,} (an entry can raise several)".format(payload["flags_raised"])),
            ("Versions", versions),
        ]),
        "",
        "### Rule tier: flags by test",
        "",
        *_table(("Test", "Name", "Flags"), [
            (md_code(test_id), jets.REGISTRY[test_id][0] if test_id in jets.REGISTRY else "",
             f"{count:,}")
            for test_id, count in payload["flags_by_test"].items()
        ], right=(2,)),
        "",
        "### Model tier",
        "",
        "Scored separately and never blended with the rule tier. " + _model_sentence(model),
        _features_sentence(model),
        "",
        "### Tier agreement",
        "",
        "Counts of how the two tiers relate, not a measure of quality.",
        "",
        *_table(("Tiers", "Entries"), [
            (name, f"{count:,}") for name, count in payload["tier_agreement"].items()
        ], right=(1,)),
    ]

    if "detection" in payload:
        out += [
            "",
            "### Detection against the labels (rule tier only)",
            "",
            "> " + payload["detection_caveat"],
            "",
            *_table(("Metric", "Value"), _metric_rows(payload["detection"]["metrics"])),
            "",
            *_table(("Archetype", "Caught", "Recall", "Read as"), [
                (md_code(row["anomaly_type"]), "{:,} of {:,}".format(row["caught"], row["n"]),
                 "{:.3f}".format(row["recall"]), _read_as(row["definitional"]))
                for row in payload["detection"]["by_archetype"]
            ], right=(1, 2)),
        ]

    top = payload["top_exceptions"]
    out += ["", f"### Top {len(top)} by rule score", "",
            "Ordered by the rule score; the model score only breaks ties.", ""]
    if top:
        out += _table(
            ("Entry", "Posted", "Amount", "Rule score", "Model score", "Tiers", "Tests", "Description"),
            [(md_code(e["entry_id"]), md_code(e["posting_date"]), "{:,.2f}".format(e["entry_amount"]),
              "{:.1f}".format(e["risk_score"]), "{:.3f}".format(e["model_score"]),
              md_code(e["agreement"]), md_code(e["tests_fired"]), md_code(e["description"]))
             for e in top],
            right=(2, 3, 4),
        )
    else:
        out.append("No entry was flagged.")

    if "review" in payload and payload["review"]["exists"]:
        out += ["", "### Review", "", *_review_sentences(payload["review"])]
    out += ["", "### Notes", ""] + [f"- {note}" for note in payload["notes"]]
    return "\n".join(out)


def render(payload: dict, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(payload, indent=2)
    if fmt == "markdown":
        return render_markdown(payload)
    if fmt == "text":
        return render_text(payload)
    raise ValueError(f"unknown format {fmt!r}; expected one of {', '.join(FORMATS)}")

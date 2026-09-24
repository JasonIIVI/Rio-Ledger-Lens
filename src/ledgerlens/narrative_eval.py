"""Measure the narrative layer against a hand-checked rubric.

An eval here is three things: a fixed set of flagged entries, the real
narrator run over each one, and a grader that turns every narrative into a
handful of yes/no properties - the ones a reviewer would check before
trusting a note:

- ``schema_valid``        the contract in :func:`narrate.validate` holds
- ``mentions_required``   the facts a note about *this* entry must contain
                          (the amount, the accounts, the tests that fired)
- ``no_assertions``       it never asserts an error, an intent or an irregularity
- ``evidence_specific``   at least one evidence item names a figure, an account or a date
- ``no_invented_numbers`` every number in the note is in the entry, or is a standard the
                          system prompt cites (AU-C 240 is a citation, not an invention)
- ``confidence_in_band``  confidence lands where a reviewer would put it

Why a rubric and not a reference narrative: the reference would itself be
model-written, and scoring similarity to it rewards imitation rather than
correctness. The rubric is a floor, not a ceiling - a note can pass every
check and still be dull - which is why every narrative is kept for reading.

Only :func:`select_cases` reads the label file, and only to choose which
entries to test. The narrator never sees a label.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .narrate import (
    NarrativeError,
    Narrator,
    Usage,
    build_prompt,
    entry_context,
    validate,
)
from .schema import AnomalyType

#: Wording that turns a question into a finding. Case-insensitive regexes over
#: the whole note. A floor, not a ceiling: a note can be assertive without any
#: of these, and the reader is still the judge.
FORBIDDEN_ASSERTIONS = (
    r"\bfraud",
    r"\bembezzl",
    r"\bintentional",
    r"\bdeliberate",
    r"\bconceal",
    r"\bcover(ed|ing)? up\b",
    r"\bmanipulat",
    r"\bfalsif",
    r"\bfabricat",
    r"\b(is|was|are|were) (clearly |certainly |definitely |obviously )?"
    r"(an error|erroneous|wrong|improper|fictitious|illegitimate|unauthori[sz]ed)\b",
    r"\bto (hide|disguise|evade|circumvent)\b",
)

#: Numbers a note may use without their appearing in the entry: the standards
#: the system prompt cites, and only those. The prompt's style guidance also
#: quotes figures ("$9,912", "Sales Revenue (4000)") as examples of specific
#: wording; a note that copied them onto an entry containing neither would be
#: inventing numbers, which is the exact failure this check exists to catch,
#: so nothing from the prompt is admitted except what it cites.
CITATION_NUMBERS = frozenset({"240"})  # AU-C 240, in the JET-05 reference

METRICS = (
    "schema_valid",
    "mentions_required",
    "no_assertions",
    "evidence_specific",
    "no_invented_numbers",
    "confidence_in_band",
)

#: Statuses a case can end in. ``invalid`` is the model's doing (unusable
#: output) and is graded as a failure; ``error`` is plumbing (a network or
#: API failure) and is kept out of the score entirely.
STATUSES = ("ok", "invalid", "error")

#: What ``cases_sha256`` reads on rows graded before the hash was stored.
UNRECORDED = "unrecorded"


class CasesChangedError(ValueError):
    """A re-grade met rows graded under a different case file than the one given."""


def cases_digest(path: str | Path) -> str:
    """sha256 of the case file's bytes: the provenance every graded row carries.

    A grade only means something relative to the expectations it was made
    against. Recording which file that was is what lets a reader tell a
    re-grade under the same expectations from one under edited ones - the
    difference between applying a grader fix and moving the goalposts.
    """
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class Case:
    entry_id: str
    archetype: str
    tests_fired: str
    why_chosen: str
    must_mention: list[str] = field(default_factory=list)
    must_not_assert: list[str] = field(default_factory=list)
    expected_confidence: list[str] = field(default_factory=lambda: ["high", "medium", "low"])
    notes: str = ""


def load_cases(path: str | Path) -> list[Case]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [Case(**item) for item in payload["cases"]]
    ids = [c.entry_id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate entry ids in the case set")
    for case in cases:
        for pattern in case.must_mention + case.must_not_assert:
            re.compile(pattern)
    return cases


def save_cases(cases: list[Case], path: str | Path, generator: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "about": (
            "Narrative eval cases. Selected deterministically from the default synthetic ledger; "
            "expectations are hand-written and reviewed. Regexes are case-insensitive."
        ),
        "generator": generator or {},
        "cases": [asdict(c) for c in cases],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def check_cases(cases: list[Case], scored: pd.DataFrame) -> list[str]:
    """Problems that make the case set stale against this ledger, if any."""
    by_id = scored.set_index("entry_id")["tests_fired"]
    problems = []
    for case in cases:
        if case.entry_id not in by_id.index:
            problems.append(f"{case.entry_id}: not in the ledger")
        elif by_id[case.entry_id] != case.tests_fired:
            problems.append(
                f"{case.entry_id}: tests fired {by_id[case.entry_id]!r}, case expects {case.tests_fired!r}"
            )
    return problems


# --- choosing what to test ------------------------------------------------------


def select_cases(
    scored: pd.DataFrame,
    flags: pd.DataFrame,
    labels: pd.DataFrame,
    n_multi: int = 3,
    n_benign: int = 2,
) -> list[Case]:
    """A deterministic case set from a labelled ledger.

    One entry per injected archetype (the highest-risk flagged one), a few
    entries several tests fired on, and a couple of ordinary entries that only
    the access-list test caught - the note has to say the innocent reading is
    the likely one. This is the one place labels are read, and only to choose.
    """
    flagged = scored[scored["risk_score"] > 0]
    chosen: list[Case] = []
    for archetype in AnomalyType.ALL:
        ids = set(labels.loc[labels["anomaly_type"] == archetype, "entry_id"])
        pool = flagged[flagged["entry_id"].isin(ids)].sort_values(
            ["risk_score", "entry_id"], ascending=[False, True]
        )
        if pool.empty:
            continue
        row = pool.iloc[0]
        chosen.append(Case(
            entry_id=row["entry_id"], archetype=archetype, tests_fired=row["tests_fired"],
            why_chosen=f"highest-risk flagged entry injected as {archetype}",
        ))

    # Multi-flag entries, preferring combinations of tests the set does not
    # already contain: three copies of the same pattern would test one thing.
    taken = {c.entry_id for c in chosen}
    seen = {c.tests_fired for c in chosen}
    multi = flagged[(flagged["n_flags"] >= 2) & ~flagged["entry_id"].isin(taken)].sort_values(
        ["risk_score", "entry_id"], ascending=[False, True]
    )
    picked = 0
    for prefer_new in (True, False):
        for row in multi.itertuples():
            if picked >= n_multi or row.entry_id in taken:
                continue
            if prefer_new and row.tests_fired in seen:
                continue
            chosen.append(Case(
                entry_id=row.entry_id, archetype="multi_flag", tests_fired=row.tests_fired,
                why_chosen="several tests fired on one entry; the note has to weigh them",
            ))
            taken.add(row.entry_id)
            seen.add(row.tests_fired)
            picked += 1

    normal = set(labels.loc[~labels["is_anomaly"], "entry_id"])
    benign = flagged[
        (flagged["tests_fired"] == "JET-12") & flagged["entry_id"].isin(normal)
    ].sort_values("entry_id").head(n_benign)
    for row in benign.itertuples():
        chosen.append(Case(
            entry_id=row.entry_id, archetype="benign", tests_fired=row.tests_fired,
            why_chosen=(
                "an ordinary entry only the access-list test caught; the note should say the "
                "innocent reading is more likely"
            ),
            expected_confidence=["low"],
        ))
    return chosen


# --- grading -------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_LINE_NAME = re.compile(r"^- line \d+: \S+ (.+?) \|", re.M)


def numbers_in(text: str) -> set[str]:
    """Numeric tokens worth checking, normalised so $9,900.00, 9,900 and 9900 agree.

    Tokens under three digits are ignored: test ids, line numbers and small
    counts are not the kind of number a model invents.
    """
    found = set()
    for match in _NUMBER.finditer(text):
        token = match.group(0).replace(",", "")
        if token.endswith(".00"):
            token = token[:-3]
        if len(token.replace(".", "")) >= 3:
            found.add(token)
    return found


def narrative_text(narrative: dict) -> str:
    return "\n".join([
        str(narrative.get("summary", "")),
        str(narrative.get("why_flagged", "")),
        *[str(item) for item in narrative.get("evidence_to_request", [])],
        str(narrative.get("suggested_control", "")),
    ])


def _is_specific(item: str, prompt_text: str) -> bool:
    """An evidence item is specific if it names a number (amount, date, code) or an account."""
    if re.search(r"\d", item):
        return True
    names = _LINE_NAME.findall(prompt_text)
    return any(name.lower() in item.lower() for name in names)


def grade(narrative: dict | None, case: Case, prompt_text: str) -> dict[str, bool]:
    """Turn one narrative into the yes/no properties above, plus ``passed``."""
    clean = None
    if narrative is not None:
        try:
            clean = validate(narrative)
        except NarrativeError:
            clean = None
    if clean is None:
        metrics = {m: False for m in METRICS}
        metrics["passed"] = False
        return metrics

    text = narrative_text(clean)
    forbidden = list(FORBIDDEN_ASSERTIONS) + list(case.must_not_assert)
    shown = numbers_in(prompt_text) | CITATION_NUMBERS
    metrics = {
        "schema_valid": True,
        "mentions_required": all(re.search(p, text, re.I) for p in case.must_mention),
        "no_assertions": not any(re.search(p, text, re.I) for p in forbidden),
        "evidence_specific": any(_is_specific(i, prompt_text) for i in clean["evidence_to_request"]),
        "no_invented_numbers": numbers_in(text) <= shown,
        "confidence_in_band": clean["confidence"] in case.expected_confidence,
    }
    metrics["passed"] = all(metrics.values())
    return metrics


def failed_checks(row: dict) -> list[str]:
    metrics = row.get("metrics") or {}
    return [m for m in METRICS if not metrics.get(m, False)]


def aggregate(rows: list[dict]) -> dict:
    """Pass rates over graded cases. Errors are counted, never averaged in."""
    graded = [r for r in rows if r["status"] in ("ok", "invalid")]
    n = len(graded)
    rates = {m: round(sum(1 for r in graded if r["metrics"][m]) / n, 4) if n else 0.0
             for m in (*METRICS, "passed")}
    usage = Usage()
    for r in rows:
        if r.get("usage"):
            usage.add(_UsageView(r["usage"]))
    crossed = [r for r in rows if r.get("previous_cases_sha256")]
    return {
        "cases": len(rows),
        "graded": n,
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "invalid": sum(1 for r in rows if r["status"] == "invalid"),
        "rates": rates,
        "usage": asdict(usage),
        "provenance": {
            "cases_sha256": sorted({r.get("cases_sha256") or UNRECORDED for r in rows}),
            "regraded_at": max((r.get("regraded_at") or "" for r in rows), default="") or None,
            "rows_regraded_across_cases": len(crossed),
            "previous_cases_sha256": sorted({r["previous_cases_sha256"] for r in crossed}),
        },
    }


class _UsageView:
    """Lets :meth:`Usage.add` read a usage dict as if it were an API object."""

    def __init__(self, data: dict) -> None:
        self.__dict__.update(data)


# --- running -------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _regrade(
    done: dict[str, dict], cases: list[Case], cases_sha256: str | None, allow_cases_change: bool,
) -> None:
    """Re-score stored rows in place, refusing to cross a case-file change quietly."""
    by_id = {c.entry_id: c for c in cases}
    rows = [r for r in done.values() if r["status"] != "error" and r["entry_id"] in by_id]
    current = cases_sha256 or UNRECORDED
    crossed = {r["entry_id"]: r.get("cases_sha256") or UNRECORDED for r in rows
               if (r.get("cases_sha256") or UNRECORDED) != current}
    if crossed and not allow_cases_change:
        previous = ", ".join(sorted({h[:12] for h in crossed.values()}))
        raise CasesChangedError(
            f"{len(crossed)} row(s) were graded under a different case file ({previous}) than "
            f"the current one ({current[:12]}), so re-scoring them would compare against "
            "expectations that may have moved. Pass --allow-cases-change to do it anyway; the "
            "report will say so."
        )
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for row in rows:
        row["metrics"] = grade(row["narrative"], by_id[row["entry_id"]], row["prompt"])
        row["regraded_at"] = now
        if row["entry_id"] in crossed:
            # Keep the earliest known origin: a second crossing must not erase the first.
            row.setdefault("previous_cases_sha256", crossed[row["entry_id"]])
            row["cases_sha256"] = cases_sha256


def run_eval(
    cases: list[Case],
    narrator: Narrator,
    scored: pd.DataFrame,
    flags: pd.DataFrame,
    lines: pd.DataFrame,
    out_dir: str | Path,
    resume: bool = True,
    regrade: bool = False,
    cases_sha256: str | None = None,
    allow_cases_change: bool = False,
) -> list[dict]:
    """Run the real narrator over every case and grade the result.

    Rows are written as each case completes, so a crash costs nothing already
    paid for, and a re-run with ``resume`` skips them. API failures go to an
    ``errors.jsonl`` sidecar rather than into the score. ``regrade`` re-scores
    the stored narratives with the current grader without calling the API,
    which is how a grader fix is applied to a run already paid for.

    Every row records ``cases_sha256``, the case file it was graded against.
    A re-grade under a different file is refused unless ``allow_cases_change``
    says otherwise, and then the row keeps the hash it was first graded under
    so the report can disclose it. Applying a grader fix leaves no such mark;
    editing expectations after seeing the output cannot avoid one.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path, errors_path = out_dir / "results.jsonl", out_dir / "errors.jsonl"
    done = {r["entry_id"]: r for r in _read_jsonl(results_path)} if resume else {}
    if regrade and done:
        _regrade(done, cases, cases_sha256, allow_cases_change)
        results_path.write_text(
            "".join(json.dumps(r) + "\n" for r in done.values()), encoding="utf-8"
        )
    if any(c.entry_id not in done for c in cases):
        _ = narrator.client  # a missing key fails here, once, not once per case as "invalid"

    rows: list[dict] = []
    for case in cases:
        if case.entry_id in done:
            rows.append(done[case.entry_id])
            continue
        entry, entry_flags, entry_lines = entry_context(scored, flags, lines, case.entry_id)
        prompt = build_prompt(entry, entry_flags, entry_lines)
        narrator.usage = Usage()
        started = time.perf_counter()
        narrative, status, error = None, "ok", None
        try:
            narrative = narrator.narrate_one(prompt)
        except NarrativeError as exc:
            status, error = "invalid", str(exc)
        except Exception as exc:  # noqa: BLE001 - plumbing; recorded, never scored
            status, error = "error", f"{type(exc).__name__}: {exc}"
        row = {
            "entry_id": case.entry_id,
            "archetype": case.archetype,
            "tests_fired": case.tests_fired,
            "status": status,
            "error": error,
            "model": narrator.model,
            "usage": asdict(narrator.usage),
            "latency_s": round(time.perf_counter() - started, 2),
            "narrative": narrative,
            "metrics": grade(narrative, case, prompt) if status != "error" else None,
            "prompt": prompt,
            "graded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cases_sha256": cases_sha256,
        }
        target = errors_path if status == "error" else results_path
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        rows.append(row)
    return rows


def _provenance_lines(summary: dict, runs_dir: str | Path | None) -> list[str]:
    """Which case file graded these rows, where the rows are, and any crossing."""
    provenance = summary.get("provenance") or {}
    hashes = provenance.get("cases_sha256") or []
    parts = ["Case file sha256: " + (
        " / ".join(f"`{h}`" for h in hashes) if hashes else UNRECORDED)]
    if runs_dir:
        parts.append(f"rows: `{Path(runs_dir).as_posix()}/results.jsonl`")
    if provenance.get("regraded_at"):
        parts.append(f"re-graded {provenance['regraded_at']} by the grader in this commit, "
                     "no API calls")
    lines = [" · ".join(parts)]
    crossed = provenance.get("rows_regraded_across_cases") or 0
    if crossed:
        previous = provenance.get("previous_cases_sha256") or []
        origins = " / ".join(
            "one whose hash was not recorded (the rows predate provenance tracking)"
            if h == UNRECORDED else f"`{h}`" for h in previous
        )
        lines += [
            "",
            f"**Provenance note:** {crossed} row(s) were first graded under a different case "
            f"file - {origins} - and re-graded under the one above. If the expectations differ "
            "between the two files, the re-graded score is not the original run's score; "
            "compare the files before reading it as one.",
        ]
    return lines


def render_markdown(
    rows: list[dict], summary: dict, runs_dir: str | Path | None = None,
) -> str:
    """The report that goes in docs/: what was measured, the numbers, every miss."""
    model = next((r["model"] for r in rows if r.get("model")), "unknown")
    when = max((r["graded_at"] for r in rows), default="")
    usage = summary["usage"]
    lines = [
        "# Narrative eval",
        "",
        f"Run: {when} · model: `{model}` · cases: {summary['cases']} · graded: "
        f"{summary['graded']} · invalid: {summary['invalid']} · errors (not scored): "
        f"{summary['errors']}",
        "",
        *_provenance_lines(summary, runs_dir),
        "",
        "## What this measures",
        "",
        "Each case is a flagged entry from the default synthetic ledger. The real narrator writes "
        "the note; the grader checks properties a reviewer would check before trusting it. These "
        "are floors, not a quality score: a note can pass every check and still be dull, so the "
        "narratives themselves are kept in the results file for reading. Expectations were written "
        "by hand from the entry's own data, never from a model's output.",
        "",
        "| Check | Pass rate |",
        "|---|---:|",
    ]
    labels = {
        "schema_valid": "Contract holds (five keys, non-empty, valid confidence)",
        "mentions_required": "Mentions the required facts (amount, accounts, tests)",
        "no_assertions": "Asserts no error, intent or irregularity",
        "evidence_specific": "At least one evidence item is specific",
        "no_invented_numbers": "Every number in the note is in the entry (or a cited standard)",
        "confidence_in_band": "Confidence in the expected band",
        "passed": "**All of the above**",
    }
    for metric in (*METRICS, "passed"):
        lines.append(f"| {labels[metric]} | {summary['rates'][metric]:.0%} |")
    lines += [
        "",
        "## Per case",
        "",
        "| Entry | Archetype | Tests | Confidence | Failed checks |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        conf = (r.get("narrative") or {}).get("confidence", "-")
        failed = ", ".join(failed_checks(r)) if r["status"] != "error" else f"error: {r['error']}"
        lines.append(f"| {r['entry_id']} | {r['archetype']} | {r['tests_fired']} | {conf} | "
                     f"{failed or '-'} |")
    misses = [r for r in rows if r["status"] != "error" and failed_checks(r)]
    if misses:
        lines += ["", "## Misses, with the text that failed", ""]
        for r in misses:
            lines.append(f"### {r['entry_id']} ({r['archetype']}): {', '.join(failed_checks(r))}")
            lines.append("")
            if r.get("narrative"):
                lines.append("> " + narrative_text(r["narrative"]).replace("\n", "\n> "))
            else:
                lines.append(f"> {r['error']}")
            lines.append("")
    lines += [
        "",
        "## Cost",
        "",
        f"{usage['requests']} requests · {usage['input_tokens']:,} uncached input tokens · "
        f"{usage['cache_read_input_tokens']:,} read from cache · "
        f"{usage['cache_creation_input_tokens']:,} written to cache · "
        f"{usage['output_tokens']:,} output tokens",
        "",
    ]
    return "\n".join(lines)

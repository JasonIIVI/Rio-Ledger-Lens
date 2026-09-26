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
- ``no_invented_numbers`` every number in the note is in the entry, once the one standard
                          the system prompt cites (AU-C 240) is set aside
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
import inspect
import json
import os
import re
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import narrate
from .ingest import ledger_digest
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

#: The standards the system prompt cites, and only those. A citation is removed
#: from the note before its numbers are extracted, so "AU-C 240" is not an
#: invented figure but a bare "240" ("$240", "240 days") still has to come from
#: the entry. The prompt's style guidance also quotes figures ("$9,912",
#: "Sales Revenue (4000)") as examples of specific wording; a note that copied
#: them onto an entry containing neither would be inventing numbers, which is
#: the exact failure this check exists to catch, so nothing else from the
#: prompt is set aside.
#: Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar:
#: models write the standard's name in all of them.
CITATIONS = (
    re.compile(r"\bAU[-\u2010-\u2015]C[-\u2010-\u2015\s]*(?:section\s*|\u00a7\s*)?240\b", re.I),
)


def strip_citations(text: str) -> str:
    """Remove the cited standards from a note so their figures are not counted."""
    for pattern in CITATIONS:
        text = pattern.sub(" ", text)
    return text

#: What has changed in the grader and what each change did to the published
#: score, printed in every report. A reader should never have to find this in
#: a commit message, and a grader that could be fixed without disclosure could
#: also be loosened without it.
GRADER_NOTES = (
    ("2026-09-23",
     "The first real run scored 88% on all checks. One of the two misses was the grader's: "
     "no_invented_numbers counted the citation \"AU-C 240\" as an invented number. The fix "
     "admitted every number in the system prompt as shown to the model, and the run was "
     "re-graded offline to 94%."),
    ("2026-09-24",
     "That fix was too broad. The system prompt's style example quotes $9,912 and account "
     "4000, so a note that copied the example onto an entry with neither would have passed. "
     "The union with the prompt was replaced by an explicit citation allowlist (AU-C 240 "
     "only). Re-grading the 2026-09-23 run under it changed no row: no note had used either "
     "figure, and the score stayed at 94%. Those rows predate the case-file hash; git records "
     "the case file as unchanged since commit 1ff36ef (2026-09-23 19:29 UTC), before the run "
     "was graded (19:57 UTC). That is consistent with the bands having been fixed first, "
     "which is as much as a commit history can show."),
    ("2026-09-25",
     "The allowlist admitted any bare \"240\" (\"$240\", \"240 days\"), not the citation. "
     "Now the citation \"AU-C 240\", in the forms it is written in (\"AU-C Section 240\", "
     "\"AU-C \u00a7240\", dash variants), is removed from the note before its numbers are "
     "extracted, and every remaining number must come from the entry. Rows record the grader's "
     "own sha256 (the grading code, its patterns and word lists, and the schema check it calls) "
     "and keep replaced grades under metrics_history, so a grader change shows on the rows and "
     "not only here; a test fails if the committed rows were not graded by the grader in the "
     "same commit. One offline re-grade of the 2026-09-23 run from its previous rows changed no "
     "check on any row; the score stayed at 94%."),
)

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
#: API failure) and is kept out of the score entirely; ``missing`` is a case
#: a re-grade found no stored row for, reported rather than narrated.
STATUSES = ("ok", "invalid", "error", "missing")

#: What ``cases_sha256`` reads on rows graded before the hash was stored.
UNRECORDED = "unrecorded"

#: Where a run's rows live, in the repository next to the case file. The rows
#: are the narratives and prompts for synthetic entries, so publishing them
#: costs nothing and lets anyone re-grade a run rather than take the report's
#: word for it.
RUNS_ROOT = Path("evals/narratives/runs")

#: The committed case file and the published report, next to the runs: what
#: `ledgerlens eval-narratives` writes by default.
CASES_FILE = Path("evals/narratives/cases.json")
REPORT_FILE = Path("docs/narrative-eval.md")

#: The directories under the checkout that hold committed eval files. Rule 1
#: at the point of writing: nothing built from any ledger but the generator's
#: default may land in them, however the path is spelled.
COMMITTED_DIRS = ("evals", "docs")

#: The checkout this module was imported from (an editable install); the
#: current directory covers a run from the repository root. Both are checked.
_CHECKOUT = Path(__file__).resolve().parents[2]


class CommittedPathError(ValueError):
    """Rows, cases or a report built from another ledger, aimed at a committed path."""


class LedgerChangedError(ValueError):
    """Stored rows were built from a ledger other than the one on hand."""


def _within(target: Path, root: Path) -> bool:
    if target.is_relative_to(root):
        return True
    # A case-insensitive disk spells evals/ as EVALS/ too: compare what
    # exists by identity, not by name.
    return root.exists() and any(
        p.exists() and p.samefile(root) for p in (target, *target.parents))


def refuse_committed_path(path: str | Path, ledger_sha256: str | None, what: str) -> None:
    """Refuse a write into a committed eval path unless the ledger is the default one.

    A missing digest counts as "not the default": a caller that cannot say
    which ledger its text came from does not get to publish it.
    """
    if not str(path).strip():
        raise CommittedPathError(f"{what}: an empty path names no destination")
    if ledger_sha256 == DEFAULT_LEDGER_SHA256:
        return
    target = Path(path).expanduser().resolve()
    for base in {Path.cwd().resolve(), _CHECKOUT}:
        for name in COMMITTED_DIRS:
            root = (base / name).resolve()
            if _within(target, root):
                raise CommittedPathError(
                    f"{what} {target} is under {root}, which is committed and holds files for "
                    f"the default synthetic ledger only (sha256 {DEFAULT_LEDGER_SHA256[:12]}); "
                    f"this ledger hashes to {(ledger_sha256 or 'unknown')[:12]}. Name a path "
                    "outside evals/ and docs/ for it (out/ is gitignored), and never commit "
                    "rows, cases or reports built from real data."
                )


def _hashes(rows, key: str) -> list[str]:
    """The distinct values of one provenance hash over rows, in a fixed order."""
    return sorted({r.get(key) or UNRECORDED for r in rows})


def default_runs_dir(model: str, regrade: bool = False, root: str | Path = RUNS_ROOT) -> Path:
    """Where a run's rows go when the caller does not say.

    A new run gets a directory named for the UTC date and the model, so runs
    sit side by side and a later one never overwrites an earlier one. A
    re-grade has to find rows that already exist, so it takes the newest
    directory for that model instead.
    """
    root = Path(root)
    if not regrade:
        return root / f"{datetime.now(timezone.utc):%Y-%m-%d}-{model}"
    existing = sorted(p for p in root.glob(f"*-{model}") if p.is_dir())
    if not existing:
        raise FileNotFoundError(f"no run for {model} under {root} to re-grade; pass --runs-dir")
    return existing[-1]


class CasesChangedError(ValueError):
    """A re-grade met rows graded under a different case file than the one given."""


class RegradeError(ValueError):
    """A re-grade was asked for something a re-grade cannot do.

    Start over, or run without the case-file hash. A ValueError so that
    callers who catch the general class still stop, but the CLI maps only
    this one (and :class:`ResultsError`) to a usage error rather than hiding
    real bugs behind it.
    """


class ResultsError(ValueError):
    """A results file cannot be trusted: it holds two rows for one case."""


#: What `ledgerlens generate` writes with its defaults (seed 20260922), as
#: :func:`ledger_digest` sees it. The committed runs directory holds rows for
#: this ledger only; a test ties the constant to the generator. When the
#: generator changes on purpose this moves with it: take the new value from
#: the failing pin test, then regenerate the case file.
DEFAULT_LEDGER_SHA256 = "646e73bb3942329e452bd2414bb5aa82f8a87f7971e27ddbed2c95d930e6bf5b"


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


def save_cases(
    cases: list[Case], path: str | Path, generator: dict | None = None,
    ledger_sha256: str | None = None,
) -> Path:
    refuse_committed_path(path, ledger_sha256, "the case file")
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
    shown = numbers_in(prompt_text)
    metrics = {
        "schema_valid": True,
        "mentions_required": all(re.search(p, text, re.I) for p in case.must_mention),
        "no_assertions": not any(re.search(p, text, re.I) for p in forbidden),
        "evidence_specific": any(_is_specific(i, prompt_text) for i in clean["evidence_to_request"]),
        "no_invented_numbers": numbers_in(strip_citations(text)) <= shown,
        "confidence_in_band": clean["confidence"] in case.expected_confidence,
    }
    metrics["passed"] = all(metrics.values())
    return metrics


def confidence_baselines(cases: list[Case]) -> dict[str, tuple[int, int]]:
    """What a narrator that always answered one level would score on confidence_in_band.

    The bands are deliberately wide (most cases accept two of the three
    levels), which makes the check forgiving; printing the constant-answer
    baseline next to the rate says how forgiving. The bands themselves are
    only ever changed for a future case-set revision, before its run.
    """
    return {level: (sum(level in c.expected_confidence for c in cases), len(cases))
            for level in ("high", "medium", "low")}


def grader_digest() -> str:
    """sha256 of the grader itself: the grading code, the word lists and patterns
    it reads, and the schema check it calls.

    Recorded on every row, so a change to how notes are judged is as visible
    as a change to what they are judged against. Whitespace and comments
    count: any edit to the grader is a change a reader may want to see. The
    case-file side (``Case``, ``load_cases``) is not in it; that is what
    ``cases_sha256`` is for. Computed on demand, never at import: it reads
    source files, and the CLI imports this module for every command.
    """
    graders = (grade, numbers_in, strip_citations, narrative_text, _is_specific, validate)
    try:
        parts = [inspect.getsource(f) for f in graders]
    except OSError as exc:
        raise RuntimeError(
            "the grader's source is not available, so its sha256 cannot be recorded; run the "
            "eval from a source checkout"
        ) from exc
    parts += [
        repr(FORBIDDEN_ASSERTIONS), repr([p.pattern for p in CITATIONS]), repr(METRICS),
        _NUMBER.pattern, _LINE_NAME.pattern,
        repr(narrate.REQUIRED_KEYS), repr(narrate.VALID_CONFIDENCE),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


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
    regraded = [r for r in graded if r.get("metrics_history")]
    changed = [r for r in regraded
               if (r["metrics_history"][0]["metrics"] or {}).get("passed") != r["metrics"]["passed"]]
    # A row's kept grades start at its first re-grade; if that came later than
    # the row's own grading, an earlier grade was overwritten before history
    # existed, and the report has to say so rather than imply "since the start".
    unkept = [r for r in regraded if r["metrics_history"][0].get("graded_at") != r.get("graded_at")]
    return {
        "cases": len(rows),
        "graded": n,
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "invalid": sum(1 for r in rows if r["status"] == "invalid"),
        "missing": sum(1 for r in rows if r["status"] == "missing"),
        "rates": rates,
        "usage": asdict(usage),
        "provenance": {
            "cases_sha256": _hashes(rows, "cases_sha256"),
            "grader_sha256": _hashes(graded, "grader_sha256"),
            "ledger_sha256": _hashes(graded, "ledger_sha256"),
            "regraded_at": max((r.get("regraded_at") or "" for r in rows), default="") or None,
            "rows_regraded": len(regraded),
            "rows_with_changed_result": len(changed),
            "earliest_kept_grade_at": min(
                (r["metrics_history"][0].get("graded_at") or "" for r in regraded), default=""
            ) or None,
            "rows_with_unkept_grades": len(unkept),
            "first_graded_at": min((r.get("graded_at") or "" for r in unkept), default="") or None,
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


def _stored_rows(path: Path) -> dict[str, dict]:
    """The rows in a results file, keyed by entry id.

    Rows are appended once per case, so two rows for one entry can only come
    from a hand edit or a damaged file. Merging them would pick one silently;
    refusing points at the rule that rows are never edited by hand.
    """
    rows = _read_jsonl(path)
    duplicates = sorted(i for i, n in Counter(r["entry_id"] for r in rows).items() if n > 1)
    if duplicates:
        raise ResultsError(
            f"{path} holds more than one row for {', '.join(duplicates)}; rows are never edited "
            "by hand, so this file cannot be trusted. Restore it (git checkout for a committed "
            "run) or use a fresh --runs-dir."
        )
    return {r["entry_id"]: r for r in rows}


def _write_rows(path: Path, rows) -> None:
    """Replace a results file in one step, so a crash mid-write leaves the old file whole."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".results-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in rows))
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates the file owner-only; the replaced file keeps its own mode.
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
    grader = grader_digest()
    for row in rows:
        # The grades being replaced are kept, with what produced them, so a
        # grader change shows on the row itself and not only in a note.
        row.setdefault("metrics_history", []).append({
            "metrics": row["metrics"],
            "grader_sha256": row.get("grader_sha256") or UNRECORDED,
            "cases_sha256": row.get("cases_sha256") or UNRECORDED,
            "graded_at": row.get("regraded_at") or row.get("graded_at"),
        })
        row["metrics"] = grade(row["narrative"], by_id[row["entry_id"]], row["prompt"])
        row["grader_sha256"] = grader
        row["regraded_at"] = now
        if row["entry_id"] in crossed:
            # Keep the earliest known origin: a second crossing must not erase the first.
            row.setdefault("previous_cases_sha256", crossed[row["entry_id"]])
            row["cases_sha256"] = cases_sha256


#: Every key of a stored row, in the order run_eval writes them. A missing
#: row carries the same keys, so a reader can treat the list uniformly.
_ROW_KEYS = (
    "entry_id", "archetype", "tests_fired", "status", "error", "model", "usage", "latency_s",
    "narrative", "metrics", "prompt", "graded_at", "cases_sha256", "grader_sha256",
    "ledger_sha256",
)


def _missing_row(case: Case, cases_sha256: str | None, errored: bool = False) -> dict:
    """The row for a case a re-grade found nothing stored for. Never written to disk."""
    why = "no stored result row for this case; a re-grade never narrates (run without --regrade)"
    if errored:
        why += " (the narrator failed on it: see errors.jsonl)"
    row = dict.fromkeys(_ROW_KEYS)
    row.update({
        "entry_id": case.entry_id, "archetype": case.archetype, "tests_fired": case.tests_fired,
        "status": "missing", "error": why, "cases_sha256": cases_sha256,
    })
    return row


def _require_rows_from_this_ledger(
    done: dict[str, dict], cases: list[Case], scored: pd.DataFrame, flags: pd.DataFrame,
    lines: pd.DataFrame, ledger_sha256: str,
) -> None:
    """Prove each stored row's prompt came from the ledger on hand, then record its digest.

    A resumed run reuses stored rows and a re-grade rewrites them, so both
    have to know the rows belong to this ledger: a row that names another
    ledger, or whose prompt does not rebuild from this one, is refused rather
    than blended in. A re-grade is also where rows written before
    ``ledger_sha256`` existed gain it, once their prompt has matched.
    """
    by_id = {c.entry_id: c for c in cases}
    for entry_id, row in done.items():
        if entry_id not in by_id:
            continue  # a re-grade refuses these earlier; a resume carries them untouched
        recorded = row.get("ledger_sha256")
        if recorded and recorded != ledger_sha256:
            raise LedgerChangedError(
                f"row {entry_id} records ledger sha256 {recorded[:12]}, but this ledger hashes "
                f"to {ledger_sha256[:12]}; use the ledger the rows were built from, or a fresh "
                "--runs-dir"
            )
        try:
            entry, entry_flags, entry_lines = entry_context(scored, flags, lines, entry_id)
        except KeyError:
            raise LedgerChangedError(
                f"row {entry_id} is not an entry in this ledger; use the ledger the rows were "
                "built from, or a fresh --runs-dir"
            ) from None
        if build_prompt(entry, entry_flags, entry_lines) != row.get("prompt"):
            raise LedgerChangedError(
                f"the stored prompt for {entry_id} does not rebuild from this ledger, so the row "
                "was built from a different ledger or generator; use the ledger it came from, "
                "or a fresh --runs-dir"
            )
        row["ledger_sha256"] = ledger_sha256


def _regrade_run(
    cases: list[Case], results_path: Path, cases_sha256: str | None,
    allow_cases_change: bool, resume: bool,
    scored: pd.DataFrame, flags: pd.DataFrame, lines: pd.DataFrame, ledger_sha256: str,
) -> list[dict]:
    """Re-score stored rows and nothing else: no narrator is in reach here."""
    if not resume:
        raise RegradeError("a re-grade re-scores stored rows; it cannot start over (--no-resume)")
    if cases_sha256 is None:
        raise RegradeError("a re-grade needs cases_sha256, so the rows can say what graded them")
    done = _stored_rows(results_path)
    if not done:
        raise FileNotFoundError(f"nothing to re-grade: {results_path} has no rows")
    # A re-grade covers every stored row, or the file would carry two grader
    # hashes and the report would quietly drop the rows it did not score.
    left_out = sorted(set(done) - {c.entry_id for c in cases})
    if left_out:
        raise RegradeError(
            f"{len(left_out)} stored row(s) are not in the case set ({', '.join(left_out)}); a "
            "re-grade covers every stored row, so run it without --limit, or move rows that no "
            "longer belong to a case to another --runs-dir"
        )
    errored = {r["entry_id"] for r in _read_jsonl(results_path.with_name("errors.jsonl"))}
    _require_rows_from_this_ledger(done, cases, scored, flags, lines, ledger_sha256)
    _regrade(done, cases, cases_sha256, allow_cases_change)
    _write_rows(results_path, done.values())
    return [done[c.entry_id] if c.entry_id in done
            else _missing_row(c, cases_sha256, errored=c.entry_id in errored)
            for c in cases]


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
    the stored narratives with the current grader, which is how a grader fix
    is applied to a run already paid for; it never calls the API, so a case
    with no stored row comes back as ``missing`` rather than narrated, and a
    directory with nothing stored is an error rather than a paid run.

    Every row records ``cases_sha256``, the case file it was graded against,
    and ``ledger_sha256``, the digest of ``lines``, the ledger its prompt was
    built from (see :func:`ledger_digest`; a re-grade rebuilds each stored
    prompt from ``lines`` before it records the digest on rows that predate
    it). A re-grade under a different case file is refused unless
    ``allow_cases_change`` says otherwise, and then the row keeps the hash it
    was first graded under so the report can disclose it. Applying a grader
    fix leaves no such mark; editing expectations after seeing the output
    cannot avoid one.

    Stored rows are never deleted: a run that does not resume into a
    directory that already holds rows is refused, so a worse result cannot
    be quietly replaced by a better one. A committed directory (under the
    checkout's evals/ or docs/) is refused for any ledger but the default.
    """
    ledger_sha256 = ledger_digest(lines)
    refuse_committed_path(out_dir, ledger_sha256, "the runs directory")
    out_dir = Path(out_dir)
    results_path, errors_path = out_dir / "results.jsonl", out_dir / "errors.jsonl"
    if regrade:
        return _regrade_run(cases, results_path, cases_sha256, allow_cases_change, resume,
                            scored, flags, lines, ledger_sha256)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not resume:
        for path in (results_path, errors_path):
            if path.exists():
                raise FileExistsError(
                    f"{path} already holds rows; a run that does not resume needs a fresh "
                    "--runs-dir (stored rows are never deleted)"
                )
    done = _stored_rows(results_path) if resume else {}
    if done:
        # Stored rows are reused as they are, so they must be this ledger's:
        # a run that stopped half-way must not resume on a different export.
        _require_rows_from_this_ledger(done, cases, scored, flags, lines, ledger_sha256)
    grader = None
    if any(c.entry_id not in done for c in cases):
        _ = narrator.client  # a missing key fails here, once, not once per case as "invalid"
        grader = grader_digest()

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
            "grader_sha256": grader,
            "ledger_sha256": ledger_sha256,
        }
        target = errors_path if status == "error" else results_path
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        rows.append(row)
    return rows


def _provenance_lines(summary: dict, runs_dir: str | Path | None) -> list[str]:
    """Which case file graded these rows, where the rows are, and any crossing."""
    provenance = summary.get("provenance") or {}
    parts = []
    for label, key in (("Case file sha256", "cases_sha256"), ("grader sha256", "grader_sha256"),
                       ("ledger sha256", "ledger_sha256")):
        hashes = provenance.get(key) or []
        parts.append(f"{label}: " + (
            " / ".join(f"`{h}`" for h in hashes) if hashes else UNRECORDED))
    if runs_dir:
        parts.append(f"rows: `{Path(runs_dir).as_posix()}/results.jsonl`")
    if provenance.get("regraded_at"):
        parts.append(f"re-graded {provenance['regraded_at']} offline (no API calls)")
    lines = [" · ".join(parts)]
    regraded = provenance.get("rows_regraded") or 0
    if regraded:
        changed = provenance.get("rows_with_changed_result") or 0
        lines += [
            "",
            f"Re-grades: {regraded} row(s) keep their earlier grades, with the grader and case "
            f"file that produced them, under `metrics_history`; {changed} row(s) changed their "
            f"overall result since their earliest kept grade "
            f"({provenance.get('earliest_kept_grade_at')}). The grader hash is the sha256 of "
            "the grading code, its word lists and patterns, and the schema check it calls, so "
            "a loosened check would show here as a new hash.",
        ]
        unkept = provenance.get("rows_with_unkept_grades") or 0
        if unkept:
            lines += [
                "",
                f"{unkept} row(s) were first graded earlier ({provenance.get('first_graded_at')}) "
                "and that grade was replaced before `metrics_history` existed; only the grader "
                "notes below describe it.",
            ]
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


def _baseline_lines(cases: list[Case] | None, rows: list[dict]) -> list[str]:
    """The constant-answer baseline for the confidence check, for the cases graded."""
    graded = {r["entry_id"] for r in rows if r["status"] in ("ok", "invalid")}
    cases = [c for c in (cases or []) if c.entry_id in graded]
    if not cases:
        return []
    baselines = confidence_baselines(cases)
    best = max(baselines, key=lambda level: baselines[level][0])
    described = ", ".join(f'always "{level}" {n}/{total} ({n / total:.0%})'
                          for level, (n, total) in baselines.items())
    n, total = baselines[best]
    counted = {c.entry_id for c in cases}  # the same cases the baseline is computed over
    actual = sum(1 for r in rows if r["entry_id"] in counted and r["status"] in ("ok", "invalid")
                 and (r["metrics"] or {}).get("confidence_in_band"))
    margin = actual - n
    cases_word = "case" if abs(margin) == 1 else "cases"
    verdict = (f"beat the best constant answer by {margin} {cases_word}" if margin > 0
               else "match the best constant answer" if margin == 0
               else f"fall short of the best constant answer by {-margin} {cases_word}")
    return [
        "",
        f"Confidence baseline: the bands accept more than one level on "
        f"{sum(len(c.expected_confidence) > 1 for c in cases)} of {total} cases, so a "
        f"narrator that always answered \"{best}\" would pass the confidence check on "
        f"{n}/{total} ({n / total:.0%}) of them - {described}. The notes passed it on "
        f"{actual}/{total}, so they {verdict}. Read the confidence row against that, not "
        f"against zero.",
    ]


def render_markdown(
    rows: list[dict], summary: dict, runs_dir: str | Path | None = None,
    cases: list[Case] | None = None,
) -> str:
    """The report that goes in docs/: what was measured, the numbers, every miss,
    the grader's own history, and the baseline the confidence check should be read against."""
    model = next((r["model"] for r in rows if r.get("model")), "unknown")
    when = max((r["graded_at"] for r in rows if r.get("graded_at")), default="")
    usage = summary["usage"]
    missing = summary.get("missing") or 0
    lines = [
        "# Narrative eval",
        "",
        f"Run: {when} · model: `{model}` · cases: {summary['cases']} · graded: "
        f"{summary['graded']} · invalid: {summary['invalid']} · errors (not scored): "
        f"{summary['errors']}"
        + (f" · missing (no stored row, not narrated): {missing}" if missing else ""),
        "",
        *_provenance_lines(summary, runs_dir),
        "",
        "## What this measures",
        "",
        ("Each case is a flagged entry from the default synthetic ledger. "
         if (summary.get("provenance") or {}).get("ledger_sha256") == [DEFAULT_LEDGER_SHA256]
         else "Each case is a flagged entry from the ledger whose sha256 is shown above, which "
              "is not the default synthetic ledger. ")
        + "The real narrator writes "
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
        "no_invented_numbers": "Every number in the note is in the entry (or is AU-C 240, the "
                               "one standard the prompt cites)",
        "confidence_in_band": "Confidence in the expected band",
        "passed": "**All of the above**",
    }
    for metric in (*METRICS, "passed"):
        lines.append(f"| {labels[metric]} | {summary['rates'][metric]:.0%} |")
    lines += _baseline_lines(cases, rows)
    lines += [
        "",
        "## Per case",
        "",
        "| Entry | Archetype | Tests | Confidence | Failed checks |",
        "|---|---|---|---|---|",
    ]
    unscored = ("error", "missing")
    for r in rows:
        conf = (r.get("narrative") or {}).get("confidence", "-")
        failed = (", ".join(failed_checks(r)) if r["status"] not in unscored
                  else f"{r['status']}: {r['error']}")
        lines.append(f"| {r['entry_id']} | {r['archetype']} | {r['tests_fired']} | {conf} | "
                     f"{failed or '-'} |")
    misses = [r for r in rows if r["status"] not in unscored and failed_checks(r)]
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
    lines += ["", "## Grader notes", ""]
    lines += [f"- **{date}** - {note}" for date, note in GRADER_NOTES]
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

"""Read-only query layer over a scored ledger.

This is what the MCP server exposes, kept free of any MCP import so it runs
and is tested on every Python the project supports (the SDK needs 3.10+).

It is also read-only on purpose. Every method answers a question about the
ledger; none records a decision. That is rule 8 - the model explains and
suggests, a named human decides - expressed as an API surface rather than as a
policy somebody has to remember. The review database is opened with a
read-only SQLite connection, so the database enforces the same thing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from . import jets
from .benford import benford_test, segmented_benford
from .ingest import ledger_identity, load_csv
from .model import combine, score_ledger
from .narrate import entry_context
from .review import ReviewStore

DEFAULT_LEDGER = "data/ledger.csv"
DEFAULT_REVIEW_DB = "data/review.sqlite"
ENV_LEDGER = "LEDGERLENS_LEDGER"
ENV_REVIEW_DB = "LEDGERLENS_REVIEW_DB"

CAVEAT = (
    "A flag is a question, not a finding. Nothing here asserts an error or an irregularity, "
    "and decisions are recorded only by a named reviewer in the dashboard."
)
BENFORD_CAVEAT = (
    "Non-conformity is a pointer, not a finding. Chi-square rejects conformity on almost any "
    "large population; MAD with Nigrini's bands is the operative statistic."
)

#: The columns an entry is described by when it leaves this module.
ENTRY_COLUMNS = (
    "entry_id", "posting_date", "fiscal_year", "period", "source", "created_by", "description",
    "entry_amount", "risk_score", "model_score", "agreement", "tests_fired", "reasons",
)
LINE_COLUMNS = ("line_no", "account_code", "account_name", "account_type", "description",
                "debit", "credit")
FLAG_COLUMNS = ("test_id", "test_name", "severity", "reason")
DECISION_COLUMNS = ("decided_at", "decision", "reviewer", "note", "narrative_id")
#: What each entry row carries from the review store, straight from
#: ReviewStore.review_state (see there for what the narrative fields mean).
REVIEW_FIELDS = ("narrative_id", "narrative_summary", "narrative_confidence", "decision",
                 "reviewer", "narrative_superseded", "narrative_seen_by_reviewer")

#: The two tiers relate in one of these ways; see model.combine.
AGREEMENTS = ("both", "rules only", "model only", "neither")


def records(df: pd.DataFrame, columns: tuple[str, ...] | None = None) -> list[dict]:
    """JSON-safe rows: timestamps to ISO strings, numpy scalars to Python, NaN to None."""
    frame = df[[c for c in columns if c in df.columns]] if columns else df
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def _jsonable(value):
    """Recursively turn a benford result (numpy scalars, int keys) into plain JSON."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


class LedgerContext:
    """A ledger, scored by both tiers once, answering questions many times.

    ``ledger`` is a CSV path or an already-prepared DataFrame (tests). The
    review database is optional and is only ever opened read-only: a server
    must neither leave a file behind nor be able to write into one.
    """

    def __init__(
        self,
        ledger: str | Path | pd.DataFrame,
        review_db: str | Path | None = None,
        model_top_pct: float = 0.02,
        ledger_id: str | None = None,
    ) -> None:
        self._ledger = ledger
        self.review_db = Path(review_db) if review_db else None
        self.model_top_pct = model_top_pct
        #: The identity the review store is bound to; computed from the ledger
        #: at load unless the caller names it (a QuickBooks pull's ``qbo:<realm>``).
        self.ledger_id = ledger_id
        self.lines: pd.DataFrame | None = None
        self.flags: pd.DataFrame | None = None
        self.combined: pd.DataFrame | None = None
        self.model_report = None

    @classmethod
    def from_env(cls) -> LedgerContext:
        return cls(
            os.environ.get(ENV_LEDGER, DEFAULT_LEDGER),
            os.environ.get(ENV_REVIEW_DB, DEFAULT_REVIEW_DB),
        )

    @property
    def loaded(self) -> bool:
        return self.combined is not None

    def load(self) -> LedgerContext:
        """Run both tiers. Idempotent; the first call is the expensive one."""
        if self.loaded:
            return self
        from_file = not isinstance(self._ledger, pd.DataFrame)
        df = load_csv(self._ledger) if from_file else self._ledger
        if self.ledger_id is None:
            self.ledger_id = ledger_identity(df, self._ledger if from_file else None)
        flags = jets.run_all(df)
        scored = jets.score_entries(df, flags)
        model_scores, report = score_ledger(df)
        self.lines, self.flags = df, flags
        self.combined = combine(scored, model_scores, model_top_pct=self.model_top_pct)
        self.model_report = report
        return self

    def _store(self) -> ReviewStore | None:
        if self.review_db is not None and self.review_db.exists():
            self.load()
            return ReviewStore.read_only(self.review_db, self.ledger_id)
        return None

    def _attach_review(self, rows: list[dict]) -> list[dict]:
        """Add what the human loop knows to entry rows, when a store exists.

        Straight from ReviewStore.review_state, which the workpaper reads too:
        the note shown is the version the decision recorded (else the latest),
        and narrative_superseded / narrative_seen_by_reviewer say how the two
        relate, so the model never presents a later note as what the reviewer
        decided on. The latest note and every version are in explain_entry.
        """
        store = self._store()
        if store is None:
            return rows
        state = {r["entry_id"]: r for r in records(store.review_state())}
        for row in rows:
            known = state.get(row["entry_id"], {})
            for field in REVIEW_FIELDS:
                row[field] = known.get(field)
            row["narrative_superseded"] = bool(known.get("narrative_superseded") or False)
            row["narrative_seen_by_reviewer"] = known.get("narrative_seen_by_reviewer") or ""
        return rows

    # --- questions ---------------------------------------------------------

    def summary(self) -> dict:
        """The population in numbers: what was tested, what fired, how review is going."""
        self.load()
        c = self.combined
        flagged = c["risk_score"] > 0
        by_test = self.flags.groupby("test_id").size() if not self.flags.empty else pd.Series(dtype=int)
        return {
            "entries": int(len(c)),
            "lines": int(len(self.lines)),
            "fiscal_years": sorted(int(y) for y in c["fiscal_year"].unique()),
            "flagged": int(flagged.sum()),
            "flag_rate": round(float(flagged.mean()), 4),
            "flags_raised": int(len(self.flags)),
            "flags_by_test": {str(k): int(v) for k, v in by_test.items()},
            "tier_agreement": {str(k): int(v) for k, v in c["agreement"].value_counts().items()},
            "model": self.model_report.describe(),
            "review": self.review_status(),
            "caveat": CAVEAT,
        }

    def top_exceptions(
        self,
        limit: int = 10,
        fiscal_year: int | None = None,
        period_from: int | None = None,
        period_to: int | None = None,
        agreement: str | None = None,
    ) -> dict:
        """The riskiest flagged entries, highest risk first, with every reason."""
        self.load()
        c = self.combined[self.combined["risk_score"] > 0]
        if fiscal_year is not None:
            c = c[c["fiscal_year"] == fiscal_year]
        if period_from is not None:
            c = c[c["period"] >= period_from]
        if period_to is not None:
            c = c[c["period"] <= period_to]
        if agreement is not None:
            c = c[c["agreement"] == agreement]
        matching = int(len(c))
        c = c.sort_values(
            ["risk_score", "model_score", "entry_id"], ascending=[False, False, True]
        ).head(limit)
        rows = self._attach_review(records(c, ENTRY_COLUMNS))
        return {
            "count": len(rows),
            "matching": matching,
            "filters": {"fiscal_year": fiscal_year, "period_from": period_from,
                        "period_to": period_to, "agreement": agreement},
            "entries": rows,
            "caveat": CAVEAT,
        }

    def explain_entry(self, entry_id: str) -> dict:
        """Everything known about one entry: lines, flags, both scores, notes, decisions.

        ``narrative`` is the latest version; ``narrative_history`` has every
        version with its id, so a decision's ``narrative_id`` can be read
        against the text it was actually made on.
        """
        self.load()
        try:
            _, flags, lines = entry_context(self.combined, self.flags, self.lines, entry_id)
        except KeyError:
            raise KeyError(f"no entry {entry_id!r} in the ledger") from None
        store = self._store()
        entry = records(self.combined[self.combined["entry_id"] == entry_id], ENTRY_COLUMNS)[0]
        return {
            "entry": entry,
            "lines": records(lines, LINE_COLUMNS),
            "flags": records(flags, FLAG_COLUMNS),
            "narrative": store.get_narrative(entry_id) if store else None,
            "narrative_history": store.narrative_versions(entry_id) if store else [],
            "decisions": records(store.history(entry_id), DECISION_COLUMNS) if store else [],
            "caveat": CAVEAT,
        }

    def search_entries(
        self,
        account_code: str | None = None,
        created_by: str | None = None,
        source: str | None = None,
        min_amount: float | None = None,
        fiscal_year: int | None = None,
        period: int | None = None,
        limit: int = 25,
    ) -> dict:
        """Entries matching simple filters, riskiest first. Unflagged entries are included."""
        self.load()
        c = self.combined
        if account_code:
            touched = set(self.lines.loc[self.lines["account_code"] == account_code, "entry_id"])
            c = c[c["entry_id"].isin(touched)]
        if created_by:
            c = c[c["created_by"] == created_by]
        if source:
            c = c[c["source"] == source]
        if min_amount is not None:
            c = c[c["entry_amount"] >= min_amount]
        if fiscal_year is not None:
            c = c[c["fiscal_year"] == fiscal_year]
        if period is not None:
            c = c[c["period"] == period]
        matching = int(len(c))
        c = c.sort_values(
            ["risk_score", "entry_amount", "entry_id"], ascending=[False, False, True]
        ).head(limit)
        rows = self._attach_review(records(c, ENTRY_COLUMNS))
        return {
            "count": len(rows),
            "matching": matching,
            "filters": {"account_code": account_code, "created_by": created_by, "source": source,
                        "min_amount": min_amount, "fiscal_year": fiscal_year, "period": period},
            "entries": rows,
            "caveat": CAVEAT,
        }

    def benford(self, by: str | None = None, min_n: int = 300) -> dict:
        """First-digit analysis over the population, or per segment of ``by``."""
        self.load()
        if by is None:
            result = _jsonable(benford_test(self.lines["abs_amount"], label="population"))
            result["scope"] = "population"
            result["caveat"] = BENFORD_CAVEAT
            return result
        if by not in self.lines.columns:
            raise KeyError(f"cannot segment by {by!r}; not a ledger column")
        segments = segmented_benford(self.lines, by=by, min_n=min_n)
        return {
            "scope": by,
            "min_n": min_n,
            "segments": records(segments),
            "caveat": BENFORD_CAVEAT,
        }

    def review_status(self) -> dict:
        """How far the human loop has got. Zeros, not an error, when nobody has started."""
        self.load()
        flagged = int((self.combined["risk_score"] > 0).sum())
        store = self._store()
        if store is None:
            return {
                "review_db": str(self.review_db) if self.review_db else None,
                "exists": False, "ledger_id": self.ledger_id, "flagged": flagged, "decided": 0,
                "outstanding": flagged, "by_decision": {}, "narratives": 0, "other_ledgers": {},
            }
        summary = store.summary()
        return {
            "review_db": str(self.review_db),
            "exists": True,
            "ledger_id": self.ledger_id,
            "flagged": flagged,
            "decided": len(store.decided_ids()),
            "outstanding": int(len(store.outstanding(self.combined))),
            "by_decision": {str(r.decision): int(r.entries) for r in summary.itertuples()},
            "narratives": len(store.narrative_ids()),
            # Rows this file holds for other ledgers, 'legacy' included: counted,
            # never shown as this ledger's.
            "other_ledgers": {
                str(r.ledger_id): {"narratives": int(r.narratives), "decisions": int(r.decisions)}
                for r in store.other_ledgers().itertuples()
            },
        }

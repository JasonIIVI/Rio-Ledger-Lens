"""The human loop: a durable record of what a reviewer decided, and why.

This is the part that makes the tool an audit tool rather than an analysis
script. A flag on its own is noise; a flag that somebody looked at, judged, and
signed their name to is evidence.

Three design choices worth stating:

1. **Decisions are append-only, and so is the advice behind them.** Changing
   your mind creates a new decision rather than overwriting the old one, and
   rewriting a narrative creates a new version rather than replacing the one a
   reviewer may already have read. Each decision records which narrative was
   on screen when it was made. An audit trail that can be silently edited is
   not an audit trail, and neither is one whose supporting text can be. The
   database enforces this itself: triggers refuse any UPDATE or DELETE on
   either table, and any INSERT that would land on an existing id (which is
   how ``REPLACE INTO`` overwrites a row without firing a delete trigger), so
   the rule does not depend on every caller going through this module. This
   is tamper-resistant, not tamper-evident: a writer who drops the triggers
   and puts them back leaves no trace, and a hash chain would be the next
   step if that ever matters.
2. **The model never writes here.** Narratives are advisory context attached to
   an entry; only a named human sets a decision.
3. **SQLite, not a CSV.** Concurrent reviewers, transactional writes, and
   queryable history - none of which a spreadsheet gives you.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path("data/review.sqlite")

#: The only decisions a reviewer may record.
#:
#: ``escalate`` is deliberately distinct from ``accept``: accepting means the
#: flag was valid and is now dealt with, escalating means it needs someone more
#: senior. Collapsing them would lose the distinction that matters most.
DECISIONS = ("accept", "dismiss", "escalate")

#: What :meth:`ReviewStore.review_state` reports for each entry, in this order.
REVIEW_STATE_COLUMNS = (
    "entry_id", "decision", "reviewer", "note", "decided_at", "narrative_id",
    "narrative_summary", "narrative_confidence", "narrative_superseded",
    "narrative_seen_by_reviewer",
)

#: On its own because the migration below has to create the same table.
NARRATIVES_TABLE = """
CREATE TABLE IF NOT EXISTS narratives (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id            TEXT NOT NULL,
    summary             TEXT,
    why_flagged         TEXT,
    evidence_to_request TEXT,
    suggested_control   TEXT,
    confidence          TEXT,
    model               TEXT,
    generated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_narratives_entry ON narratives(entry_id);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT    NOT NULL,
    decision     TEXT    NOT NULL CHECK (decision IN ('accept','dismiss','escalate')),
    reviewer     TEXT    NOT NULL,
    note         TEXT,
    risk_score   REAL,
    model_score  REAL,
    narrative_id INTEGER REFERENCES narratives(id),
    decided_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_entry ON decisions(entry_id);
CREATE INDEX IF NOT EXISTS ix_decisions_time  ON decisions(decided_at);
""" + NARRATIVES_TABLE + """
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS narratives_no_update BEFORE UPDATE ON narratives
BEGIN SELECT RAISE(ABORT, 'narratives are append-only'); END;
CREATE TRIGGER IF NOT EXISTS narratives_no_delete BEFORE DELETE ON narratives
BEGIN SELECT RAISE(ABORT, 'narratives are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_replace BEFORE INSERT ON decisions
WHEN EXISTS (SELECT 1 FROM decisions WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS narratives_no_replace BEFORE INSERT ON narratives
WHEN EXISTS (SELECT 1 FROM narratives WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'narratives are append-only'); END;
"""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _require_current_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse to read a file this version cannot read correctly.

    A reader never creates or migrates anything, so a file from before
    narratives were versioned, or an empty file, is an error with the fix in
    the message rather than a silent schema write.
    """
    if "id" not in _columns(conn, "narratives") or "narrative_id" not in _columns(conn, "decisions"):
        raise RuntimeError(
            f"{path} is not a current review database (no tables, or an older schema). "
            "Open it once with the dashboard or `ledgerlens narrate` to create or migrate it; "
            "a read-only connection will not."
        )


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database written by an earlier version up to this schema.

    Two steps, each a no-op on a current database. Narratives written before
    they were versioned had ``entry_id`` as their primary key; they are copied
    into the versioned table in generation order, so the ids follow the
    timeline. Decisions gain ``narrative_id``, left empty on rows recorded
    before it existed - the honest value, since nothing recorded what those
    reviewers saw. Only local, synthetic databases exist, which is why this is
    a rebuild in place rather than a migration framework.
    """
    narrative_columns = _columns(conn, "narratives")
    if narrative_columns and "id" not in narrative_columns:
        conn.executescript(
            "BEGIN;\n"
            "ALTER TABLE narratives RENAME TO narratives_v1;\n"
            + NARRATIVES_TABLE +
            "INSERT INTO narratives (entry_id, summary, why_flagged, evidence_to_request, "
            "suggested_control, confidence, model, generated_at) "
            "SELECT entry_id, summary, why_flagged, evidence_to_request, suggested_control, "
            "confidence, model, generated_at FROM narratives_v1 ORDER BY generated_at, entry_id;\n"
            "DROP TABLE narratives_v1;\n"
            "COMMIT;"
        )
    decision_columns = _columns(conn, "decisions")
    if decision_columns and "narrative_id" not in decision_columns:
        conn.execute(
            "ALTER TABLE decisions ADD COLUMN narrative_id INTEGER REFERENCES narratives(id)"
        )
        conn.commit()


@dataclass
class Decision:
    entry_id: str
    decision: str
    reviewer: str
    note: str = ""
    risk_score: float | None = None
    model_score: float | None = None
    #: The narrative on screen when the reviewer decided, so the workpaper can
    #: show the text they actually read rather than whatever is latest now.
    narrative_id: int | None = None


def _narrative_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["evidence_to_request"] = [
        line for line in (data.get("evidence_to_request") or "").split("\n") if line
    ]
    return data


class ReviewStore:
    """Append-only store of reviewer decisions and generated narratives."""

    def __init__(self, path: str | Path = DEFAULT_DB, *, _read_only: bool = False) -> None:
        self.path = Path(path)
        self.is_read_only = _read_only
        if _read_only:
            # No mkdir and no DDL: a reader never creates, migrates or touches the file.
            if not self.path.exists():
                raise FileNotFoundError(f"no review database at {self.path}")
            with closing(self._connect()) as conn:
                _require_current_schema(conn, self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            _migrate(conn)
            conn.executescript(SCHEMA)
            conn.commit()

    @classmethod
    def read_only(cls, path: str | Path) -> ReviewStore:
        """Open an existing database for reading only.

        SQLite itself refuses every write on this connection (URI ``mode=ro``)
        and no schema statement runs, so the file is never created, migrated
        or altered by a reader. The MCP server opens the store this way:
        read-only is then a property of the connection, not of which methods
        the tools happen to call.
        """
        return cls(path, _read_only=True)

    def _connect(self) -> sqlite3.Connection:
        if self.is_read_only:
            # as_uri() escapes the path (spaces, '~') for SQLite's URI parser.
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    # --- decisions --------------------------------------------------------

    def record(self, decision: Decision) -> int:
        """Append a decision. Returns its row id."""
        if decision.decision not in DECISIONS:
            raise ValueError(
                "decision must be one of {}, got {!r}".format(
                    ", ".join(DECISIONS), decision.decision)
            )
        if not decision.reviewer.strip():
            raise ValueError("a decision must be attributable to a named reviewer")
        if not str(decision.entry_id).strip():
            raise ValueError("a decision must name an entry")

        with closing(self._connect()) as conn:
            if decision.narrative_id is not None:
                # The recorded narrative must be one written for this entry; a
                # decision that pointed at another entry's note would be worse
                # than one that recorded nothing.
                seen = conn.execute(
                    "SELECT entry_id FROM narratives WHERE id = ?", (decision.narrative_id,)
                ).fetchone()
                if seen is None or seen["entry_id"] != decision.entry_id:
                    raise ValueError(
                        f"narrative {decision.narrative_id} is not a narrative for "
                        f"entry {decision.entry_id}"
                    )
            cur = conn.execute(
                "INSERT INTO decisions "
                "(entry_id, decision, reviewer, note, risk_score, model_score, narrative_id, "
                " decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.entry_id, decision.decision, decision.reviewer.strip(),
                    decision.note, decision.risk_score, decision.model_score,
                    decision.narrative_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def history(self, entry_id: str) -> pd.DataFrame:
        """Every decision ever recorded against one entry, oldest first."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT * FROM decisions WHERE entry_id = ? ORDER BY id",
                conn, params=(entry_id,),
            )

    def current(self) -> pd.DataFrame:
        """The latest decision per entry - what the queue state is *now*."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT d.* FROM decisions d "
                "JOIN (SELECT entry_id, MAX(id) AS id FROM decisions GROUP BY entry_id) last "
                "  ON d.id = last.id "
                "ORDER BY d.decided_at DESC",
                conn,
            )

    def decided_ids(self) -> set:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT DISTINCT entry_id FROM decisions").fetchall()
        return {r["entry_id"] for r in rows}

    def summary(self) -> pd.DataFrame:
        """Counts by current decision, for a progress readout."""
        current = self.current()
        if current.empty:
            return pd.DataFrame(columns=["decision", "entries"])
        out = current.groupby("decision").size().reset_index(name="entries")
        return out.sort_values("entries", ascending=False).reset_index(drop=True)

    def outstanding(self, scored: pd.DataFrame) -> pd.DataFrame:
        """Flagged entries with no decision yet - the actual work queue."""
        done = self.decided_ids()
        flagged = scored[scored["risk_score"] > 0]
        return flagged[~flagged["entry_id"].isin(done)]

    # --- narratives -------------------------------------------------------

    def save_narrative(self, entry_id: str, narrative: dict, model: str = "") -> int:
        """Store a narrative as a new version and return its id.

        Never replaces. A reviewer may already have read the previous version
        and decided against it, and that decision has to keep pointing at the
        text they saw. A rewrite costs a few kilobytes; the advice trail is
        then as tamper-resistant as the decision trail.
        """
        with closing(self._connect()) as conn:
            cur = conn.execute(
                "INSERT INTO narratives "
                "(entry_id, summary, why_flagged, evidence_to_request, suggested_control, "
                " confidence, model, generated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    narrative.get("summary", ""),
                    narrative.get("why_flagged", ""),
                    "\n".join(narrative.get("evidence_to_request", []) or []),
                    narrative.get("suggested_control", ""),
                    narrative.get("confidence", ""),
                    model,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_narrative(self, entry_id: str) -> dict | None:
        """The latest narrative written for an entry, with its ``id``, or None."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM narratives WHERE entry_id = ? ORDER BY id DESC LIMIT 1",
                (entry_id,),
            ).fetchone()
        return None if row is None else _narrative_dict(row)

    def narrative_by_id(self, narrative_id: int) -> dict | None:
        """One specific version - the one a decision recorded, typically."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM narratives WHERE id = ?", (int(narrative_id),)
            ).fetchone()
        return None if row is None else _narrative_dict(row)

    def narrative_history(self, entry_id: str) -> pd.DataFrame:
        """Every version written for one entry, oldest first."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT * FROM narratives WHERE entry_id = ? ORDER BY id",
                conn, params=(entry_id,),
            )

    def narrative_versions(self, entry_id: str) -> list[dict]:
        """The same history as dicts, shaped like :meth:`get_narrative` (evidence as a list)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM narratives WHERE entry_id = ? ORDER BY id", (entry_id,)
            ).fetchall()
        return [_narrative_dict(row) for row in rows]

    def narratives_frame(self, latest_only: bool = True) -> pd.DataFrame:
        """Cached narratives for the workpaper and the MCP server.

        The latest version per entry by default; every version when a caller
        has to resolve the ids that decisions recorded.
        """
        with closing(self._connect()) as conn:
            if latest_only:
                return pd.read_sql_query(
                    "SELECT n.* FROM narratives n "
                    "JOIN (SELECT entry_id, MAX(id) AS id FROM narratives GROUP BY entry_id) "
                    "  last ON n.id = last.id "
                    "ORDER BY n.entry_id",
                    conn,
                )
            return pd.read_sql_query("SELECT * FROM narratives ORDER BY entry_id, id", conn)

    def narrative_ids(self) -> set:
        """Entries that have at least one narrative."""
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT DISTINCT entry_id FROM narratives").fetchall()
        return {r["entry_id"] for r in rows}

    # --- both together ----------------------------------------------------

    def review_state(self) -> pd.DataFrame:
        """One row per entry with a decision or a narrative: what to show beside it.

        The workpaper and the MCP server both read this, so they cannot
        disagree about which note goes with which decision. Columns are
        :data:`REVIEW_STATE_COLUMNS`.

        The narrative shown is the version the decision recorded, because that
        is the text the decision was based on. An entry with no decision, or
        one that recorded no note, shows the latest version.
        ``narrative_superseded`` says when a newer version exists than the one
        shown. ``narrative_seen_by_reviewer`` says whether the note shown is
        the one the reviewer read: "yes" when the decision recorded it, "no"
        when the decision recorded nothing and the note was written afterwards,
        "unknown" when the decision recorded nothing but the note already
        existed (any decision recorded without an id; those from before notes
        were versioned are the common case), and "" when there is no decision
        or no note. A later note is never passed off as the basis of an
        earlier decision.
        """
        decisions = self.current()[
            ["entry_id", "decision", "reviewer", "note", "narrative_id", "decided_at"]
        ]
        latest = self.narratives_frame().set_index("entry_id")["id"]
        versions = self.narratives_frame(latest_only=False).set_index("id")

        # dtype=object: an empty id list would otherwise be float64 and refuse to merge.
        ids = sorted(set(decisions["entry_id"]) | set(latest.index))
        entries = pd.DataFrame({"entry_id": pd.Series(ids, dtype=object)})
        out = entries.merge(decisions, on="entry_id", how="left")
        newest = pd.to_numeric(out["entry_id"].map(latest)).astype("Int64")
        recorded = pd.to_numeric(out["narrative_id"]).astype("Int64")
        shown = recorded.fillna(newest)
        out["narrative_id"] = shown
        out["narrative_summary"] = shown.map(versions["summary"])
        out["narrative_confidence"] = shown.map(versions["confidence"])
        out["narrative_superseded"] = (
            (shown.notna() & (shown != newest)).fillna(False).astype(bool)
        )
        decided = out["decision"].notna()
        # ISO-8601 UTC strings compare correctly as text; a note written in the
        # same second as the decision is "unknown", the honest reading.
        written_after = (
            shown.map(versions["generated_at"]).fillna("") > out["decided_at"].fillna("")
        )
        seen = pd.Series("", index=out.index, dtype=object)
        seen[decided & recorded.notna()] = "yes"
        seen[decided & recorded.isna() & shown.notna() & written_after] = "no"
        seen[decided & recorded.isna() & shown.notna() & ~written_after] = "unknown"
        out["narrative_seen_by_reviewer"] = seen
        return out[list(REVIEW_STATE_COLUMNS)]

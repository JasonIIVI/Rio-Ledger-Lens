"""The human loop: a durable record of what a reviewer decided, and why.

This is the part that makes the tool an audit tool rather than an analysis
script. A flag on its own is noise; a flag that somebody looked at, judged, and
signed their name to is evidence.

Three design choices worth stating:

1. **Decisions are append-only.** Changing your mind creates a new decision
   rather than overwriting the old one. An audit trail that can be silently
   edited is not an audit trail.
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT    NOT NULL,
    decision     TEXT    NOT NULL CHECK (decision IN ('accept','dismiss','escalate')),
    reviewer     TEXT    NOT NULL,
    note         TEXT,
    risk_score   REAL,
    model_score  REAL,
    decided_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_entry ON decisions(entry_id);
CREATE INDEX IF NOT EXISTS ix_decisions_time  ON decisions(decided_at);

CREATE TABLE IF NOT EXISTS narratives (
    entry_id            TEXT PRIMARY KEY,
    summary             TEXT,
    why_flagged         TEXT,
    evidence_to_request TEXT,
    suggested_control   TEXT,
    confidence          TEXT,
    model               TEXT,
    generated_at        TEXT NOT NULL
);
"""


@dataclass
class Decision:
    entry_id: str
    decision: str
    reviewer: str
    note: str = ""
    risk_score: float | None = None
    model_score: float | None = None


class ReviewStore:
    """Append-only store of reviewer decisions and generated narratives."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
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
            cur = conn.execute(
                "INSERT INTO decisions "
                "(entry_id, decision, reviewer, note, risk_score, model_score, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.entry_id, decision.decision, decision.reviewer.strip(),
                    decision.note, decision.risk_score, decision.model_score,
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

    def save_narrative(self, entry_id: str, narrative: dict, model: str = "") -> None:
        """Cache a generated narrative so it is not paid for twice."""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO narratives "
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

    def get_narrative(self, entry_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM narratives WHERE entry_id = ?", (entry_id,)
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["evidence_to_request"] = [
            line for line in (data.get("evidence_to_request") or "").split("\n") if line
        ]
        return data

    def narratives_frame(self) -> pd.DataFrame:
        """Every cached narrative, one row per entry, for the workpaper and the MCP server."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query("SELECT * FROM narratives ORDER BY entry_id", conn)

    def narrative_ids(self) -> set:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT entry_id FROM narratives").fetchall()
        return {r["entry_id"] for r in rows}

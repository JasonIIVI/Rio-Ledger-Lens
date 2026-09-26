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
   the rule does not depend on every caller going through this module. Every
   read-write open puts back a guard the file has lost, and a read-only open
   refuses a file that is missing one. This is tamper-resistant, not
   tamper-evident: a writer who drops the triggers and puts them back leaves
   no trace, and a hash chain would be the next step if that ever matters.
2. **The model never writes here.** Narratives are advisory context attached to
   an entry; only a named human sets a decision.
3. **SQLite, not a CSV.** Concurrent reviewers, transactional writes, and
   queryable history - none of which a spreadsheet gives you.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
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

#: Rows written before review data was keyed by ledger (schema < 4) sit under
#: this id after migration. A store is never bound to it; adopt_legacy()
#: copies such rows into the ledger they were written for.
LEGACY_LEDGER_ID = "legacy"

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
    generated_at        TEXT NOT NULL,
    ledger_id           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_narratives_ledger_entry ON narratives(ledger_id, entry_id);
"""

#: The schema this code writes, stamped in ``PRAGMA user_version``. Each step
#: in :data:`_MIGRATIONS` moves a file up by one. 1 = v0.3.0 (narratives keyed
#: by entry), 2 = versioned narratives and narrative_id on decisions, 3 = the
#: append-only triggers including the REPLACE guards, 4 = ledger_id on both
#: tables (rows from before it sit under :data:`LEGACY_LEDGER_ID`).
SCHEMA_VERSION = 4

TABLES = """
CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT    NOT NULL,
    decision     TEXT    NOT NULL CHECK (decision IN ('accept','dismiss','escalate')),
    reviewer     TEXT    NOT NULL,
    note         TEXT,
    risk_score   REAL,
    model_score  REAL,
    narrative_id INTEGER REFERENCES narratives(id),
    decided_at   TEXT    NOT NULL,
    ledger_id    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_time         ON decisions(decided_at);
CREATE INDEX IF NOT EXISTS ix_decisions_ledger_entry ON decisions(ledger_id, entry_id);
""" + NARRATIVES_TABLE

TRIGGERS = """
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

SCHEMA = TABLES + TRIGGERS

#: The append-only guards a current file must carry. A read-write open puts
#: back any that went missing; a read-only open refuses a file without them,
#: since what it holds can no longer be read as a record.
GUARDS = (
    "decisions_no_update", "decisions_no_delete", "decisions_no_replace",
    "narratives_no_update", "narratives_no_delete", "narratives_no_replace",
)

# A migration step creates the shape of the version it moves a file *to*,
# from text frozen when that version shipped - never from the live constants
# above. Otherwise a v1 file would pick up a later version's columns at step
# 1 -> 2, and the step that adds them would fail on "duplicate column".
_NARRATIVES_TABLE_V2 = """
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

_TRIGGERS_V3 = """
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


def _triggers(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def _schema_version(conn: sqlite3.Connection, path: Path) -> tuple[int, bool]:
    """The file's schema version, and whether the file says so itself.

    Files written before the stamp existed are recognised by their shape.
    0 means a fresh file with no tables. A file that is not SQLite, or that
    carries a stamp but none of this module's tables (another program's
    database, pointed at by mistake), is refused rather than guessed at.
    """
    try:
        stamped = int(conn.execute("PRAGMA user_version").fetchone()[0])
        decisions = _columns(conn, "decisions")
        narratives = _columns(conn, "narratives")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"{path} is not a SQLite database ({exc})") from exc
    if stamped:
        if stamped < 0 or not decisions:
            raise RuntimeError(
                f"{path} is not a LedgerLens review database (user_version {stamped}"
                + ("" if decisions else ", no decisions table") + ")"
            )
        return stamped, True
    if not decisions:
        if narratives:
            raise RuntimeError(
                f"{path} holds a narratives table but no decisions table, a shape no LedgerLens "
                "version writes; restore it from a backup rather than opening it here"
            )
        return 0, False
    if "id" not in narratives or "narrative_id" not in decisions:
        return 1, False
    if "decisions_no_replace" not in _triggers(conn):
        return 2, False
    if "ledger_id" not in decisions:
        return 3, False
    # The newest shape this ladder recognises, as a literal: returning
    # SCHEMA_VERSION here would skip every later step the day the constant
    # moves. (Stamping began at 3, so an unstamped file is normally 3 or
    # older; a v4 file with its stamp cleared walks nothing it already has.)
    return 4, False


def _refuse_newer(version: int, path: Path, verb: str) -> None:
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"{path} was written by a newer LedgerLens (schema {version}; this version {verb} "
            f"{SCHEMA_VERSION}). Upgrade LedgerLens to open it."
        )


def _require_current_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse to read a file this version cannot read correctly.

    A reader never creates or migrates anything, so a file from an earlier
    version, or an empty file, is an error with the fix in the message rather
    than a silent schema write; a file from a later version is refused too,
    since this code does not know what it holds; and so is a current file
    that has lost an append-only guard, since its history may have been
    edited and only a read-write open can put the guard back.
    """
    version, _ = _schema_version(conn, path)
    _refuse_newer(version, path, "reads")
    if version < SCHEMA_VERSION:
        raise RuntimeError(
            f"{path} is not a current review database (no tables, or an older schema). "
            "Open it once with the dashboard or `ledgerlens narrate` to create or migrate it; "
            "a read-only connection will not."
        )
    missing = sorted(set(GUARDS) - _triggers(conn))
    if missing:
        raise RuntimeError(
            f"{path} is missing its append-only guard(s) {', '.join(missing)}, so its history "
            "may have been edited. Open it once with the dashboard or `ledgerlens narrate` to "
            "put the guards back; a read-only connection will not."
        )


def _v1_to_v2(conn: sqlite3.Connection) -> str:
    """Version the narratives, and let a decision record the note it was made on.

    Narratives keyed by ``entry_id`` are copied into the versioned table in
    generation order, so the ids follow the timeline. Decisions gain
    ``narrative_id``, left empty on rows recorded before it existed - the
    honest value, since nothing recorded what those reviewers saw.
    """
    script = ""
    narrative_columns = _columns(conn, "narratives")
    if narrative_columns and "id" not in narrative_columns:
        script += (
            "ALTER TABLE narratives RENAME TO narratives_v1;\n"
            + _NARRATIVES_TABLE_V2 +
            "INSERT INTO narratives (entry_id, summary, why_flagged, evidence_to_request, "
            "suggested_control, confidence, model, generated_at) "
            "SELECT entry_id, summary, why_flagged, evidence_to_request, suggested_control, "
            "confidence, model, generated_at FROM narratives_v1 ORDER BY generated_at, entry_id;\n"
            "DROP TABLE narratives_v1;\n"
        )
    elif not narrative_columns:
        script += _NARRATIVES_TABLE_V2
    if "narrative_id" not in _columns(conn, "decisions"):
        script += "ALTER TABLE decisions ADD COLUMN narrative_id INTEGER REFERENCES narratives(id);\n"
    return script


def _v2_to_v3(conn: sqlite3.Connection) -> str:
    """Enforce append-only in the database: the update, delete and REPLACE guards."""
    return _TRIGGERS_V3


def _v3_to_v4(conn: sqlite3.Connection) -> str:
    """Key both tables by ledger.

    ADD COLUMN is the only route: the update triggers refuse an UPDATE, and a
    rebuild would renumber narratives that decisions point at. So a migrated
    file carries DEFAULT 'legacy' where a fresh file has no default; the
    default is what files the old rows, and nothing this version writes
    relies on it (a store always names its ledger). The single-column
    indexes give way to the composite ones a fresh file has.
    """
    script = ""
    for table in ("decisions", "narratives"):
        if "ledger_id" not in _columns(conn, table):
            script += (f"ALTER TABLE {table} ADD COLUMN ledger_id TEXT NOT NULL "
                       f"DEFAULT '{LEGACY_LEDGER_ID}';\n")
    return script + (
        "DROP INDEX IF EXISTS ix_decisions_entry;\n"
        "DROP INDEX IF EXISTS ix_narratives_entry;\n"
        "CREATE INDEX IF NOT EXISTS ix_decisions_ledger_entry ON decisions(ledger_id, entry_id);\n"
        "CREATE INDEX IF NOT EXISTS ix_narratives_ledger_entry ON narratives(ledger_id, entry_id);\n"
    )


#: Each step takes a file from version n to n + 1, as one transaction that
#: also writes the new stamp, so a crash mid-way leaves the file where it was.
_MIGRATIONS = {1: _v1_to_v2, 2: _v2_to_v3, 3: _v3_to_v4}


def _migrate(conn: sqlite3.Connection, path: Path) -> None:
    """Bring a file up to :data:`SCHEMA_VERSION`, or refuse one from a later version.

    A fresh file gets the whole schema and the stamp; an unstamped file at the
    current shape gets the same script, which creates only what is missing.
    Anything older walks the steps one at a time. ``executescript`` commits a
    pending transaction before it runs, which is why each step carries its own
    BEGIN and COMMIT. Every open then re-runs the schema's IF NOT EXISTS
    statements: a guard or index the file has lost comes back, and a whole
    file is left untouched.
    """
    version, stamped = _schema_version(conn, path)
    _refuse_newer(version, path, "writes")
    if version in (0, SCHEMA_VERSION) and not stamped:
        conn.executescript(f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
        version = SCHEMA_VERSION
    while version < SCHEMA_VERSION:
        step = _MIGRATIONS[version](conn)
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{step}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
            )
        except sqlite3.OperationalError:
            # Two writers can open an old file at once (two dashboards, say),
            # and the second one's script was built before the first committed.
            # Re-read what the file is now and carry on from there, or fail.
            conn.rollback()
            now, _ = _schema_version(conn, path)
            if now <= version:
                raise
            version = now
            continue
        version += 1
    conn.executescript(SCHEMA)


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


def _check_ledger_id(ledger_id) -> str:
    """The identity a store is bound to: ``csv:<digest>`` or ``qbo:<realm>``, never blank."""
    if not isinstance(ledger_id, str) or not ledger_id or any(c.isspace() for c in ledger_id):
        raise ValueError(
            f"a store is bound to one ledger by its identity string, got {ledger_id!r} "
            "(see ingest.ledger_identity)"
        )
    if ledger_id == LEGACY_LEDGER_ID:
        raise ValueError(
            "a store is never bound to 'legacy': those are rows from before review data was "
            "keyed by ledger; adopt_legacy() files them under the ledger they were written for"
        )
    return ledger_id


class ReviewStore:
    """Append-only store of reviewer decisions and generated narratives, for one ledger.

    Every read and write is scoped to the ledger the store was bound to at
    open (``ledger_id``): two ledgers can share entry ids (the generator's
    are ``JE-<year>-<seq>`` whatever the seed), and a note or decision from
    one must never show beside the other's entry. :meth:`ledgers` is the one
    view across ledgers.
    """

    def __init__(self, path: str | Path, ledger_id: str, *, _read_only: bool = False) -> None:
        self.path = Path(path)
        self.ledger_id = _check_ledger_id(ledger_id)
        self.is_read_only = _read_only
        if _read_only:
            # No mkdir and no DDL: a reader never creates, migrates or touches the file.
            if not self.path.exists():
                raise FileNotFoundError(f"no review database at {self.path}")
            with closing(self._open()) as conn:
                _require_current_schema(conn, self.path)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as conn:
            _migrate(conn, self.path)

    def _open(self) -> sqlite3.Connection:
        """The first connection: a path that is no database file gets a message, not a traceback."""
        try:
            return self._connect()
        except sqlite3.Error as exc:
            raise RuntimeError(f"{self.path} cannot be opened as a SQLite database ({exc})") from exc

    @classmethod
    def read_only(cls, path: str | Path, ledger_id: str) -> ReviewStore:
        """Open an existing database for reading only, bound to one ledger.

        SQLite itself refuses every write on this connection (URI ``mode=ro``)
        and no schema statement runs, so the file is never created, migrated
        or altered by a reader. The MCP server opens the store this way:
        read-only is then a property of the connection, not of which methods
        the tools happen to call.
        """
        return cls(path, ledger_id, _read_only=True)

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
                # The recorded narrative must be one written for this entry in
                # this ledger; a decision that pointed at another entry's note,
                # or another ledger's, would be worse than one that recorded
                # nothing.
                seen = conn.execute(
                    "SELECT entry_id, ledger_id FROM narratives WHERE id = ?",
                    (decision.narrative_id,),
                ).fetchone()
                if seen is None or (seen["entry_id"], seen["ledger_id"]) != (
                        decision.entry_id, self.ledger_id):
                    raise ValueError(
                        f"narrative {decision.narrative_id} is not a narrative for "
                        f"entry {decision.entry_id} in ledger {self.ledger_id}"
                    )
            cur = conn.execute(
                "INSERT INTO decisions "
                "(entry_id, decision, reviewer, note, risk_score, model_score, narrative_id, "
                " decided_at, ledger_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision.entry_id, decision.decision, decision.reviewer.strip(),
                    decision.note, decision.risk_score, decision.model_score,
                    decision.narrative_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    self.ledger_id,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def history(self, entry_id: str) -> pd.DataFrame:
        """Every decision ever recorded against one entry, oldest first."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT * FROM decisions WHERE ledger_id = ? AND entry_id = ? ORDER BY id",
                conn, params=(self.ledger_id, entry_id),
            )

    def current(self) -> pd.DataFrame:
        """The latest decision per entry - what the queue state is *now*."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT d.* FROM decisions d "
                "JOIN (SELECT entry_id, MAX(id) AS id FROM decisions WHERE ledger_id = ? "
                "      GROUP BY entry_id) last "
                "  ON d.id = last.id "
                "ORDER BY d.decided_at DESC",
                conn, params=(self.ledger_id,),
            )

    def decided_ids(self) -> set:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT entry_id FROM decisions WHERE ledger_id = ?", (self.ledger_id,)
            ).fetchall()
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
                " confidence, model, generated_at, ledger_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id,
                    narrative.get("summary", ""),
                    narrative.get("why_flagged", ""),
                    "\n".join(narrative.get("evidence_to_request", []) or []),
                    narrative.get("suggested_control", ""),
                    narrative.get("confidence", ""),
                    model,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    self.ledger_id,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_narrative(self, entry_id: str) -> dict | None:
        """The latest narrative written for an entry, with its ``id``, or None."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM narratives WHERE ledger_id = ? AND entry_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (self.ledger_id, entry_id),
            ).fetchone()
        return None if row is None else _narrative_dict(row)

    def narrative_by_id(self, narrative_id: int) -> dict | None:
        """One specific version - the one a decision recorded, typically.

        By id, across ledgers: the dict says which ledger it belongs to."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM narratives WHERE id = ?", (int(narrative_id),)
            ).fetchone()
        return None if row is None else _narrative_dict(row)

    def narrative_history(self, entry_id: str) -> pd.DataFrame:
        """Every version written for one entry, oldest first."""
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT * FROM narratives WHERE ledger_id = ? AND entry_id = ? ORDER BY id",
                conn, params=(self.ledger_id, entry_id),
            )

    def narrative_versions(self, entry_id: str) -> list[dict]:
        """The same history as dicts, shaped like :meth:`get_narrative` (evidence as a list)."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM narratives WHERE ledger_id = ? AND entry_id = ? ORDER BY id",
                (self.ledger_id, entry_id),
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
                    "JOIN (SELECT entry_id, MAX(id) AS id FROM narratives WHERE ledger_id = ? "
                    "      GROUP BY entry_id) last ON n.id = last.id "
                    "ORDER BY n.entry_id",
                    conn, params=(self.ledger_id,),
                )
            return pd.read_sql_query(
                "SELECT * FROM narratives WHERE ledger_id = ? ORDER BY entry_id, id",
                conn, params=(self.ledger_id,),
            )

    def narrative_ids(self) -> set:
        """Entries that have at least one narrative."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT entry_id FROM narratives WHERE ledger_id = ?", (self.ledger_id,)
            ).fetchall()
        return {r["entry_id"] for r in rows}

    # --- across ledgers ---------------------------------------------------

    def ledgers(self) -> pd.DataFrame:
        """Every ledger with rows in this file, the bound one included.

        Columns ``ledger_id, narratives, decisions``: entries with at least
        one narrative or decision, which is how :meth:`narrative_ids` and
        :meth:`decided_ids` count. The one view across ledgers; nothing else
        in this class reads another ledger's rows.
        """
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT ledger_id, "
                "       COUNT(DISTINCT CASE WHEN kind = 'n' THEN entry_id END) AS narratives, "
                "       COUNT(DISTINCT CASE WHEN kind = 'd' THEN entry_id END) AS decisions "
                "FROM (SELECT ledger_id, entry_id, 'n' AS kind FROM narratives "
                "      UNION ALL "
                "      SELECT ledger_id, entry_id, 'd' FROM decisions) "
                "GROUP BY ledger_id ORDER BY ledger_id",
                conn,
            )

    def other_ledgers(self) -> pd.DataFrame:
        """:meth:`ledgers` without the bound ledger: what this file holds that is not shown."""
        ledgers = self.ledgers()
        return ledgers[ledgers["ledger_id"] != self.ledger_id].reset_index(drop=True)

    def adopt_legacy(self, entry_ids: Iterable[str] | None = None) -> dict:
        """Copy every 'legacy' row into the bound ledger, as new rows.

        Rows from before schema 4 do not say which ledger they were written
        for; the person running this does. The originals stay (append-only:
        the copies are the record of the adoption). Narratives are copied
        first, so each copied decision's ``narrative_id`` points at the copy
        of the note it recorded; ``generated_at``, ``model``, ``reviewer`` and
        ``decided_at`` are kept as written. Refused when the bound ledger
        already holds rows, and when ``entry_ids`` is given and a legacy row
        names an entry outside it, the surest sign the rows belong to another
        ledger. Returns the counts and the old-to-new narrative id map.
        """
        known = None if entry_ids is None else set(entry_ids)
        with closing(self._connect()) as conn:
            # The check and the copy see one file; a second adopter waits, then is refused.
            conn.execute("BEGIN IMMEDIATE")
            try:
                mine = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM narratives WHERE ledger_id = ?) "
                    "     + (SELECT COUNT(*) FROM decisions WHERE ledger_id = ?)",
                    (self.ledger_id, self.ledger_id),
                ).fetchone()[0]
                if mine:
                    raise ValueError(
                        f"ledger {self.ledger_id} already holds {mine} row(s); legacy rows are "
                        "adopted only into a ledger with none"
                    )
                notes = conn.execute(
                    "SELECT * FROM narratives WHERE ledger_id = ? ORDER BY id", (LEGACY_LEDGER_ID,)
                ).fetchall()
                decisions = conn.execute(
                    "SELECT * FROM decisions WHERE ledger_id = ? ORDER BY id", (LEGACY_LEDGER_ID,)
                ).fetchall()
                if known is not None:
                    strays = sorted({r["entry_id"] for r in notes + decisions} - known)
                    if strays:
                        raise ValueError(
                            f"{len(strays)} legacy row(s) name entries not in this ledger (e.g. "
                            f"{strays[0]}); they were written for a different ledger"
                        )
                remap: dict[int, int] = {}
                for r in notes:
                    cur = conn.execute(
                        "INSERT INTO narratives (ledger_id, entry_id, summary, why_flagged, "
                        " evidence_to_request, suggested_control, confidence, model, generated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.ledger_id, r["entry_id"], r["summary"], r["why_flagged"],
                         r["evidence_to_request"], r["suggested_control"], r["confidence"],
                         r["model"], r["generated_at"]),
                    )
                    remap[r["id"]] = int(cur.lastrowid)
                for r in decisions:
                    old = r["narrative_id"]
                    if old is not None and old not in remap:
                        raise RuntimeError(
                            f"legacy decision {r['id']} records narrative {old}, which is not a "
                            "legacy narrative"
                        )
                    conn.execute(
                        "INSERT INTO decisions (ledger_id, entry_id, decision, reviewer, note, "
                        " risk_score, model_score, narrative_id, decided_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.ledger_id, r["entry_id"], r["decision"], r["reviewer"], r["note"],
                         r["risk_score"], r["model_score"], None if old is None else remap[old],
                         r["decided_at"]),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"ledger_id": self.ledger_id, "narratives": len(notes),
                "decisions": len(decisions), "narrative_ids": remap}

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

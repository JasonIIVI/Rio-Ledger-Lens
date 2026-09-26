import sqlite3
from contextlib import closing

import pandas as pd
import pytest

from ledgerlens import jets, review
from ledgerlens.review import (
    DECISIONS,
    REVIEW_STATE_COLUMNS,
    SCHEMA_VERSION,
    Decision,
    ReviewStore,
)


def schema_version(path):
    return sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]


#: Identities for these tests. One file can hold rows for both, and a store bound to
#: one must never show the other's.
LEDGER = "csv:" + "a" * 64
OTHER = "csv:" + "b" * 64


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "review.sqlite", LEDGER)


def narrative(summary="s"):
    return {"summary": summary, "why_flagged": "w", "evidence_to_request": ["a", "b"],
            "suggested_control": "c", "confidence": "low"}


def test_store_creates_its_schema_on_a_fresh_path(tmp_path):
    path = tmp_path / "nested" / "review.sqlite"
    ReviewStore(path, LEDGER)
    assert path.exists()
    assert schema_version(path) == SCHEMA_VERSION == 4
    with closing(sqlite3.connect(str(path))) as raw:
        for table in ("decisions", "narratives"):
            info = {r[1]: r for r in raw.execute(f"PRAGMA table_info({table})")}
            assert info["ledger_id"][3] == 1 and info["ledger_id"][4] is None  # NOT NULL, no default
        names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_decisions_ledger_entry", "ix_narratives_ledger_entry", "ix_decisions_time"} <= names
    assert not {"ix_decisions_entry", "ix_narratives_entry"} & names


def test_history_is_append_only(store):
    first = store.record(Decision("JE-1", "dismiss", "ana", "routine"))
    second = store.record(Decision("JE-1", "escalate", "ana", "changed my mind"))

    assert second > first
    history = store.history("JE-1")
    assert history["decision"].tolist() == ["dismiss", "escalate"]
    assert history["note"].tolist() == ["routine", "changed my mind"]


def test_current_is_the_latest_decision_per_entry(store):
    store.record(Decision("JE-1", "dismiss", "ana"))
    store.record(Decision("JE-2", "accept", "ben"))
    store.record(Decision("JE-1", "escalate", "ana"))

    current = store.current().set_index("entry_id")["decision"]
    assert current.to_dict() == {"JE-1": "escalate", "JE-2": "accept"}


@pytest.mark.parametrize("decision", [
    Decision("JE-1", "approve", "ana"),   # not a documented decision
    Decision("JE-1", "accept", "   "),    # nobody signed it
    Decision("  ", "accept", "ana"),      # no entry
])
def test_record_rejects_bad_decisions(store, decision):
    with pytest.raises(ValueError):
        store.record(decision)


def test_summary_counts_current_decisions_only(store):
    assert store.summary().empty
    store.record(Decision("JE-1", "dismiss", "ana"))
    store.record(Decision("JE-2", "dismiss", "ana"))
    store.record(Decision("JE-3", "accept", "ana"))
    store.record(Decision("JE-3", "escalate", "ana"))  # supersedes the accept

    summary = store.summary().set_index("decision")["entries"]
    assert summary.to_dict() == {"dismiss": 2, "escalate": 1}


def test_outstanding_is_the_flagged_entries_without_a_decision(store, small_ledger):
    ledger, _ = small_ledger
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    flagged = scored[scored["risk_score"] > 0]["entry_id"].tolist()
    assert len(flagged) > 1

    store.record(Decision(flagged[0], "accept", "ana"))

    outstanding = set(store.outstanding(scored)["entry_id"])
    assert outstanding == set(flagged[1:])
    assert store.decided_ids() == {flagged[0]}


def test_narrative_round_trip_keeps_the_evidence_list(store):
    first = store.save_narrative("JE-1", narrative(), model="claude-test")

    assert store.get_narrative("JE-9") is None
    back = store.get_narrative("JE-1")
    assert back["id"] == first
    assert back["evidence_to_request"] == ["a", "b"]
    assert back["model"] == "claude-test"
    assert back["generated_at"]
    assert store.narrative_ids() == {"JE-1"}


def test_rewriting_a_narrative_keeps_every_version(store):
    first = store.save_narrative("JE-1", narrative("original"))
    second = store.save_narrative("JE-1", narrative("updated"))
    assert second > first

    # The latest is what a reader sees by default...
    assert store.get_narrative("JE-1")["summary"] == "updated"
    assert store.narratives_frame()["summary"].tolist() == ["updated"]
    assert store.narrative_ids() == {"JE-1"}
    # ...and the earlier version is still there, by id and in the history.
    assert store.narrative_by_id(first)["summary"] == "original"
    assert store.narrative_by_id(9999) is None
    assert store.narrative_history("JE-1")["summary"].tolist() == ["original", "updated"]
    assert len(store.narratives_frame(latest_only=False)) == 2


def test_review_state_says_which_note_the_reviewer_saw(store):
    """The one answer the workpaper and the MCP server both read."""
    empty = store.review_state()
    assert empty.empty and list(empty.columns) == list(REVIEW_STATE_COLUMNS)

    # JE-1: decided against note 1, which was then rewritten.
    seen = store.save_narrative("JE-1", narrative("what ana read"))
    store.record(Decision("JE-1", "escalate", "ana", narrative_id=seen))
    store.save_narrative("JE-1", narrative("rewritten"))
    # JE-2: narrated twice, never decided.
    store.save_narrative("JE-2", narrative("first"))
    newest = store.save_narrative("JE-2", narrative("latest"))
    # JE-3: decided, never narrated.
    store.record(Decision("JE-3", "dismiss", "ben"))
    # JE-4: decided with no note, then narrated. The decision is inserted with
    # an earlier timestamp through a raw connection (an insert with a new id is
    # allowed) so the test does not have to wait a second.
    with sqlite3.connect(str(store.path)) as raw:
        raw.execute("INSERT INTO decisions (entry_id, decision, reviewer, note, decided_at, ledger_id) "
                    "VALUES ('JE-4', 'accept', 'ben', 'checked', '2026-01-01T00:00:00+00:00', ?)",
                    (LEDGER,))
    later = store.save_narrative("JE-4", narrative("written after the decision"))
    # JE-5: narrated, then decided without recording the note.
    existing = store.save_narrative("JE-5", narrative("already there"))
    store.record(Decision("JE-5", "dismiss", "ana"))

    state = store.review_state().set_index("entry_id")
    assert list(state.index) == ["JE-1", "JE-2", "JE-3", "JE-4", "JE-5"]

    assert state.at["JE-1", "narrative_id"] == seen
    assert state.at["JE-1", "narrative_summary"] == "what ana read"
    assert state.at["JE-1", "narrative_superseded"] is True or state.at["JE-1", "narrative_superseded"] == True  # noqa: E712
    assert state.at["JE-1", "narrative_seen_by_reviewer"] == "yes"
    assert state.at["JE-1", "decision"] == "escalate"

    assert state.at["JE-2", "narrative_id"] == newest
    assert state.at["JE-2", "narrative_summary"] == "latest"
    assert not state.at["JE-2", "narrative_superseded"]
    assert state.at["JE-2", "narrative_seen_by_reviewer"] == ""
    assert pd.isna(state.at["JE-2", "decision"])

    assert pd.isna(state.at["JE-3", "narrative_id"])
    assert pd.isna(state.at["JE-3", "narrative_summary"])
    assert state.at["JE-3", "narrative_seen_by_reviewer"] == ""
    assert state.at["JE-3", "decision"] == "dismiss"

    # The later note is shown, but not passed off as the basis of the decision.
    assert state.at["JE-4", "narrative_id"] == later
    assert not state.at["JE-4", "narrative_superseded"]
    assert state.at["JE-4", "narrative_seen_by_reviewer"] == "no"

    assert state.at["JE-5", "narrative_id"] == existing
    assert state.at["JE-5", "narrative_seen_by_reviewer"] == "unknown"

    versions = store.narrative_versions("JE-1")
    assert [v["summary"] for v in versions] == ["what ana read", "rewritten"]
    assert versions[0]["evidence_to_request"] == ["a", "b"]
    assert store.narrative_versions("JE-9") == []


def test_a_decision_records_the_narrative_the_reviewer_saw(store):
    seen = store.save_narrative("JE-1", narrative("what the reviewer read"))
    store.record(Decision("JE-1", "escalate", "ana", "needs a senior", narrative_id=seen))
    store.save_narrative("JE-1", narrative("rewritten afterwards"))

    history = store.history("JE-1")
    assert history["narrative_id"].tolist() == [seen]
    assert store.narrative_by_id(seen)["summary"] == "what the reviewer read"
    assert store.get_narrative("JE-1")["summary"] == "rewritten afterwards"
    assert store.current().iloc[0]["narrative_id"] == seen

    # A decision may point at nothing (no note was on screen), never at another
    # entry's note or at a note that does not exist.
    store.record(Decision("JE-1", "dismiss", "ana"))
    assert pd.isna(store.history("JE-1")["narrative_id"].iloc[-1])
    with pytest.raises(ValueError, match="not a narrative for entry JE-2"):
        store.record(Decision("JE-2", "accept", "ana", narrative_id=seen))
    with pytest.raises(ValueError, match="not a narrative"):
        store.record(Decision("JE-1", "accept", "ana", narrative_id=9999))


@pytest.mark.parametrize("statement", [
    "UPDATE decisions SET decision = 'accept'",
    "DELETE FROM decisions",
    "UPDATE narratives SET summary = 'edited'",
    "DELETE FROM narratives",
    # REPLACE deletes the conflicting row without firing delete triggers
    # (unless recursive_triggers is on, and it is off by default), so it is
    # caught on the way in instead.
    "REPLACE INTO decisions (id, entry_id, decision, reviewer, decided_at) "
    "VALUES (1, 'JE-1', 'accept', 'mallory', '2026-01-01T00:00:00+00:00')",
    "INSERT OR REPLACE INTO narratives (id, entry_id, summary, generated_at) "
    "VALUES (1, 'JE-1', 'rewritten', '2026-01-01T00:00:00+00:00')",
    "INSERT INTO narratives (id, entry_id, summary, generated_at) "
    "VALUES (1, 'JE-1', 'duplicate id', '2026-01-01T00:00:00+00:00')",
])
def test_the_database_itself_refuses_to_change_history(store, statement):
    """Append-only is enforced by SQLite, not just by this module's API.

    Anything that opens the file - a stray script, a DB browser, a future
    tool with a write method - hits the same wall, including the REPLACE
    route around the delete trigger.
    """
    seen = store.save_narrative("JE-1", narrative())
    store.record(Decision("JE-1", "dismiss", "ana", narrative_id=seen))

    with sqlite3.connect(str(store.path)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(statement)
    assert store.history("JE-1")["decision"].tolist() == ["dismiss"]
    assert store.history("JE-1")["reviewer"].tolist() == ["ana"]
    assert store.get_narrative("JE-1")["summary"] == "s"
    # Ordinary appends still work, with or without an explicit new id.
    assert store.save_narrative("JE-1", narrative("second")) == 2
    with sqlite3.connect(str(store.path)) as raw:
        raw.execute("INSERT INTO narratives (id, entry_id, summary, generated_at, ledger_id) "
                    "VALUES (50, 'JE-1', 'explicit new id', '2026-01-01T00:00:00+00:00', ?)",
                    (LEDGER,))
    assert store.get_narrative("JE-1")["id"] == 50


def test_decisions_survive_a_restart(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewStore(path, LEDGER).record(Decision("JE-1", "escalate", "ana", risk_score=5.0, model_score=0.9))

    reopened = ReviewStore(path, LEDGER)
    history = reopened.history("JE-1")
    assert len(history) == 1
    assert history.iloc[0]["risk_score"] == 5.0
    assert reopened.decided_ids() == {"JE-1"}


# The schema before narratives were versioned (v0.3.0): entry_id was the
# narrative key, so a rewrite replaced the note, and decisions did not say
# which note they were made against.
V030_SCHEMA = """
CREATE TABLE decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     TEXT    NOT NULL,
    decision     TEXT    NOT NULL CHECK (decision IN ('accept','dismiss','escalate')),
    reviewer     TEXT    NOT NULL,
    note         TEXT,
    risk_score   REAL,
    model_score  REAL,
    decided_at   TEXT    NOT NULL
);
CREATE INDEX ix_decisions_entry ON decisions(entry_id);
CREATE INDEX ix_decisions_time  ON decisions(decided_at);
CREATE TABLE narratives (
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


def make_v030_database(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(V030_SCHEMA)
    conn.executemany("INSERT INTO narratives VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        ("JE-2", "second", "w", "a\nb", "c", "low", "m", "2026-09-23T10:00:00+00:00"),
        ("JE-1", "first", "w", "e", "c", "high", "m", "2026-09-23T09:00:00+00:00"),
    ])
    conn.execute(
        "INSERT INTO decisions (entry_id, decision, reviewer, note, decided_at) "
        "VALUES ('JE-1', 'accept', 'ana', 'ok', '2026-09-23T11:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_a_database_from_before_versioning_is_migrated_on_open(tmp_path):
    path = tmp_path / "review.sqlite"
    make_v030_database(path)

    store = ReviewStore(path, LEDGER)
    # The old rows are filed under 'legacy' in generation order, and a store
    # bound to a ledger does not see them; adopt_legacy() is the way in.
    assert store.narrative_ids() == set() and store.decided_ids() == set()
    assert store.get_narrative("JE-1") is None and store.history("JE-1").empty
    assert store.narrative_by_id(1)["entry_id"] == "JE-1"  # ids follow the generation order
    assert store.narrative_by_id(2) == {
        "id": 2, "entry_id": "JE-2", "summary": "second", "why_flagged": "w",
        "evidence_to_request": ["a", "b"], "suggested_control": "c", "confidence": "low",
        "model": "m", "generated_at": "2026-09-23T10:00:00+00:00", "ledger_id": "legacy",
    }
    assert store.ledgers().to_dict("records") == [
        {"ledger_id": "legacy", "narratives": 2, "decisions": 1}]
    with sqlite3.connect(str(path)) as raw:
        legacy = raw.execute("SELECT narrative_id FROM decisions WHERE ledger_id = 'legacy'").fetchall()
    assert legacy == [(None,)]  # nothing recorded what that reviewer saw

    # Opening again is a no-op, and the store behaves like a fresh one from here.
    again = ReviewStore(path, LEDGER)
    assert again.save_narrative("JE-1", narrative("third")) == 3
    with pytest.raises(ValueError, match="not a narrative"):
        again.record(Decision("JE-2", "dismiss", "ben", narrative_id=2))  # a legacy note
    assert again.record(Decision("JE-1", "dismiss", "ben", narrative_id=3)) == 2
    found = {(r[0], r[1]) for r in sqlite3.connect(str(path)).execute(
        "SELECT type, name FROM sqlite_master")}
    assert ("table", "narratives_v1") not in found
    for name in review.GUARDS:
        assert ("trigger", name) in found  # the migrated file gets every guard too
    assert schema_version(path) == SCHEMA_VERSION


# The shape PR #4 wrote, as committed in 50b2fa6: versioned narratives,
# narrative_id on decisions, the update and delete guards - no REPLACE guards
# and no stamp. Written out rather than derived from V030_SCHEMA, so a
# reformat of either literal cannot quietly turn this into a v1 file.
V2_SCHEMA = """
CREATE TABLE decisions (
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
CREATE INDEX ix_decisions_entry ON decisions(entry_id);
CREATE INDEX ix_decisions_time  ON decisions(decided_at);
CREATE TABLE narratives (
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
CREATE INDEX ix_narratives_entry ON narratives(entry_id);
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER narratives_no_update BEFORE UPDATE ON narratives
BEGIN SELECT RAISE(ABORT, 'narratives are append-only'); END;
CREATE TRIGGER narratives_no_delete BEFORE DELETE ON narratives
BEGIN SELECT RAISE(ABORT, 'narratives are append-only'); END;
"""


def make_v2_database(path):
    """A file as v0.3.1's predecessor wrote it: versioned narratives, no REPLACE guards, no stamp."""
    conn = sqlite3.connect(str(path))
    conn.executescript(V2_SCHEMA)
    conn.execute("INSERT INTO narratives (entry_id, summary, generated_at) "
                 "VALUES ('JE-1', 'kept', '2026-09-24T00:00:00+00:00')")
    conn.commit()
    conn.close()


def shape(path):
    """How the module classifies a file, before any opener touches it."""
    with closing(sqlite3.connect(str(path))) as conn:
        return review._schema_version(conn, path)


def objects(path):
    """Column names per table, index names and trigger names: what a migration must reproduce."""
    with closing(sqlite3.connect(str(path))) as conn:
        names = {(r[0], r[1]) for r in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        columns = {t: set(review._columns(conn, t)) for t in ("decisions", "narratives")}
    return names, columns


def test_each_older_shape_is_recognised_and_walked_up_to_the_current_version(tmp_path):
    v1, v2 = tmp_path / "v1.sqlite", tmp_path / "v2.sqlite"
    make_v030_database(v1)
    make_v2_database(v2)
    assert shape(v1) == (1, False) and shape(v2) == (2, False)  # unstamped, recognised by shape
    store = ReviewStore(v2, LEDGER)
    assert schema_version(v2) == SCHEMA_VERSION
    assert store.narrative_by_id(1)["summary"] == "kept"  # filed as legacy
    with sqlite3.connect(str(v2)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("REPLACE INTO narratives (id, entry_id, generated_at) VALUES (1, 'JE-1', 'x')")

    # Whichever version a file started at, it ends with the objects a fresh file has.
    ReviewStore(v1, LEDGER)
    fresh = tmp_path / "fresh.sqlite"
    ReviewStore(fresh, LEDGER)
    assert objects(v1) == objects(v2) == objects(fresh)

    # A file already at the current shape but unstamped is stamped and otherwise untouched.
    current = tmp_path / "current.sqlite"
    ReviewStore(current, LEDGER)
    with sqlite3.connect(str(current)) as raw:
        raw.execute("PRAGMA user_version = 0")
    assert shape(current) == (4, False)
    ReviewStore(current, LEDGER)
    assert schema_version(current) == SCHEMA_VERSION


def make_v3_database(path):
    """A file as v0.3.1 wrote it: the schema-3 shape, every guard, no ledger_id, no stamp."""
    conn = sqlite3.connect(str(path))
    conn.executescript(V2_SCHEMA + """
CREATE TRIGGER decisions_no_replace BEFORE INSERT ON decisions
WHEN EXISTS (SELECT 1 FROM decisions WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER narratives_no_replace BEFORE INSERT ON narratives
WHEN EXISTS (SELECT 1 FROM narratives WHERE id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'narratives are append-only'); END;
""")
    conn.execute("INSERT INTO narratives (entry_id, summary, generated_at) "
                 "VALUES ('JE-1', 'kept', '2026-09-25T00:00:00+00:00')")
    conn.execute("INSERT INTO decisions (entry_id, decision, reviewer, decided_at) "
                 "VALUES ('JE-1', 'accept', 'ana', '2026-09-25T01:00:00+00:00')")
    conn.commit()
    conn.close()


def test_a_v031_file_is_read_as_three_and_migrated_with_its_rows_filed_as_legacy(tmp_path):
    """Stamped 3 or not: the unstamped case is the one the literal in the ladder protects."""
    for stamp in (0, 3):
        path = tmp_path / f"v3-{stamp}.sqlite"
        make_v3_database(path)
        with sqlite3.connect(str(path)) as raw:
            raw.execute(f"PRAGMA user_version = {stamp}")
        assert shape(path) == (3, stamp == 3)
        store = ReviewStore(path, LEDGER)
        assert schema_version(path) == 4
        with closing(sqlite3.connect(str(path))) as raw:
            info = {r[1]: r for r in raw.execute("PRAGMA table_info(decisions)")}
            assert info["ledger_id"][4] == "'legacy'"  # a migrated file's default; a fresh one has none
            names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert "ix_decisions_entry" not in names and "ix_decisions_ledger_entry" in names
        assert store.ledgers().to_dict("records") == [
            {"ledger_id": "legacy", "narratives": 1, "decisions": 1}]
        assert store.narrative_ids() == set()
        ReviewStore(path, LEDGER)  # a second open is a no-op
        assert schema_version(path) == 4


def test_the_shape_ladder_ends_in_a_literal_so_a_later_version_still_walks_its_step(
        tmp_path, monkeypatch):
    """An unstamped file is read by its shape, which must not say "current" once the constant moves."""
    current = tmp_path / "current.sqlite"
    ReviewStore(current, LEDGER)
    with sqlite3.connect(str(current)) as raw:
        raw.execute("PRAGMA user_version = 0")
    assert shape(current) == (4, False)
    monkeypatch.setattr(review, "SCHEMA_VERSION", 5)
    monkeypatch.setitem(review._MIGRATIONS, 4,
                        lambda conn: "ALTER TABLE narratives ADD COLUMN source_system TEXT;\n")
    ReviewStore(current, LEDGER)
    assert schema_version(current) == 5
    with sqlite3.connect(str(current)) as raw:
        assert "source_system" in review._columns(raw, "narratives")


def test_migration_steps_never_read_the_live_schema(tmp_path, monkeypatch):
    """When a later version adds a column, a v1 file gains it once, in that version's own step."""
    path = tmp_path / "review.sqlite"
    make_v030_database(path)
    future = review.SCHEMA.replace("    ledger_id           TEXT NOT NULL\n);",
                                   "    ledger_id           TEXT NOT NULL,\n    source_system TEXT\n);")
    assert future != review.SCHEMA
    monkeypatch.setattr(review, "SCHEMA", future)
    monkeypatch.setattr(review, "SCHEMA_VERSION", 5)
    monkeypatch.setitem(review._MIGRATIONS, 4,
                        lambda conn: "ALTER TABLE narratives ADD COLUMN source_system TEXT;\n")
    store = ReviewStore(path, LEDGER)  # "duplicate column" if step 1 -> 2 had used the live DDL
    assert schema_version(path) == 5
    assert store.narrative_by_id(2)["summary"] == "second"
    with sqlite3.connect(str(path)) as raw:
        assert review._columns(raw, "narratives").count("source_system") == 1


def test_a_dropped_guard_is_put_back_by_a_writer_and_refused_by_a_reader(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewStore(path, LEDGER).record(Decision("JE-1", "accept", "ana"))
    with sqlite3.connect(str(path)) as raw:
        raw.execute("DROP TRIGGER decisions_no_update")
        raw.execute("DROP TRIGGER narratives_no_replace")
    with pytest.raises(RuntimeError, match="decisions_no_update, narratives_no_replace"):
        ReviewStore.read_only(path, LEDGER)

    # A writer puts every guard back on open, whatever the stamp says.
    ReviewStore(path, LEDGER)
    with sqlite3.connect(str(path)) as raw:
        assert review._triggers(raw) == set(review.GUARDS)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE decisions SET decision = 'dismiss'")
    assert ReviewStore.read_only(path, LEDGER).history("JE-1")["decision"].tolist() == ["accept"]
    assert schema_version(path) == SCHEMA_VERSION


def test_a_foreign_or_damaged_file_is_refused_with_a_message(tmp_path):
    foreign = tmp_path / "places.sqlite"
    with closing(sqlite3.connect(str(foreign))) as raw:
        raw.execute("CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT)")
        raw.execute("PRAGMA user_version = 3")
        raw.commit()
    for opener in (ReviewStore, ReviewStore.read_only):
        with pytest.raises(RuntimeError, match="not a LedgerLens review database"):
            opener(foreign, LEDGER)
    with closing(sqlite3.connect(str(foreign))) as raw:
        raw.execute("PRAGMA user_version = -1")
        raw.commit()
    before = foreign.read_bytes()
    with pytest.raises(RuntimeError, match="not a LedgerLens review database"):
        ReviewStore(foreign, LEDGER)
    assert foreign.read_bytes() == before

    text = tmp_path / "notes.sqlite"
    text.write_text("this is not a database\n")
    for opener in (ReviewStore, ReviewStore.read_only):
        with pytest.raises(RuntimeError, match="not a SQLite database"):
            opener(text, LEDGER)
    assert text.read_text() == "this is not a database\n"

    with pytest.raises(RuntimeError, match="cannot be opened|not a SQLite database"):
        ReviewStore(tmp_path, LEDGER)  # a directory


def test_a_writer_that_loses_a_migration_race_carries_on_from_where_the_winner_left_it(
        tmp_path, monkeypatch):
    path = tmp_path / "review.sqlite"
    make_v030_database(path)
    real, raced = review._MIGRATIONS[1], []

    def stale_step(conn):
        script = real(conn)      # built from the v1 shape...
        if not raced:
            raced.append(True)
            ReviewStore(path, LEDGER)    # ...while another writer takes the file to the current version
        return script

    monkeypatch.setitem(review._MIGRATIONS, 1, stale_step)
    store = ReviewStore(path, LEDGER)    # its stale step fails, rolls back, and it re-reads the file
    assert raced and schema_version(path) == SCHEMA_VERSION
    assert store.ledgers().to_dict("records") == [
        {"ledger_id": "legacy", "narratives": 2, "decisions": 1}]
    assert store.narrative_by_id(2)["entry_id"] == "JE-2"
    with sqlite3.connect(str(path)) as raw:
        assert review._triggers(raw) == set(review.GUARDS)
        names = {r[1] for r in raw.execute("SELECT type, name FROM sqlite_master")}
    assert "narratives_v1" not in names


def test_a_file_from_a_newer_version_is_refused_by_both_openers(tmp_path):
    path = tmp_path / "future.sqlite"
    ReviewStore(path, LEDGER)
    with sqlite3.connect(str(path)) as raw:
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="newer LedgerLens"):
        ReviewStore(path, LEDGER)
    with pytest.raises(RuntimeError, match="newer LedgerLens"):
        ReviewStore.read_only(path, LEDGER)
    assert path.read_bytes() == before


def test_a_read_only_store_reads_everything_and_writes_nothing(tmp_path):
    path = tmp_path / "review.sqlite"
    writer = ReviewStore(path, LEDGER)
    seen = writer.save_narrative("JE-1", narrative())
    writer.record(Decision("JE-1", "dismiss", "ana", narrative_id=seen))

    reader = ReviewStore.read_only(path, LEDGER)
    assert reader.is_read_only
    assert reader.get_narrative("JE-1")["id"] == seen
    assert reader.current()["decision"].tolist() == ["dismiss"]
    assert reader.history("JE-1")["narrative_id"].tolist() == [seen]
    assert reader.narrative_ids() == {"JE-1"}

    # SQLite refuses the write; nothing in this module has to remember to.
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.record(Decision("JE-1", "accept", "ana"))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.save_narrative("JE-1", narrative("again"))
    assert writer.history("JE-1")["decision"].tolist() == ["dismiss"]
    assert len(writer.narrative_history("JE-1")) == 1


def test_a_read_only_store_never_creates_migrates_or_touches_a_file(tmp_path):
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(FileNotFoundError):
        ReviewStore.read_only(absent, LEDGER)
    assert not absent.exists()

    # A zero-byte file would have had the schema written into it by a
    # read-write open; a reader reports it instead.
    empty = tmp_path / "empty.sqlite"
    empty.touch()
    with pytest.raises(RuntimeError, match="ledgerlens narrate"):
        ReviewStore.read_only(empty, LEDGER)
    assert empty.stat().st_size == 0

    old = tmp_path / "old.sqlite"
    make_v030_database(old)
    before = old.read_bytes()
    with pytest.raises(RuntimeError, match="older schema"):
        ReviewStore.read_only(old, LEDGER)
    assert old.read_bytes() == before


def test_the_only_decisions_are_the_documented_ones():
    assert DECISIONS == ("accept", "dismiss", "escalate")


# --- one store, one ledger ------------------------------------------------------


def test_a_store_must_be_bound_to_a_real_ledger(tmp_path):
    path = tmp_path / "review.sqlite"
    with pytest.raises(TypeError):
        ReviewStore(path)
    for bad in ("", "csv: x", "legacy", None):
        with pytest.raises(ValueError):
            ReviewStore(path, bad)
    assert not path.exists()  # refused before any file was touched
    ReviewStore(path, LEDGER)
    with pytest.raises(ValueError, match="legacy"):
        ReviewStore.read_only(path, "legacy")


def test_two_ledgers_sharing_an_entry_id_never_see_each_others_rows(tmp_path):
    path = tmp_path / "review.sqlite"
    a, b = ReviewStore(path, LEDGER), ReviewStore(path, OTHER)
    note = a.save_narrative("JE-2024-000001", narrative("a's note"))
    a.record(Decision("JE-2024-000001", "accept", "ana", narrative_id=note))

    assert b.get_narrative("JE-2024-000001") is None
    assert b.narrative_ids() == set() and b.decided_ids() == set()
    assert b.current().empty and b.history("JE-2024-000001").empty
    assert b.review_state().empty and b.narratives_frame().empty
    assert b.narrative_versions("JE-2024-000001") == []
    with pytest.raises(ValueError, match="not a narrative for entry JE-2024-000001"):
        b.record(Decision("JE-2024-000001", "dismiss", "ben", narrative_id=note))  # a's note

    other = b.save_narrative("JE-2024-000001", narrative("b's note"))
    assert a.get_narrative("JE-2024-000001")["id"] == note
    assert b.get_narrative("JE-2024-000001")["id"] == other
    assert a.narrative_by_id(other)["ledger_id"] == OTHER  # by id is global, and says so
    assert a.ledgers().to_dict("records") == [
        {"ledger_id": LEDGER, "narratives": 1, "decisions": 1},
        {"ledger_id": OTHER, "narratives": 1, "decisions": 0},
    ]
    assert a.other_ledgers()["ledger_id"].tolist() == [OTHER]
    assert b.other_ledgers()["ledger_id"].tolist() == [LEDGER]


def test_a_fresh_file_refuses_a_row_that_names_no_ledger(store):
    with sqlite3.connect(str(store.path)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
            raw.execute("INSERT INTO narratives (id, entry_id, summary, generated_at) "
                        "VALUES (7, 'JE-1', 'no ledger', '2026-01-01T00:00:00+00:00')")


def test_a_read_only_store_bound_to_another_ledger_sees_nothing_but_the_counts(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewStore(path, LEDGER).record(Decision("JE-1", "accept", "ana"))
    reader = ReviewStore.read_only(path, OTHER)
    assert reader.current().empty and reader.decided_ids() == set()
    assert reader.ledgers().to_dict("records") == [
        {"ledger_id": LEDGER, "narratives": 0, "decisions": 1}]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.record(Decision("JE-1", "accept", "ana"))


# --- adopting rows from before ledgers were keyed ------------------------------


def _legacy_rows(path):
    """What a migrated v0.3.1 file holds: rows filed under 'legacy', one decision on a note."""
    with sqlite3.connect(str(path)) as raw:
        raw.execute("INSERT INTO narratives (id, entry_id, summary, why_flagged, evidence_to_request, "
                    "suggested_control, confidence, model, generated_at, ledger_id) VALUES "
                    "(1, 'JE-1', 'first', 'w', 'e', 'c', 'high', 'm', '2026-09-23T09:00:00+00:00', 'legacy')")
        raw.execute("INSERT INTO narratives (id, entry_id, summary, why_flagged, evidence_to_request, "
                    "suggested_control, confidence, model, generated_at, ledger_id) VALUES "
                    "(2, 'JE-2', 'second', 'w', 'a\nb', 'c', 'low', 'm', '2026-09-23T10:00:00+00:00', 'legacy')")
        raw.execute("INSERT INTO decisions (id, entry_id, decision, reviewer, note, narrative_id, "
                    "decided_at, ledger_id) VALUES (1, 'JE-1', 'accept', 'ana', 'ok', 1, "
                    "'2026-09-23T11:00:00+00:00', 'legacy')")


def test_adopt_legacy_copies_rows_and_remaps_narrative_ids(tmp_path):
    path = tmp_path / "review.sqlite"
    store = ReviewStore(path, LEDGER)
    _legacy_rows(path)
    assert store.narrative_ids() == set()
    adopted = store.adopt_legacy()
    assert adopted == {"ledger_id": LEDGER, "narratives": 2, "decisions": 1,
                       "narrative_ids": {1: 3, 2: 4}}
    assert store.narrative_ids() == {"JE-1", "JE-2"} and store.decided_ids() == {"JE-1"}
    assert store.history("JE-1")["narrative_id"].tolist() == [3]  # points at the copy of note 1
    state = store.review_state().set_index("entry_id")
    assert state.loc["JE-1", "narrative_seen_by_reviewer"] == "yes"
    assert store.get_narrative("JE-2") == {
        "id": 4, "entry_id": "JE-2", "summary": "second", "why_flagged": "w",
        "evidence_to_request": ["a", "b"], "suggested_control": "c", "confidence": "low",
        "model": "m", "generated_at": "2026-09-23T10:00:00+00:00", "ledger_id": LEDGER,
    }
    assert store.ledgers().to_dict("records") == [
        {"ledger_id": LEDGER, "narratives": 2, "decisions": 1},
        {"ledger_id": "legacy", "narratives": 2, "decisions": 1},  # the originals stay
    ]
    assert store.save_narrative("JE-1", narrative("third")) == 5
    assert store.record(Decision("JE-2", "dismiss", "ben", narrative_id=4)) == 3


def test_adopt_legacy_refuses_rows_that_are_not_this_ledgers_or_a_ledger_that_has_rows(tmp_path):
    path = tmp_path / "review.sqlite"
    store = ReviewStore(path, LEDGER)
    _legacy_rows(path)
    with pytest.raises(ValueError, match="not in this ledger"):
        store.adopt_legacy(entry_ids={"JE-1"})  # JE-2 is a stray
    assert store.ledgers()["ledger_id"].tolist() == ["legacy"]  # nothing written
    store.adopt_legacy(entry_ids={"JE-1", "JE-2"})
    with pytest.raises(ValueError, match="already holds"):
        store.adopt_legacy()
    assert store.ledgers()["narratives"].tolist() == [2, 2]

    fresh = tmp_path / "fresh.sqlite"
    assert ReviewStore(fresh, LEDGER).adopt_legacy()["narratives"] == 0  # nothing to adopt
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ReviewStore.read_only(path, OTHER).adopt_legacy()

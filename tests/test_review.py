import sqlite3

import pandas as pd
import pytest

from ledgerlens import jets
from ledgerlens.review import DECISIONS, Decision, ReviewStore


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "review.sqlite")


def narrative(summary="s"):
    return {"summary": summary, "why_flagged": "w", "evidence_to_request": ["a", "b"],
            "suggested_control": "c", "confidence": "low"}


def test_store_creates_its_schema_on_a_fresh_path(tmp_path):
    path = tmp_path / "nested" / "review.sqlite"
    ReviewStore(path)
    assert path.exists()


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
])
def test_the_database_itself_refuses_to_change_history(store, statement):
    """Append-only is enforced by SQLite, not just by this module's API.

    Anything that opens the file - a stray script, a DB browser, a future
    tool with a write method - hits the same wall.
    """
    seen = store.save_narrative("JE-1", narrative())
    store.record(Decision("JE-1", "dismiss", "ana", narrative_id=seen))

    with sqlite3.connect(str(store.path)) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(statement)
    assert store.history("JE-1")["decision"].tolist() == ["dismiss"]
    assert store.get_narrative("JE-1")["summary"] == "s"


def test_decisions_survive_a_restart(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewStore(path).record(Decision("JE-1", "escalate", "ana", risk_score=5.0, model_score=0.9))

    reopened = ReviewStore(path)
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

    store = ReviewStore(path)
    # Ids follow the generation order, not the old table's order.
    assert store.get_narrative("JE-1")["id"] == 1
    assert store.get_narrative("JE-2") == {
        "id": 2, "entry_id": "JE-2", "summary": "second", "why_flagged": "w",
        "evidence_to_request": ["a", "b"], "suggested_control": "c", "confidence": "low",
        "model": "m", "generated_at": "2026-09-23T10:00:00+00:00",
    }
    assert store.narrative_ids() == {"JE-1", "JE-2"}
    history = store.history("JE-1")
    assert history["decision"].tolist() == ["accept"]
    assert pd.isna(history["narrative_id"].iloc[0])  # nothing recorded what that reviewer saw

    # Opening again is a no-op, and the store behaves like a fresh one from here.
    again = ReviewStore(path)
    assert again.save_narrative("JE-1", narrative("third")) == 3
    assert again.record(Decision("JE-2", "dismiss", "ben", narrative_id=2)) == 2
    objects = {(r[0], r[1]) for r in sqlite3.connect(str(path)).execute(
        "SELECT type, name FROM sqlite_master")}
    assert ("table", "narratives_v1") not in objects
    assert ("trigger", "decisions_no_update") in objects  # the migrated file gets the guards too


def test_the_only_decisions_are_the_documented_ones():
    assert DECISIONS == ("accept", "dismiss", "escalate")

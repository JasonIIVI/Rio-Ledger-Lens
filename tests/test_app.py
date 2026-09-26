"""Smoke test for the dashboard.

Streamlit's AppTest runs the script headlessly. The point is not to test
Streamlit; it is to catch the class of breakage week 2 hit, where a deprecated
argument silently collapsed every table, and to prove the review loop is wired.
"""

import sqlite3
from pathlib import Path

import pytest

from ledgerlens.cli import main
from ledgerlens.review import Decision, ReviewStore

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("dash")
    main(["generate", "--start", "2024-01-01", "--end", "2024-04-30", "--out-dir", str(out)])
    return out


@pytest.fixture(scope="module")
def identity(data_dir):
    """What the dashboard binds its store to for this ledger."""
    from ledgerlens.ingest import ledger_identity, load_csv

    return ledger_identity(load_csv(data_dir / "ledger.csv"), data_dir / "ledger.csv")


@pytest.fixture(autouse=True)
def _away_from_the_repo(monkeypatch, tmp_path):
    """The script's default paths are relative; nothing here may touch the real data/."""
    monkeypatch.chdir(tmp_path)


def _run(data_dir, db, reviewer=""):
    at = AppTest.from_file(str(APP), default_timeout=300)
    # The inputs are seeded before the first run, so the script never opens its
    # default data/review.sqlite (a developer's own) on the way to the test's.
    at.session_state["ledger_path"] = str(data_dir / "ledger.csv")
    at.session_state["labels_path"] = str(data_dir / "labels.csv")
    at.session_state["db_path"] = str(db)
    at.session_state["reviewer"] = reviewer
    at.run()
    assert not at.exception, at.exception
    return at


def test_dashboard_renders_the_queue_and_the_review_loop(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")

    labels = [m.label for m in at.metric]
    for expected in ("Entries", "Flagged", "Decided", "Rule score", "Model score"):
        assert expected in labels
    assert [o.lower() for o in at.radio[0].options] == ["accept", "dismiss", "escalate"]
    assert any("Record decision" in b.label for b in at.button)


def _note(summary):
    return {"summary": summary, "why_flagged": "w", "evidence_to_request": ["x"],
            "suggested_control": "c", "confidence": "low"}


def _submit(at, decision="Accept", note="fine"):
    picked = at.selectbox(key="picked").value
    at.radio(key=f"choice-{picked}").set_value(decision)
    at.text_area(key=f"note-{picked}").set_value(note)
    _click_record(at)


def _click_record(at):
    next(b for b in at.button if "Record decision" in b.label).click()
    at.run()
    assert not at.exception, at.exception


def test_a_decision_records_the_note_the_reviewer_read_not_one_written_meanwhile(data_dir, tmp_path, identity):
    """A submit reruns the script, so the note is fetched again at that moment.

    If a version was written in between, the decision must not claim the
    reviewer read it; the dashboard refuses and shows the new note instead.
    """
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    store = ReviewStore(db, identity)
    first = store.save_narrative(picked, _note("what ana read"), model="claude-test")
    at.run()
    assert any(f"note #{first}" in c.value for c in at.caption)

    second = store.save_narrative(picked, _note("written meanwhile"), model="claude-test")
    _submit(at)
    assert store.history(picked).empty
    assert any("changed while you were reading" in w.value for w in at.warning)
    assert any(f"note #{second}" in c.value for c in at.caption)  # the rerun shows the new one
    assert at.text_area(key=f"note-{picked}").value == "fine"  # the draft survived the refusal

    # Read it, decide again: this time the recorded note is the one on screen.
    _submit(at)
    history = store.history(picked)
    assert history["decision"].tolist() == ["accept"]
    assert history["narrative_id"].tolist() == [second]


def test_a_refusal_keeps_the_draft_and_the_next_click_records_it(data_dir, tmp_path, identity):
    """No note on screen, a note written before the submit: refused, nothing lost."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    store = ReviewStore(db, identity)
    written = store.save_narrative(picked, _note("written before the submit"), model="claude-test")

    _submit(at, "Escalate", "checked the purchase order")
    assert store.history(picked).empty
    assert any(f"now note #{written}" in w.value for w in at.warning)
    assert at.text_area(key=f"note-{picked}").value == "checked the purchase order"
    assert at.radio(key=f"choice-{picked}").value == "escalate"

    # The rerun showed the note; clicking again records the untouched draft
    # against it, and only then is the form cleared.
    _click_record(at)
    history = store.history(picked)
    assert history["decision"].tolist() == ["escalate"]
    assert history["note"].tolist() == ["checked the purchase order"]
    assert history["narrative_id"].tolist() == [written]
    assert at.text_area(key=f"note-{picked}").value == ""
    assert at.radio(key=f"choice-{picked}").value == "accept"


def test_a_decision_with_no_note_on_screen_records_none(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _submit(at, decision="Dismiss", note="routine")
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["dismiss"]
    assert history["narrative_id"].isna().all()


def test_dashboard_shows_recorded_decisions_and_the_note_they_saw(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    first = _run(data_dir, db, reviewer="ana")
    picked = first.selectbox(key="picked").value
    store = ReviewStore(db, identity)
    seen = store.save_narrative(picked, {
        "summary": "A note.", "why_flagged": "w", "evidence_to_request": ["x"],
        "suggested_control": "c", "confidence": "low",
    }, model="claude-test")
    store.record(Decision(picked, "escalate", "ana", "needs a senior", narrative_id=seen))

    at = _run(data_dir, db, reviewer="ana")
    decided = next(m for m in at.metric if m.label == "Decided")
    assert decided.value == "1"
    assert any("escalate 1" in c.value for c in at.caption)
    assert any(f"note #{seen}" in c.value for c in at.caption)
    assert any(identity in c.value for c in at.caption)  # the sidebar names the ledger
    assert any("Decision history" in m.value for m in at.markdown)


def test_the_dashboard_refuses_a_database_it_cannot_read_without_a_traceback(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    ReviewStore(db, identity)
    with sqlite3.connect(str(db)) as raw:
        raw.execute("PRAGMA user_version = 99")
    at = _run(data_dir, db, reviewer="ana")  # _run asserts the script raised nothing
    assert any("newer LedgerLens" in e.value for e in at.error)
    assert not any("Record decision" in b.label for b in at.button)  # stopped before the form

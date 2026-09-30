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


def _entry_key(at):
    """The dashboard keys an entry's widgets by database, ledger and entry id."""
    return next(r.key for r in at.radio if r.key.startswith("choice-"))[len("choice-"):]


def _submit(at, decision="Accept", note="fine"):
    key = _entry_key(at)
    at.radio(key=f"choice-{key}").set_value(decision)
    at.text_area(key=f"note-{key}").set_value(note)
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
    assert at.text_area(key=f"note-{_entry_key(at)}").value == "fine"  # the draft survived the refusal

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
    assert at.text_area(key=f"note-{_entry_key(at)}").value == "checked the purchase order"
    assert at.radio(key=f"choice-{_entry_key(at)}").value == "escalate"

    # The rerun showed the note; clicking again records the untouched draft
    # against it, and only then is the form cleared.
    _click_record(at)
    history = store.history(picked)
    assert history["decision"].tolist() == ["escalate"]
    assert history["note"].tolist() == ["checked the purchase order"]
    assert history["narrative_id"].tolist() == [written]
    assert at.text_area(key=f"note-{_entry_key(at)}").value == ""
    assert at.radio(key=f"choice-{_entry_key(at)}").value == "accept"


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


def _legacy_note_on_screen(data_dir, db, identity):
    """A file holding one note from before ledgers were keyed, for the entry the dashboard shows."""
    picked = _run(data_dir, db, reviewer="ana").selectbox(key="picked").value
    with sqlite3.connect(str(db)) as raw:
        raw.execute("INSERT INTO narratives (entry_id, summary, why_flagged, evidence_to_request, "
                    "suggested_control, confidence, generated_at, ledger_id) VALUES "
                    "(?, 'old', 'w', '[\"x\"]', 'c', 'low', '2026-09-23T00:00:00+00:00', 'legacy')",
                    (picked,))
    assert identity not in set(ReviewStore(db, identity).ledgers()["ledger_id"])
    return picked


def _record_button(at):
    return next(b for b in at.button if "Record decision" in b.label)


def _start_fresh_box(at):
    return next(c for c in at.sidebar.checkbox if "from scratch" in c.label)


def test_the_sidebar_names_other_ledgers_and_points_at_adopt_legacy(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    picked = _legacy_note_on_screen(data_dir, db, identity)
    at = _run(data_dir, db, reviewer="ana")
    assert at.selectbox(key="picked").value == picked
    assert any("1 other ledger" in c.value for c in at.caption)
    assert any("adopt-legacy" in w.value for w in at.sidebar.warning)
    # The legacy note is for the entry on screen, and it is not shown as this ledger's.
    assert not any("note #" in c.value for c in at.caption)
    assert any("No narrative yet for this entry." in c.value for c in at.caption)

    ReviewStore(db, identity).adopt_legacy()
    at = _run(data_dir, db, reviewer="ana")
    assert not any("adopt-legacy" in w.value for w in at.sidebar.warning)  # this ledger has rows now
    assert any("1 other ledger" in c.value for c in at.caption)  # the originals stay
    assert any("note #" in c.value for c in at.caption)  # the adopted copy is this ledger's note
    assert not _record_button(at).disabled


def test_no_write_can_shut_the_legacy_rows_out_before_a_choice_is_made(
        data_dir, tmp_path, identity, monkeypatch):
    """adopt_legacy copies only into a ledger with no rows: one note or decision
    written here first would make the legacy notes unadoptable and narrate would buy
    them again. Both write buttons wait until the rows are adopted or declined."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")  # so only the legacy rows disable it
    db = tmp_path / "review.sqlite"
    _legacy_note_on_screen(data_dir, db, identity)
    at = _run(data_dir, db, reviewer="ana")
    assert _record_button(at).disabled
    assert next(b for b in at.button if b.label == "Write narrative").disabled
    assert any("from scratch" in c.value for c in at.caption)
    assert identity not in set(ReviewStore(db, identity).ledgers()["ledger_id"])
    assert ReviewStore(db, identity).adopt_legacy()["narratives"] == 1  # still adoptable


def test_starting_from_scratch_is_an_explicit_choice(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    picked = _legacy_note_on_screen(data_dir, db, identity)
    at = _run(data_dir, db, reviewer="ana")
    _start_fresh_box(at).check()
    at.run()
    assert not _record_button(at).disabled

    # The choice is made for that file. Pointing the same session at another file
    # that also holds unadopted legacy rows asks again, with the box unticked: a
    # tick carried over would let one click there shut its legacy rows out.
    other = tmp_path / "other.sqlite"
    _legacy_note_on_screen(data_dir, other, identity)
    at.text_input(key="db_path").set_value(str(other))
    at.run()
    assert any("adopt-legacy" in w.value for w in at.sidebar.warning)
    assert not _start_fresh_box(at).value
    assert _record_button(at).disabled
    assert ReviewStore(other, identity).adopt_legacy()["narratives"] == 1  # still adoptable

    at.text_input(key="db_path").set_value(str(db))
    at.run()
    _start_fresh_box(at).check()
    at.run()
    _submit(at, "Dismiss", "looked fine")
    assert ReviewStore(db, identity).history(picked)["decision"].tolist() == ["dismiss"]
    at = _run(data_dir, db, reviewer="ana")
    assert not any("adopt-legacy" in w.value for w in at.sidebar.warning)  # the choice is made


def test_the_same_decision_on_a_shared_entry_id_in_another_ledger_is_recorded(
        data_dir, tmp_path, identity):
    """The double-click guard, the draft and the remembered note id belong to one
    ledger's entry: a second ledger's entry with the same id is a different entry."""
    import pandas as pd

    from ledgerlens import jets
    from ledgerlens.ingest import ledger_identity, load_csv

    lines = pd.read_csv(data_dir / "ledger.csv", dtype=str, keep_default_na=False)
    flagged = set(jets.run_all(load_csv(data_dir / "ledger.csv"))["entry_id"])
    quiet = lines.index[~lines["entry_id"].isin(flagged)][-1]
    lines.loc[quiet, "description"] = "a different ledger"  # same ids and flags, another digest
    other = tmp_path / "other"
    other.mkdir()
    lines.to_csv(other / "ledger.csv", index=False)
    other_id = ledger_identity(load_csv(other / "ledger.csv"), other / "ledger.csv")
    assert other_id != identity

    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _submit(at, "Accept", "")
    assert ReviewStore(db, identity).history(picked)["decision"].tolist() == ["accept"]
    # A draft typed here, not submitted, stays with this ledger's entry.
    key = _entry_key(at)
    at.radio(key=f"choice-{key}").set_value("Escalate")
    at.text_area(key=f"note-{key}").set_value("checked the PO for the first ledger")
    at.run()

    at.text_input(key="ledger_path").set_value(str(other / "ledger.csv"))
    at.run()
    assert at.selectbox(key="picked").value == picked
    other_key = _entry_key(at)
    assert at.radio(key=f"choice-{other_key}").value == "accept"  # the form starts over
    assert not at.text_area(key=f"note-{other_key}").value
    assert other_key != key
    _submit(at, "Accept", "")
    assert not any("just recorded" in w.value for w in at.warning)
    assert ReviewStore(db, other_id).history(picked)["decision"].tolist() == ["accept"]


def test_a_malformed_identity_sidecar_is_an_error_not_a_traceback(data_dir, tmp_path):
    import shutil

    bad = tmp_path / "bad"
    bad.mkdir()
    shutil.copy(data_dir / "ledger.csv", bad / "ledger.csv")
    (bad / "ledger.identity.json").write_text('{"ledger_id": "qbo 123"}')
    at = _run(bad, tmp_path / "review.sqlite", reviewer="ana")  # _run asserts nothing was raised
    assert any("ledger.identity.json" in e.value for e in at.error)
    assert not (tmp_path / "review.sqlite").exists()  # stopped before any store was opened


def test_the_sidebar_names_a_qbo_ledger_by_its_realm(data_dir, tmp_path):
    """Beside a QuickBooks pull's sidecar, the dashboard binds to qbo:<realm> and says so."""
    import shutil

    pulled = tmp_path / "pulled"
    pulled.mkdir()
    for name in ("ledger.csv", "labels.csv"):
        shutil.copy(data_dir / name, pulled / name)
    (pulled / "ledger.identity.json").write_text('{"ledger_id": "qbo:4620816365"}')
    db = tmp_path / "review.sqlite"
    at = _run(pulled, db, reviewer="ana")
    assert any("qbo:4620816365" in c.value for c in at.caption)
    assert set(ReviewStore(db, "qbo:4620816365").ledgers()["ledger_id"]) <= {"qbo:4620816365"}

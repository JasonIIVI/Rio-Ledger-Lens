"""Smoke test for the dashboard.

Streamlit's AppTest runs the script headlessly. The point is not to test
Streamlit; it is to catch the class of breakage week 2 hit, where a deprecated
argument silently collapsed every table, and to prove the review loop is wired.
"""

import os
import re
import shutil
import sqlite3
import string
import sys
from pathlib import Path

import pandas as pd
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
    assert at.radio(key=f"choice-{_entry_key(at)}").value is None


def test_a_decision_with_no_note_on_screen_records_none(data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _submit(at, decision="Dismiss", note="routine")
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["dismiss"]
    assert history["narrative_id"].isna().all()


def _narrating_with(monkeypatch, responses, llm):
    """The dashboard's Narrator, given a fake client. The key only enables the button, and
    the base URL is a closed local port: had the fake been bypassed, no request leaves here."""
    from ledgerlens import narrate

    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    client = llm.client(responses)
    real = narrate.Narrator
    monkeypatch.setattr(narrate, "Narrator", lambda **kw: real(client=client, **kw))
    return client


def _click_write(at):
    next(b for b in at.button if b.label in ("Write narrative", "Rewrite narrative")).click()
    at.run()
    assert not at.exception, at.exception


def test_a_note_written_here_records_the_model_the_response_names(
        data_dir, tmp_path, identity, monkeypatch, llm):
    dated = "claude-opus-5-20261001"
    client = _narrating_with(monkeypatch, [llm.response(model=dated)], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)

    assert [call["model"] for call in client.calls] == ["claude-opus-5"]
    assert ReviewStore(db, identity).get_narrative(picked)["model"] == dated
    # md() escapes every ASCII punctuation character, the hyphens included.
    assert any("Written by claude\\-opus\\-5\\-20261001 at" in c.value for c in at.caption)
    assert any(s.value.startswith("Narrative written as note #") for s in at.success)


def test_a_response_that_names_no_model_is_an_error_here_and_nothing_is_stored(
        data_dir, tmp_path, identity, monkeypatch, llm):
    response = llm.response()
    del response.model
    client = _narrating_with(monkeypatch, [response], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)

    assert len(client.calls) == 1
    assert any("named no model" in e.value for e in at.error)
    # the request was made and answered: a refusal of its reply is not the request failing
    assert not any("request to Claude failed" in e.value for e in at.error)
    assert ReviewStore(db, identity).get_narrative(picked) is None


def test_a_refusal_that_quotes_the_response_forms_no_markdown(
        data_dir, tmp_path, identity, monkeypatch, llm):
    """st.error draws markdown, and ascii() leaves ASCII markup as it is: an image
    named as the model would be fetched when the error is drawn. md() escapes it, as
    it does the note's prose (a bare URL is still linked there too)."""
    _narrating_with(monkeypatch, [llm.response(model=" ![x](http://127.0.0.1:9/x.png)")], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)

    errors = [e.value for e in at.error]
    assert any("\\!\\[x\\]\\(http\\:\\/\\/127\\.0\\.0\\.1\\:9\\/x\\.png\\)" in e for e in errors)
    assert not any("![x](" in e for e in errors)
    assert ReviewStore(db, identity).get_narrative(picked) is None


def _sdk_error(kind):
    """A real SDK error, built on the HTTP library the SDK's own types use (httpx under
    anthropic 0.x, httpx2 under 1.x). The 401's body quotes markdown, as a server's may."""
    anthropic = pytest.importorskip("anthropic")
    from anthropic import _exceptions

    http = getattr(_exceptions, "httpx2", None) or _exceptions.httpx
    request = http.Request("POST", "http://127.0.0.1:9/v1/messages")
    if kind == "no-connection":
        return anthropic.APIConnectionError(request=request)
    body = {"type": "error",
            "error": {"type": "authentication_error", "message": "![x](http://127.0.0.1:9/x.png)"}}
    return anthropic.AuthenticationError(f"Error code: 401 - {body}",
                                         response=http.Response(401, request=request), body=body)


@pytest.mark.parametrize("kind", ["no-connection", "refused-key"])
def test_an_sdk_error_is_a_message_here_and_the_page_goes_on(
        data_dir, tmp_path, identity, monkeypatch, llm, kind):
    """The SDK's errors are not NarrativeErrors. Uncaught, one drew Streamlit's traceback and
    stopped the run there, so the decision form below the note was not drawn."""
    error = _sdk_error(kind)
    client = _narrating_with(monkeypatch, [error], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)  # asserts the script raised nothing

    assert len(client.calls) == 1
    errors = [e.value for e in at.error]
    # md() escapes the comma, as it does every ASCII punctuation character
    sentence = "The request to Claude failed\\, so no note was stored"
    assert any(e.startswith(sentence) and type(error).__name__ in e for e in errors), errors
    assert ReviewStore(db, identity).get_narrative(picked) is None
    assert not _record_button(at).disabled  # the rest of the page was drawn
    if kind == "refused-key":
        # st.error draws markdown, and a status error quotes the server's body.
        assert any("\\!\\[x\\]\\(http\\:\\/\\/127\\.0\\.0\\.1\\:9\\/x\\.png\\)" in e for e in errors)
        assert not any("![x](" in e for e in errors)


NOT_STORED = ("The note came back but could not be stored\\, so it is not shown and no "
              "decision can point to it \\(")  # md() escapes every ASCII punctuation character
PAGE_WENT_ON = "answer different questions"  # the Tiers tab, drawn after the queue's form


def _refusing(monkeypatch, method, error):
    def refuse(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(ReviewStore, method, refuse)


def _assert_the_note_was_not_stored(at, db, identity, picked, client):
    assert len(client.calls) == 1  # requested and answered: it counts
    assert ReviewStore(db, identity).get_narrative(picked) is None
    errors = [e.value for e in at.error]
    assert any(e.startswith(NOT_STORED) for e in errors), errors
    assert any("This request still counts\\: 1 request\\(s\\)" in e for e in errors), errors
    assert not any("request to Claude failed" in e for e in errors)  # the request did not fail
    # Not drawn: a decision must point at advice on record, and this note is not. (The
    # note's summary opens like the page's own JET-05 reason, so its close is looked for.)
    assert not any("two days before period close" in m.value for m in at.markdown)
    assert not any("Evidence to request" in m.value for m in at.markdown)
    assert not any("note #" in c.value for c in at.caption)
    assert any("No narrative yet for this entry." in c.value for c in at.caption)
    assert not at.success
    assert not _record_button(at).disabled  # the rest of the page was drawn
    assert any(PAGE_WENT_ON in m.value for m in at.markdown)
    at.run()  # and nothing said it was written, on the next run either
    assert not at.exception, at.exception
    assert not at.success
    assert ReviewStore(db, identity).get_narrative(picked) is None


# Each refusal as md() shows it. A file that is no database any more is a DatabaseError, not
# an OperationalError: the store's errors are every sqlite3.Error but the driver's own faults.
STORE_REFUSALS = [
    pytest.param(sqlite3.OperationalError("database is locked ![x](http://127.0.0.1:9/x.png)"),
                 "OperationalError\\: database is locked "
                 "\\!\\[x\\]\\(http\\:\\/\\/127\\.0\\.0\\.1\\:9\\/x\\.png\\)", id="locked"),
    pytest.param(sqlite3.DatabaseError("file is not a database"),
                 "DatabaseError\\: file is not a database", id="not-a-database"),
    # A store's own guard (a file holding a row at id -1 refuses every insert as a replace)
    # and a value the file's limits refuse: the store's, not the driver's faults.
    pytest.param(sqlite3.IntegrityError("narratives are append-only"),
                 "IntegrityError\\: narratives are append\\-only", id="integrity"),
    pytest.param(sqlite3.DataError("string or blob too big"),
                 "DataError\\: string or blob too big", id="data"),
]


@pytest.mark.parametrize("error, shown", STORE_REFUSALS)
def test_a_note_the_store_refuses_is_a_message_and_is_not_drawn(
        data_dir, tmp_path, identity, monkeypatch, llm, error, shown):
    """A note that came back and then could not be saved was requested (and paid for).
    Uncaught, the store's error drew Streamlit's traceback, stopped the page there, and the
    note was gone without a word. The message quotes the error, so it goes through md()."""
    client = _narrating_with(monkeypatch, [llm.response()], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _refusing(monkeypatch, "save_narrative", error)

    _click_write(at)  # asserts the script raised nothing

    errors = [e.value for e in at.error]
    assert any(e.startswith(NOT_STORED + shown + "\\)\\.") for e in errors), errors
    assert not any("![x](" in e for e in errors)
    _assert_the_note_was_not_stored(at, db, identity, picked, client)


def _read_only(db):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root writes to a read-only file")
    db.chmod(0o444)


def test_a_read_only_review_database_refuses_the_note_with_a_message(
        data_dir, tmp_path, identity, monkeypatch, llm):
    """The same, from SQLite itself: the file became read-only after the page loaded."""
    client = _narrating_with(monkeypatch, [llm.response()], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _read_only(db)

    _click_write(at)  # asserts the script raised nothing

    errors = [e.value for e in at.error]
    assert any(e.startswith(NOT_STORED + "OperationalError\\: attempt to write a readonly database")
               for e in errors), errors
    _assert_the_note_was_not_stored(at, db, identity, picked, client)


def test_a_refused_rewrite_leaves_the_note_on_record_on_screen_and_decidable(
        data_dir, tmp_path, identity, monkeypatch, llm):
    """A rewrite the store refuses changes nothing: the stored note stays drawn, and a
    decision made now records that note, the one on screen and on record."""
    _narrating_with(monkeypatch, [llm.response()], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    store = ReviewStore(db, identity)
    first = store.save_narrative(picked, _note("what ana read"), model="claude-test")
    at.run()
    _refusing(monkeypatch, "save_narrative", sqlite3.OperationalError("database is locked"))

    _click_write(at)  # "Rewrite narrative"; asserts the script raised nothing

    assert any(e.value.startswith(NOT_STORED) for e in at.error)
    assert any("what ana read" in m.value for m in at.markdown)
    assert any(f"note #{first}" in c.value for c in at.caption)
    assert not any("two days before period close" in m.value for m in at.markdown)
    assert store.get_narrative(picked)["id"] == first
    _submit(at, "Dismiss", "routine")
    assert store.history(picked)["narrative_id"].tolist() == [first]


# Not the store's: record()'s own checks (ValueError; the form refuses a missing name before
# it builds a decision), our own code (TypeError), and the driver's faults for a value it
# cannot bind (ProgrammingError on 3.12, InterfaceError on 3.9), which are sqlite3.Errors too.
NOT_THE_STORES = [
    pytest.param(TypeError("a fault in our own code"), id="TypeError"),
    pytest.param(ValueError("a decision must name an entry"), id="ValueError"),
    pytest.param(sqlite3.ProgrammingError("Error binding parameter 2: type 'dict' is not supported"),
                 id="ProgrammingError"),
    pytest.param(sqlite3.InterfaceError("Error binding parameter 1 - probably unsupported type."),
                 id="InterfaceError"),
]


@pytest.mark.parametrize("error", NOT_THE_STORES)
def test_a_fault_in_saving_that_is_not_the_store_s_is_still_a_traceback(
        data_dir, tmp_path, identity, monkeypatch, llm, error):
    """Only the store's refusals are caught: a fault in our own code is a traceback, as a bug
    should be, and never worded as a refused save or a failed request."""
    _narrating_with(monkeypatch, [llm.response()], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _refusing(monkeypatch, "save_narrative", error)
    next(b for b in at.button if b.label == "Write narrative").click()
    at.run()

    assert at.exception and str(error) in at.exception[0].message
    assert not any("could not be stored" in e.value for e in at.error)
    assert not any("request to Claude failed" in e.value for e in at.error)
    assert not at.success
    assert ReviewStore(db, identity).get_narrative(picked) is None


@pytest.mark.parametrize("summary, shown", [
    pytest.param({"not": "text"}, "summary must be text\\, got dict", id="object"),
    pytest.param("Manual credit " + chr(0xD800),
                 "summary is not text the store can keep\\: it holds a lone surrogate",
                 id="lone-surrogate"),
])
def test_a_reply_field_that_is_not_text_is_refused_before_the_store(
        data_dir, tmp_path, identity, monkeypatch, llm, summary, shown):
    """validate() refuses a field the schema says is text and is not, and text holding a lone
    surrogate, so the reply is refused like any other that breaks the contract, and the page
    goes on. Before, the object reached the insert as a value the driver cannot bind, and the
    surrogate as text sqlite3 cannot encode: a traceback either way."""
    payload = dict(llm.good, summary=summary)
    client = _narrating_with(monkeypatch, [llm.response(payload=payload)], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)  # asserts the script raised nothing

    assert len(client.calls) == 1
    assert [e.value for e in at.error] == [shown]  # through md()
    assert ReviewStore(db, identity).get_narrative(picked) is None
    assert any("No narrative yet for this entry." in c.value for c in at.caption)
    assert not at.success
    assert not _record_button(at).disabled  # the rest of the page was drawn


NOT_RECORDED = "The decision was not recorded \\("


def _assert_not_recorded_and_the_draft_kept(at, db, identity, picked, shown):
    errors = [e.value for e in at.error]
    assert any(e == NOT_RECORDED + shown + "\\)\\. What you typed is kept\\: record it again\\."
               for e in errors), errors
    assert ReviewStore(db, identity).history(picked).empty
    assert not at.success
    assert not any("just recorded" in w.value for w in at.warning)
    assert any(PAGE_WENT_ON in m.value for m in at.markdown)  # the page went on past the form
    for _ in range(2):  # kept in this run, and on the next one
        assert at.text_area(key=f"note-{_entry_key(at)}").value == "checked the purchase order"
        assert at.radio(key=f"choice-{_entry_key(at)}").value == "escalate"
        at.run()
        assert not at.exception, at.exception
        # A flash is shown once, on the run after it is set: look on every run.
        assert not at.success


def _assert_the_retry_records_it(at, db, identity, picked):
    _click_record(at)  # the untouched draft: not refused as the decision "just recorded"
    assert not any("just recorded" in w.value for w in at.warning)
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["escalate"]
    assert history["note"].tolist() == ["checked the purchase order"]
    assert any(s.value == "Recorded: escalate by ana." for s in at.success)
    assert at.text_area(key=f"note-{_entry_key(at)}").value == ""  # cleared only now


@pytest.mark.parametrize("error, shown", STORE_REFUSALS)
def test_a_decision_the_store_refuses_is_a_message_and_a_retry_records_it(
        data_dir, tmp_path, identity, monkeypatch, error, shown):
    """Uncaught, the store's error drew Streamlit's traceback and stopped the page. Nothing
    that marks the decision done may be set when it was not recorded: the draft would be
    cleared, and the retry refused as a duplicate of a decision that does not exist."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    real = ReviewStore.record
    _refusing(monkeypatch, "record", error)

    _submit(at, "Escalate", "checked the purchase order")  # asserts the script raised nothing

    assert not any("![x](" in e.value for e in at.error)
    _assert_not_recorded_and_the_draft_kept(at, db, identity, picked, shown)
    monkeypatch.setattr(ReviewStore, "record", real)
    _assert_the_retry_records_it(at, db, identity, picked)


def test_a_read_only_review_database_refuses_a_decision_and_keeps_the_draft(
        data_dir, tmp_path, identity):
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _read_only(db)

    _submit(at, "Escalate", "checked the purchase order")  # asserts the script raised nothing

    _assert_not_recorded_and_the_draft_kept(
        at, db, identity, picked, "OperationalError\\: attempt to write a readonly database")
    db.chmod(0o644)
    _assert_the_retry_records_it(at, db, identity, picked)


def test_a_click_after_the_name_was_cleared_records_nothing_and_keeps_the_draft(
        data_dir, tmp_path, identity):
    """The button is drawn disabled without a name, but on Streamlit 1.50 a click lands when
    the name is cleared and the button clicked in one go, and record() refused the nameless
    decision as a traceback that stopped the page. Later versions drop that click."""
    import streamlit

    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    key = _entry_key(at)
    at.radio(key=f"choice-{key}").set_value("Escalate")
    at.text_area(key=f"note-{key}").set_value("checked the purchase order")
    at.text_input(key="reviewer").set_value("")
    _record_button(at).click()
    at.run()

    assert not at.exception, at.exception
    assert ReviewStore(db, identity).history(picked).empty
    assert at.text_area(key=f"note-{key}").value == "checked the purchase order"
    assert any(PAGE_WENT_ON in m.value for m in at.markdown)
    refused = any(w.value.startswith("Enter your name in the sidebar first") for w in at.warning)
    if streamlit.__version__.startswith("1.50."):
        assert refused  # the click landed, and was refused in words
    else:
        assert refused or _record_button(at).disabled


@pytest.mark.parametrize("error", NOT_THE_STORES)
def test_a_fault_in_recording_that_is_not_the_store_s_is_still_a_traceback(
        data_dir, tmp_path, identity, monkeypatch, error):
    """A traceback, never worded as the store refusing the decision."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _refusing(monkeypatch, "record", error)
    key = _entry_key(at)
    at.radio(key=f"choice-{key}").set_value("Accept")
    at.text_area(key=f"note-{key}").set_value("fine")
    _record_button(at).click()
    at.run()

    assert at.exception and str(error) in at.exception[0].message
    assert not any("was not recorded" in e.value for e in at.error)
    assert not at.success
    assert ReviewStore(db, identity).history(picked).empty


def test_a_fault_in_reading_the_reply_is_not_called_the_request_failing(
        data_dir, tmp_path, identity, monkeypatch, llm):
    """A fault in our own code after the request was answered (here, reading the reply's
    model) is a traceback, as a bug should be: the request did not fail."""
    from ledgerlens import narrate

    client = _narrating_with(monkeypatch, [llm.response()], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    def faulty(response):
        raise TypeError("a fault in our own code")

    monkeypatch.setattr(narrate, "response_model", faulty)
    next(b for b in at.button if b.label == "Write narrative").click()
    at.run()

    assert len(client.calls) == 1
    assert at.exception and "a fault in our own code" in at.exception[0].message
    assert not any("request to Claude failed" in e.value for e in at.error)
    assert ReviewStore(db, identity).get_narrative(picked) is None


def test_the_page_does_not_import_the_sdk_until_a_note_is_asked_for(data_dir, tmp_path, monkeypatch):
    """The SDK reads its own settings (ANTHROPIC_LOG) when it is first imported, and the
    page loads .env after its imports: imported on load, it would miss a setting there."""
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)
    _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    assert "anthropic" not in sys.modules


def test_without_the_sdk_the_button_says_so_and_the_page_goes_on(
        data_dir, tmp_path, identity, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import fails, as if not installed
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)  # asserts the script raised nothing

    assert any("package is required" in e.value for e in at.error)
    assert ReviewStore(db, identity).get_narrative(picked) is None
    assert not _record_button(at).disabled


def test_without_the_sdk_nothing_else_is_caught(data_dir, tmp_path, monkeypatch, llm):
    """Without the SDK there are no SDK errors to catch: any other exception from a
    request (here a client's own) is a traceback, never "the request failed"."""
    _narrating_with(monkeypatch, [ConnectionError("no route to the API")], llm)
    monkeypatch.setitem(sys.modules, "anthropic", None)
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    next(b for b in at.button if b.label == "Write narrative").click()
    at.run()

    assert at.exception and "no route to the API" in at.exception[0].message
    assert not any("request to Claude failed" in e.value for e in at.error)


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


def test_a_path_the_store_cannot_use_is_a_message_with_its_markdown_escaped(data_dir, tmp_path):
    """Two paths the store's open let through as raw errors, a traceback here: a folder that
    cannot be made, and an empty file that is read-only. And the open's message names the
    path as typed, which st.error would draw as markdown: through md(), like the rest."""
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    at = _run(data_dir, blocker / "review.sqlite", reviewer="ana")  # asserts nothing was raised
    assert len(at.error) == 1 and "cannot be made" in at.error[0].value
    assert any(c.value == KEPT for c in at.caption)
    assert not at.metric and not any("Record decision" in b.label for b in at.button)

    if not (hasattr(os, "geteuid") and os.geteuid() == 0):  # root writes to a read-only file
        empty = tmp_path / "empty.sqlite"
        empty.write_bytes(b"")
        empty.chmod(0o444)
        at = _run(data_dir, empty, reviewer="ana")
        assert len(at.error) == 1
        assert "cannot be opened as a review database" in at.error[0].value
        assert "attempt to write a readonly database" in at.error[0].value

    image = tmp_path / "![x](x.png).sqlite"
    image.write_text("not a database\n")
    at = _run(data_dir, image, reviewer="ana")
    assert len(at.error) == 1 and "is not a SQLite database" in at.error[0].value
    assert "\\!\\[x\\]\\(x\\.png\\)\\.sqlite" in at.error[0].value
    assert "![x](" not in at.error[0].value


# --- the store's reads -----------------------------------------------------------------------

# As md() shows them: it escapes every ASCII punctuation character.
STOPS_HERE = "The review database could not be read\\, so the page stops here \\("
END = "\\)\\."
KEPT = ("When the file can be used again, or another is named in the sidebar, rerun the page: "
        "Rerun, in the menu at the top right. Reloading the browser tab starts a new session and "
        "loses what was being recorded. With this file again, the entry, \"Outstanding only\" and "
        "the decision you submitted come back; a filter or a fiscal year this page no longer "
        "shows starts over.")
NO_HISTORY = "The decision history could not be read\\, so it is not shown \\("
LOCKED = "OperationalError\\: database is locked"


def _through_pandas(cause):
    """What ``pd.read_sql_query`` raises when SQLite refuses: pandas' own DatabaseError (an
    OSError, not a sqlite3.Error), with what SQLite raised as its cause."""
    wrapped = pd.errors.DatabaseError(f"Execution failed on sql 'SELECT 1': {cause}")
    wrapped.__cause__ = cause
    return wrapped


def _locked():
    """A lock that outlasted SQLite's wait, as the page's pandas reads meet it."""
    return _through_pandas(sqlite3.OperationalError("database is locked"))


# What a read can meet, raw (decided_ids, get_narrative) or through pandas (the other five),
# and what the message shows of it: the type and text SQLite gave, never pandas' wrapping.
READ_REFUSALS = [
    *STORE_REFUSALS,
    pytest.param(_through_pandas(sqlite3.OperationalError("no such table: decisions")),
                 "OperationalError\\: no such table\\: decisions", id="pandas-no-table"),
    pytest.param(_through_pandas(sqlite3.DatabaseError("file is not a database")),
                 "DatabaseError\\: file is not a database", id="pandas-not-a-database"),
]

# Where each read stops the page: what was drawn above it, by the last thing drawn there.
DRAWN_BEFORE = {
    "other_ledgers": lambda at: not at.metric,
    "current": lambda at: not at.metric,
    "summary": lambda at: any(m.label == "Decided" for m in at.metric) and not at.tabs,
    "get_narrative": lambda at: any("Journal entry lines" in m.value for m in at.markdown),
}


@pytest.mark.parametrize("read", sorted(DRAWN_BEFORE))
@pytest.mark.parametrize("error, shown", READ_REFUSALS)
def test_a_read_the_store_refuses_is_a_message_and_the_run_stops_there(
        data_dir, tmp_path, identity, monkeypatch, read, error, shown):
    """The store is opened once a run and then read at up to seven places, each on a
    connection of its own, so a file that opened can refuse the next read: locked past
    SQLite's wait, removed or replaced while a note was being requested, a disk error.
    Uncaught, that was Streamlit's traceback. Nothing below the message is drawn: it would
    be built on what could not be read."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    _refusing(monkeypatch, read, error)

    at.run()

    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [STOPS_HERE + shown + END]
    assert not any("![x](" in e.value for e in at.error)
    assert any(c.value == KEPT for c in at.caption)
    assert DRAWN_BEFORE[read](at)
    assert not any("Record decision" in b.label for b in at.button)
    assert not any(PAGE_WENT_ON in m.value for m in at.markdown)


def test_the_read_that_gates_writes_is_guarded_too(data_dir, tmp_path, identity, monkeypatch):
    """Writes wait for `adopt-legacy` on the strength of a second read of the store's
    ledgers. Refused, it ends the run: a page that went on could not say whether to wait."""
    db = tmp_path / "review.sqlite"
    _legacy_note_on_screen(data_dir, db, identity)
    at = _run(data_dir, db, reviewer="ana")
    real, calls = ReviewStore.ledgers, []

    def refused_the_second_time(self):
        calls.append(1)
        if len(calls) == 2:  # the first is other_ledgers' own
            raise _locked()
        return real(self)

    monkeypatch.setattr(ReviewStore, "ledgers", refused_the_second_time)
    at.run()

    assert len(calls) == 2
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert not at.metric and not any("Record decision" in b.label for b in at.button)


def test_the_outstanding_filter_s_read_is_guarded_too(data_dir, tmp_path, monkeypatch):
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    _refusing(monkeypatch, "decided_ids", sqlite3.OperationalError("database is locked"))

    at.checkbox(key="outstanding_only").check().run()

    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert not any("Record decision" in b.label for b in at.button)


@pytest.mark.parametrize("error, shown", READ_REFUSALS)
def test_a_history_the_store_refuses_is_a_message_in_its_place_and_the_page_goes_on(
        data_dir, tmp_path, identity, monkeypatch, error, shown):
    """The history is the last thing the queue draws and nothing is built on it, so the
    message stands where it would have been and the other tabs are still drawn."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    ReviewStore(db, identity).record(Decision(picked, "accept", "ana", "fine"))
    at.run()
    assert any("Decision history" in m.value for m in at.markdown)
    _refusing(monkeypatch, "history", error)

    at.run()

    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [NO_HISTORY + shown + END]
    assert not any("![x](" in e.value for e in at.error)
    assert not any(c.value == KEPT for c in at.caption)  # the run did not stop
    assert not any("Decision history" in m.value for m in at.markdown)
    assert any(PAGE_WENT_ON in m.value for m in at.markdown)
    assert not _record_button(at).disabled


@pytest.mark.parametrize("read", ["other_ledgers", "get_narrative"])
def test_a_file_that_is_no_database_any_more_is_said_in_sqlite_s_words(
        data_dir, tmp_path, monkeypatch, read):
    """Through SQLite itself, and through pandas for the read that goes that way: the file is
    replaced between the open and the read. pandas reports "Execution failed on sql ..."
    around what SQLite said; the page shows what SQLite said."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    real = getattr(ReviewStore, read)

    def after_the_file_was_replaced(self, *args):
        db.write_bytes(b"not a database any more\n" * 64)
        return real(self, *args)

    monkeypatch.setattr(ReviewStore, read, after_the_file_was_replaced)
    at.run()

    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [
        STOPS_HERE + "DatabaseError\\: file is not a database" + END]


def test_a_file_replaced_while_a_note_is_requested_is_two_messages_and_no_traceback(
        data_dir, tmp_path, monkeypatch, llm):
    """The case the store-writes change left open: the save is refused (a message since
    then), the page goes on, and its next read, the history, met the same file."""
    client = _narrating_with(monkeypatch, [llm.response()], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    answer = client.messages.create

    def while_the_file_is_replaced(**kwargs):
        db.write_bytes(b"not a database any more\n" * 64)
        return answer(**kwargs)

    client.messages.create = while_the_file_is_replaced
    _click_write(at)  # asserts the script raised nothing

    assert len(client.calls) == 1
    errors = [e.value for e in at.error]
    assert len(errors) == 2, errors
    assert errors[0].startswith(NOT_STORED + "DatabaseError\\: file is not a database")
    assert errors[1] == NO_HISTORY + "DatabaseError\\: file is not a database" + END
    assert any(PAGE_WENT_ON in m.value for m in at.markdown)
    assert not at.success


# Not the store's, raw or wrapped by pandas: our own code, the driver's faults for a value it
# cannot bind, and a pandas error with no SQLite refusal behind it. pandas' DatabaseError is
# an OSError, so a plain OSError must not pass for one either.
NOT_THE_STORES_READS = [
    *NOT_THE_STORES,
    pytest.param(_through_pandas(
        sqlite3.ProgrammingError("Error binding parameter 2: type 'dict' is not supported")),
        id="pandas-ProgrammingError"),
    pytest.param(_through_pandas(
        sqlite3.InterfaceError("Error binding parameter 1 - probably unsupported type.")),
        id="pandas-InterfaceError"),
    pytest.param(pd.errors.DatabaseError("Execution failed on sql 'SELECT 1': nothing behind it"),
                 id="pandas-no-cause"),
    pytest.param(_through_pandas(ValueError("not SQLite's")), id="pandas-not-sqlite"),
    pytest.param(OSError("a disk error in our own code"), id="OSError"),
]


@pytest.mark.parametrize("read", ["current", "get_narrative", "history"])
@pytest.mark.parametrize("error", NOT_THE_STORES_READS)
def test_a_fault_in_reading_that_is_not_the_store_s_is_still_a_traceback(
        data_dir, tmp_path, monkeypatch, read, error):
    """A traceback, as a bug should be, and never worded as the store refusing a read."""
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    _refusing(monkeypatch, read, error)
    at.run()

    assert at.exception and str(error) in at.exception[0].message
    assert not any("could not be read" in e.value for e in at.error)


def test_a_decision_recorded_just_before_the_store_fails_is_still_said_once(
        data_dir, tmp_path, identity, monkeypatch):
    """A recorded decision reruns the page, and the rerun says so. If that rerun's first read
    is refused, an error alone, straight after the click, reads as if the click had failed:
    the confirmation is drawn above it. And when the form comes back the recorded draft is
    cleared, not given back as a decision still being recorded."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    key = _entry_key(at)
    real, calls = ReviewStore.other_ledgers, []

    def refused_on_the_rerun(self):
        calls.append(1)
        if len(calls) == 2:
            raise _locked()
        return real(self)

    monkeypatch.setattr(ReviewStore, "other_ledgers", refused_on_the_rerun)
    _submit(at, "Escalate", "checked the purchase order")  # asserts the script raised nothing

    assert ReviewStore(db, identity).history(picked)["decision"].tolist() == ["escalate"]
    assert [s.value for s in at.success] == ["Recorded: escalate by ana."]
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    # In that order: the confirmation, the error, what to do next. Read around the error,
    # not as the page's last three: after a rerun inside one run, Streamlit 1.50's harness
    # can leave elements of the run before behind the new ones.
    kinds = [node.type for _, node in sorted(at.main.children.items())]
    at_error = kinds.index("error")
    assert kinds[at_error - 1:at_error + 2] == ["success", "error", "caption"]

    at.run()  # the store answers again
    assert not at.exception, at.exception
    assert not at.success and not at.error  # said once
    assert at.text_area(key=f"note-{key}").value == ""
    assert at.radio(key=f"choice-{key}").value is None


def _recorded_then_the_rerun_stops(data_dir, db, monkeypatch, read="other_ledgers",
                                   outstanding=False):
    """A decision is recorded, the rerun after it is refused a read, and the store answers again."""
    at = _run(data_dir, db, reviewer="ana")
    if outstanding:
        at.checkbox(key="outstanding_only").check().run()
        assert not at.exception, at.exception
    picked, key = at.selectbox(key="picked").value, _entry_key(at)
    real, calls = getattr(ReviewStore, read), []

    def refused_on_the_rerun(self, *args):
        calls.append(1)
        if len(calls) == 2:
            raise _locked()
        return real(self, *args)

    monkeypatch.setattr(ReviewStore, read, refused_on_the_rerun)
    _submit(at, "Escalate", "checked the purchase order")
    assert [s.value for s in at.success] == ["Recorded: escalate by ana."]
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    return at, picked, key


def test_a_recorded_decision_is_not_given_back_as_a_draft_later(data_dir, tmp_path, monkeypatch):
    """When the rerun after a recorded decision stopped, the decision just recorded was still
    in the form's keys and was set aside with the drafts. The run that cleared the form left
    it waiting, and one run later it was back in the form as if it were still being
    recorded. It is no draft: its form is waiting to be cleared."""
    at, _, key = _recorded_then_the_rerun_stops(data_dir, tmp_path / "review.sqlite", monkeypatch)
    at.run()  # the store answers again: the form is cleared
    assert not at.exception, at.exception
    assert at.radio(key=f"choice-{key}").value is None

    at.run()  # any later run of the page

    assert not at.exception, at.exception
    note, choice = at.text_area(key=f"note-{key}"), at.radio(key=f"choice-{key}")
    assert (note.value, choice.value) == ("", None)
    assert not note.proto.set_value and not choice.proto.set_value


def test_another_decision_for_that_entry_is_recorded_not_thrown_away(
        data_dir, tmp_path, identity, monkeypatch):
    """The same fault from the reviewer's side: the next decision submitted for that entry was
    overwritten by the one that had come back, refused as "just recorded", and lost."""
    db = tmp_path / "review.sqlite"
    at, picked, _ = _recorded_then_the_rerun_stops(data_dir, db, monkeypatch)
    at.run()
    assert not at.exception, at.exception

    _submit(at, "Dismiss", "on second thoughts this is routine")

    assert "That decision was just recorded." not in [w.value for w in at.warning]
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["escalate", "dismiss"]
    assert history["note"].tolist()[-1] == "on second thoughts this is routine"


@pytest.mark.parametrize("read", ["decided_ids", "get_narrative"])
def test_a_decision_recorded_just_before_a_stop_inside_the_tab_is_said_above_that_error(
        data_dir, tmp_path, monkeypatch, read):
    """A stop inside the first tab says its error above the tab bar, and the confirmation of
    a decision recorded on the run before goes there with it, first: drawn where the run
    stopped, it would stand below the error and show on one tab only. At the note's read it
    did: the page said "Recorded: ..." before it read the note, inside the tab. The
    confirmation waits for the note now."""
    at, _, _ = _recorded_then_the_rerun_stops(
        data_dir, tmp_path / "review.sqlite", monkeypatch, read=read,
        outstanding=read == "decided_ids")  # the read "Outstanding only" asks for
    blocks = [[child.type for _, child in sorted(getattr(node, "children", {}).items())]
              for _, node in sorted(at.main.children.items())]
    said = next(inner for inner in blocks if "error" in inner)  # the block above the tab bar
    assert [kind for kind in said if kind != "tab"] == ["success", "error", "caption"]
    assert not any(tab.success or tab.error for tab in at.tabs)


def test_a_stopped_run_forgets_the_filters_it_did_not_draw(data_dir, tmp_path, monkeypatch):
    """A session keeps the value of a control a stopped run did not reach; the browser has
    dropped the control and starts it again from its default. So the next run drew the queue
    from filters the browser no longer showed, and under Streamlit 1.50 (where the selector
    is another control once its options change) the click after that was thrown away. The
    run forgets them when it stops, and both start from the defaults. Seen from here only
    as the keys leaving the session: AppTest drops them by itself one run later."""
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    severity = next(w for w in at.multiselect if w.label == "Severity")
    amount = next(w for w in at.number_input if w.label == "Minimum amount")
    assert severity.key.startswith("filter-severity|") and len(severity.options) > 1
    severity.set_value(severity.options[:1])
    amount.set_value(5000.0)
    at.run()
    assert not at.exception, at.exception
    assert severity.key in at.session_state and amount.key in at.session_state
    _refusing(monkeypatch, "current", sqlite3.OperationalError("database is locked"))

    at.run()

    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert severity.key not in at.session_state and amount.key not in at.session_state


def test_a_stopped_run_keeps_what_it_had_drawn_and_forgets_a_fiscal_year_it_had_not(
        tmp_path, monkeypatch):
    """The other half, and the fiscal year. A filter the run drew before it stopped is still
    on the page with its value, so the session keeps it: forgotten, the next run drew the
    queue from the defaults under a filter the browser still showed narrowed. The fiscal
    year, in the sidebar, goes by the same rule."""
    folder = tmp_path / "two-years"
    main(["generate", "--start", "2024-12-01", "--end", "2025-01-31", "--out-dir", str(folder)])
    at = _run_with(folder, tmp_path / "review.sqlite", materiality="50000")
    everything = _queue_frame(at)["entry_id"].tolist()
    severity = next(w for w in at.multiselect if w.label == "Severity")
    severity.set_value(severity.options[:1])
    at.selectbox(key="materiality_year").set_value(2024)  # the latest year, 2025, is the default
    at.run()
    assert not at.exception, at.exception
    narrowed = _queue_frame(at)["entry_id"].tolist()
    assert 0 < len(narrowed) < len(everything)
    real = ReviewStore.get_narrative
    _refusing(monkeypatch, "get_narrative", sqlite3.OperationalError("database is locked"))

    at.run()  # stops inside the first tab, below the filters and the year

    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert at.session_state[severity.key] == severity.options[:1]
    assert at.session_state["materiality_year"] == 2024
    monkeypatch.setattr(ReviewStore, "get_narrative", real)
    at.run()
    assert not at.exception and not at.error
    assert _queue_frame(at)["entry_id"].tolist() == narrowed  # the queue the filter still shows
    assert at.selectbox(key="materiality_year").value == 2024
    _refusing(monkeypatch, "other_ledgers", sqlite3.OperationalError("database is locked"))

    at.run()  # stops in the sidebar, above both

    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert severity.key not in at.session_state and "materiality_year" not in at.session_state


def _assert_given_back_and_recorded_once(at, db, identity, picked, key):
    """The form shows what was being recorded, the browser is told so (an assigned key sets
    ``set_value``; a value the script merely still holds does not), once, and it records.
    The radio's own value travels in a field Streamlit renamed between 1.50 and 1.64, so
    for the radio only the flag is read."""
    note, choice = at.text_area(key=f"note-{key}"), at.radio(key=f"choice-{key}")
    assert (note.value, choice.value) == ("checked the purchase order", "escalate")
    assert note.proto.set_value and note.proto.value == "checked the purchase order"
    assert choice.proto.set_value
    at.run()
    assert not at.exception, at.exception
    assert not at.text_area(key=f"note-{key}").proto.set_value  # then the form is the reviewer's
    assert not at.radio(key=f"choice-{key}").proto.set_value
    _click_record(at)
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["escalate"]
    assert history["note"].tolist() == ["checked the purchase order"]


@pytest.mark.parametrize("read", ["other_ledgers", "get_narrative"])
def test_a_decision_being_recorded_when_a_read_fails_is_given_back_to_the_form(
        data_dir, tmp_path, identity, monkeypatch, read):
    """A run that stops before the form takes the form off the page, and a browser forgets
    what was in a form it no longer shows: once the store answered again the form came back
    empty (seen in a browser on Streamlit 1.50 and 1.64, on the open's own stop). What was
    being recorded is set aside when the run stops and assigned back to the form."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    key = _entry_key(at)
    real = getattr(ReviewStore, read)
    _refusing(monkeypatch, read, sqlite3.OperationalError("database is locked"))

    _submit(at, "Escalate", "checked the purchase order")  # asserts the script raised nothing

    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert not any("Record decision" in b.label for b in at.button)
    at.run()  # still refused: a second stopped run must not empty what the first set aside
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    monkeypatch.setattr(ReviewStore, read, real)
    assert ReviewStore(db, identity).history(picked).empty  # no write followed the failed read

    at.run()
    assert not at.exception, at.exception
    _assert_given_back_and_recorded_once(at, db, identity, picked, key)


def test_a_decision_being_recorded_when_the_file_cannot_be_opened_is_given_back_too(
        data_dir, tmp_path, identity):
    """The same through SQLite itself, and for the open's stop, which was there before: the
    file is unreadable at the click and readable again afterwards."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads a file with no permissions")
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    key = _entry_key(at)
    db.chmod(0o000)

    _submit(at, "Escalate", "checked the purchase order")  # asserts the script raised nothing

    assert len(at.error) == 1 and "cannot be opened as a SQLite database" in at.error[0].value
    assert not any("Record decision" in b.label for b in at.button)
    db.chmod(0o644)
    assert ReviewStore(db, identity).history(picked).empty

    at.run()
    assert not at.exception, at.exception
    _assert_given_back_and_recorded_once(at, db, identity, picked, key)


def test_a_recorded_decision_s_draft_is_cleared_for_the_browser_too(data_dir, tmp_path):
    """The page removed the draft's keys once a decision was recorded. That resets the value
    for the script and tells the browser nothing: on Streamlit 1.50 and 1.64 a browser went
    on showing the choice and the note just recorded, and sent them back with the next
    submit. AppTest has no browser, so its values read as cleared either way; what a browser
    is told is ``set_value`` on the widget, and only an assigned key sets it."""
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    key = _entry_key(at)

    _submit(at, "Escalate", "checked the purchase order")

    assert any(s.value == "Recorded: escalate by ana." for s in at.success)
    note, choice = at.text_area(key=f"note-{key}"), at.radio(key=f"choice-{key}")
    assert (note.value, choice.value) == ("", None)
    assert note.proto.set_value and note.proto.value == ""
    assert choice.proto.set_value
    at.run()  # on that run only: the form is not reset under a reviewer who is typing
    assert not at.exception, at.exception
    assert not at.text_area(key=f"note-{key}").proto.set_value
    assert not at.radio(key=f"choice-{key}").proto.set_value


@pytest.mark.parametrize("outstanding", [False, True])
def test_a_decision_being_recorded_on_another_entry_comes_back_with_its_entry(
        data_dir, tmp_path, identity, monkeypatch, outstanding):
    """A run that stops before the queue takes the entry selector and "Outstanding only" off
    the page with the form, and a browser starts a control it is shown again from its
    default: the selector on the queue's first entry, the box unticked. Given back to the
    form alone, the decision came back only for a reviewer who was on the first entry. On
    any other the page returned to the first with an empty form, or (the session still
    holding the entry the browser had let go) drew the right form and threw the next click
    away. The two controls are set aside with the draft and assigned back, so the browser is
    told which entry it is. AppTest forgets more than a session does after a stopped run
    (it drops what it did not send, where a session keeps it), so what is read here is what
    the browser is told: the value and ``set_value``. Checked in a browser as well."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    first, _, third = at.selectbox(key="picked").options[:3]
    if outstanding:
        at.checkbox(key="outstanding_only").check().run()
    at.selectbox(key="picked").set_value(third).run()
    assert not at.exception, at.exception
    key = _entry_key(at)
    real = ReviewStore.other_ledgers
    _refusing(monkeypatch, "other_ledgers", sqlite3.OperationalError("database is locked"))

    _submit(at, "Escalate", "checked the purchase order")  # asserts the script raised nothing

    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert not at.selectbox and not at.checkbox  # off the page with the form
    monkeypatch.setattr(ReviewStore, "other_ledgers", real)
    at.run()
    assert not at.exception, at.exception
    entry, box = at.selectbox(key="picked"), at.checkbox(key="outstanding_only")
    assert entry.value == third and entry.proto.set_value
    assert box.value is outstanding and box.proto.set_value
    assert _entry_key(at) == key
    _assert_given_back_and_recorded_once(at, db, identity, third, key)
    assert ReviewStore(db, identity).history(first).empty


def _stopped_on_the_third_entry(data_dir, db, monkeypatch):
    """A decision submitted on the queue's third entry with "Outstanding only" ticked, on a
    run that stops before it draws either control; then the store answers again."""
    at = _run(data_dir, db, reviewer="ana")
    first, _, third = at.selectbox(key="picked").options[:3]
    at.checkbox(key="outstanding_only").check().run()
    at.selectbox(key="picked").set_value(third).run()
    assert not at.exception, at.exception
    real = ReviewStore.other_ledgers
    _refusing(monkeypatch, "other_ledgers", sqlite3.OperationalError("database is locked"))
    _submit(at, "Escalate", "checked the purchase order")
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    monkeypatch.setattr(ReviewStore, "other_ledgers", real)
    return at, first, third


def test_an_entry_that_left_the_queue_meanwhile_is_let_go(data_dir, tmp_path, identity, monkeypatch):
    """What was set aside is given back only where it still fits. An entry decided elsewhere
    while the page was stopped is no longer in an "Outstanding only" queue: the selector
    starts on the queue's first entry, as it would have without the stop, and nothing is
    assigned to it (given a value that is no option, Streamlit 1.64 falls back to the first
    entry by itself and still tells the browser it was given one; 1.50 takes the value).
    And it is let go for good: when the entry is in the queue again, the reviewer is not
    moved to it."""
    db = tmp_path / "review.sqlite"
    at, first, third = _stopped_on_the_third_entry(data_dir, db, monkeypatch)
    ReviewStore(db, identity).record(Decision(third, "dismiss", "ben", "seen elsewhere"))

    at.run()

    assert not at.exception, at.exception
    entry = at.selectbox(key="picked")
    assert entry.value == first and third not in entry.options
    assert not entry.proto.set_value
    assert at.checkbox(key="outstanding_only").value is True
    assert ReviewStore(db, identity).history(third)["decision"].tolist() == ["dismiss"]

    at.checkbox(key="outstanding_only").uncheck().run()  # the entry is in the queue again

    assert not at.exception, at.exception
    entry = at.selectbox(key="picked")
    assert third in entry.options and entry.value == first and not entry.proto.set_value


def test_the_selector_and_the_box_are_the_reviewer_s_again_once_given_back(
        data_dir, tmp_path, monkeypatch):
    """Given back once, as the form's two widgets are. Assigned again on a later run, the
    selector pulled a reviewer who had picked another entry back to the old one, and the
    box ticked itself again."""
    at, first, third = _stopped_on_the_third_entry(data_dir, tmp_path / "review.sqlite", monkeypatch)
    at.run()
    assert not at.exception, at.exception
    entry, box = at.selectbox(key="picked"), at.checkbox(key="outstanding_only")
    assert entry.value == third and entry.proto.set_value
    assert box.value is True and box.proto.set_value

    at.run()  # a later run tells the browser nothing about either

    assert not at.exception, at.exception
    assert not at.selectbox(key="picked").proto.set_value
    assert not at.checkbox(key="outstanding_only").proto.set_value
    at.selectbox(key="picked").set_value(first).run()
    assert not at.exception, at.exception
    assert at.selectbox(key="picked").value == first
    at.checkbox(key="outstanding_only").uncheck().run()
    assert not at.exception, at.exception
    assert at.checkbox(key="outstanding_only").value is False
    assert at.selectbox(key="picked").value == first


def test_a_box_unticked_at_a_stop_that_had_drawn_it_stays_unticked(data_dir, tmp_path, monkeypatch):
    """Only what the stopped run had not drawn is set aside. "Outstanding only" is drawn
    before the read it asks for, so at that read's stop it is still on the page, and
    unticking it is the way out. Set aside all the same, it was ticked again by the next
    run, which made the refused read again and stopped again."""
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    _refusing(monkeypatch, "decided_ids", sqlite3.OperationalError("database is locked"))
    at.checkbox(key="outstanding_only").check().run()
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]

    at.checkbox(key="outstanding_only").uncheck().run()

    assert not at.exception, at.exception
    box = at.checkbox(key="outstanding_only")
    assert box.value is False and not box.proto.set_value and not at.error


def test_the_next_entry_s_form_is_not_cleared_for_the_one_just_recorded(data_dir, tmp_path):
    """The clear is for the recorded entry's own form. With "Outstanding only" ticked that
    entry leaves the queue, and the form drawn on the next run is another entry's: it is
    left alone. Cleared too, a draft waiting for that entry was held back a run, and a
    click on that run was refused as a duplicate."""
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    at.checkbox(key="outstanding_only").check().run()
    first, second = at.selectbox(key="picked").options[:2]

    _submit(at, "Escalate", "checked the purchase order")

    assert any(s.value == "Recorded: escalate by ana." for s in at.success)
    entry = at.selectbox(key="picked")
    assert first not in entry.options and entry.value == second
    key = _entry_key(at)
    assert not at.radio(key=f"choice-{key}").proto.set_value
    assert not at.text_area(key=f"note-{key}").proto.set_value


def test_a_draft_set_aside_waits_for_its_own_entry_s_form(data_dir, tmp_path, identity, monkeypatch):
    """Whichever form was drawn next took everything that had been set aside, so a look at
    another entry on the way back threw the waiting draft away."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    other, mine = at.selectbox(key="picked").options[:2]
    at.selectbox(key="picked").set_value(mine).run()
    key = _entry_key(at)
    real = ReviewStore.get_narrative
    _refusing(monkeypatch, "get_narrative", sqlite3.OperationalError("database is locked"))
    _submit(at, "Escalate", "checked the purchase order")
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    monkeypatch.setattr(ReviewStore, "get_narrative", real)

    at.selectbox(key="picked").set_value(other).run()  # the selector was drawn before that stop
    assert not at.exception, at.exception
    other_key = _entry_key(at)
    assert other_key != key
    assert at.radio(key=f"choice-{other_key}").value is None
    assert not at.text_area(key=f"note-{other_key}").value

    at.selectbox(key="picked").set_value(mine).run()
    assert not at.exception, at.exception
    _assert_given_back_and_recorded_once(at, db, identity, mine, key)
    assert ReviewStore(db, identity).history(other).empty


NO_CHOICE = "Choose a decision first: nothing was recorded, and what you typed is kept."


def test_a_form_with_no_decision_chosen_records_nothing(data_dir, tmp_path, identity):
    """The form opened on "accept", so a click on a form nobody had filled in recorded an
    accept. No decision is chosen until the reviewer chooses one."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    key = _entry_key(at)
    assert at.radio(key=f"choice-{key}").value is None
    at.text_area(key=f"note-{key}").set_value("checked the purchase order")

    _click_record(at)

    said = [w.value for w in at.warning]
    assert NO_CHOICE in said and "That decision was just recorded." not in said
    assert ReviewStore(db, identity).history(picked).empty and not at.success
    assert at.text_area(key=f"note-{key}").value == "checked the purchase order"


def test_a_second_click_after_a_decision_was_recorded_records_nothing(data_dir, tmp_path, identity):
    """Once the cleared form reached the browser as "accept" with an empty note, a second
    click on Record decision recorded exactly that, as the entry's latest decision: an
    escalation became an accept the reviewer never chose. (While the browser still showed
    the decision just recorded, the fault this change fixes, the second click sent the same
    values and the double-click guard refused it.)"""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _submit(at, "Escalate", "checked the purchase order")
    assert any(s.value == "Recorded: escalate by ana." for s in at.success)

    _click_record(at)  # the form as it now stands: no choice, no note

    said = [w.value for w in at.warning]
    assert NO_CHOICE in said and "That decision was just recorded." not in said
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["escalate"] and not at.success


def test_a_click_that_arrives_while_the_form_is_being_cleared_records_nothing(
        data_dir, tmp_path, identity):
    """A second click can reach the script before the rerun that clears the form has drawn
    it: it carries the decision just recorded, and the run it starts is the one that
    clears. The cleared values were read as a new decision ("accept", no note) and recorded.
    That click is the duplicate, whatever the form holds by then."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    key = _entry_key(at)
    _submit(at, "Escalate", "checked the purchase order")
    # The second click: the browser's own values, on a run that still has the clear to do.
    at.session_state["clear-decision"] = key
    at.radio(key=f"choice-{key}").set_value("Escalate")
    at.text_area(key=f"note-{key}").set_value("checked the purchase order")

    _click_record(at)

    said = [w.value for w in at.warning]
    assert "That decision was just recorded." in said and NO_CHOICE not in said
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["escalate"] and not at.success
    assert at.radio(key=f"choice-{key}").value is None  # and the form is cleared


def test_the_same_decision_sent_again_is_refused_as_just_recorded(data_dir, tmp_path, identity):
    """The guard's other half: the same choice and note for the same entry, on a run that is
    not the clearing one. A rerun cut short before it redrew the form leaves the browser
    holding the decision just recorded, and a click then sends exactly that."""
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value
    _submit(at, "Escalate", "checked the purchase order")

    _submit(at, "Escalate", "checked the purchase order")

    said = [w.value for w in at.warning]
    assert "That decision was just recorded." in said and NO_CHOICE not in said
    history = ReviewStore(db, identity).history(picked)
    assert history["decision"].tolist() == ["escalate"] and not at.success


@pytest.mark.parametrize("read", ["decided_ids", "get_narrative"])
def test_a_refusal_inside_the_queue_tab_is_said_above_the_tab_bar(
        data_dir, tmp_path, monkeypatch, read):
    """Two reads sit inside the first tab. Said there, their refusal left the other three
    tabs blank with no word for a reviewer who had one of them open."""
    at = _run(data_dir, tmp_path / "review.sqlite", reviewer="ana")
    _refusing(monkeypatch, read, sqlite3.OperationalError("database is locked"))

    at.checkbox(key="outstanding_only").check().run()  # decided_ids is read only then

    assert not at.exception, at.exception
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    assert at.tabs and not any(tab.error or tab.caption for tab in at.tabs)
    # Above it: the block that holds the message is drawn before the one that holds the tabs.
    blocks = [[child.type for _, child in sorted(getattr(node, "children", {}).items())]
              for _, node in sorted(at.main.children.items())]
    assert next(i for i, inner in enumerate(blocks) if "error" in inner) < next(
        i for i, inner in enumerate(blocks) if "tab" in inner)


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


def test_starting_from_scratch_is_still_the_choice_after_a_stopped_run(
        data_dir, tmp_path, identity, monkeypatch):
    """The box is drawn after the first reads, so a run that stops before it takes it off
    the page and the browser shows it unticked again, while the script still held the tick:
    the next click was thrown away with the button disabled. The tick is given back."""
    db = tmp_path / "review.sqlite"
    _legacy_note_on_screen(data_dir, db, identity)
    at = _run(data_dir, db, reviewer="ana")
    _start_fresh_box(at).check()
    at.run()
    assert not _record_button(at).disabled
    real = ReviewStore.other_ledgers
    _refusing(monkeypatch, "other_ledgers", sqlite3.OperationalError("database is locked"))
    at.run()
    assert [e.value for e in at.error] == [STOPS_HERE + LOCKED + END]
    monkeypatch.setattr(ReviewStore, "other_ledgers", real)

    at.run()

    assert not at.exception, at.exception
    box = _start_fresh_box(at)
    assert box.value is True and box.proto.set_value
    assert not _record_button(at).disabled


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
    assert at.radio(key=f"choice-{other_key}").value is None  # the form starts over
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
    assert at.error and any("ledger.identity.json" in t.value for t in at.text)
    assert not (tmp_path / "review.sqlite").exists()  # stopped before any store was opened


def _with_cell(data_dir, tmp_path, column, value):
    import csv

    bad = tmp_path / "bad"
    bad.mkdir()
    with (data_dir / "ledger.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    rows[2][rows[0].index(column)] = value
    with (bad / "ledger.csv").open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return bad


def test_a_blank_account_code_is_a_message_not_an_exception_page(data_dir, tmp_path):
    """The rules raised a TypeError over a blank account code: the dashboard showed Streamlit's
    exception page. The load now refuses the cell, and the page says which one."""
    bad = _with_cell(data_dir, tmp_path, "account_code", "")
    at = _run(bad, tmp_path / "review.sqlite", reviewer="ana")  # _run asserts nothing was raised
    assert at.error
    assert any("account_code is blank on 1 row, the first is data row 2" in t.value for t in at.text)
    assert not (tmp_path / "review.sqlite").exists()


def test_a_cell_quoted_in_the_message_is_drawn_as_text(data_dir, tmp_path):
    """The message now quotes the cell it could not read: as markdown, an image in it would
    be fetched the moment the page loaded."""
    bad = _with_cell(data_dir, tmp_path, "debit", "![x](http://example.invalid/x.png)")
    at = _run(bad, tmp_path / "review.sqlite", reviewer="ana")
    assert any("example.invalid" in t.value for t in at.text)
    rendered = [e.value for e in at.error] + [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert not any("example.invalid" in value for value in rendered), rendered


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
    picked = at.selectbox(key="picked").value
    assert not any("note #" in c.value for c in at.caption)
    # A note filed under the realm is this ledger's note on screen; one under the CSV's
    # digest (what the file would be keyed by without its sidecar) would not be.
    note = {"summary": "s", "why_flagged": "w", "evidence_to_request": ["e"],
            "suggested_control": "c", "confidence": "Low"}
    note_id = ReviewStore(db, "qbo:4620816365").save_narrative(picked, note, model="m")
    at = _run(pulled, db, reviewer="ana")
    assert any(f"note #{note_id}" in c.value for c in at.caption)


def test_every_measured_number_sits_beside_its_caveat(data_dir, tmp_path):
    """The quality tab and the tier tab both quote numbers measured against the labels."""
    from ledgerlens import evaluate

    at = _run(data_dir, tmp_path / "review.sqlite")
    assert "Precision" in [m.label for m in at.metric]
    assert any(w.value == evaluate.DETECTION_CAVEAT for w in at.warning)
    # the tier tab's segment table is the rule tier's figures too: its caveat leads there
    captions = [c.value for c in at.caption]
    assert captions.index(evaluate.DETECTION_CAVEAT) < captions.index(evaluate.MODEL_TIER_CAVEAT)


def test_the_tabs_say_which_test_caught_what_and_what_the_budget_holds(data_dir, tmp_path):
    at = _run(data_dir, tmp_path / "review.sqlite")
    headings = [m.value for m in at.markdown]
    assert "**Caught by the test written for each archetype, or by others**" in headings
    assert "**Each archetype against the model's budget**" in headings
    columns = [list(frame.value.columns) for frame in at.dataframe]
    assert ["anomaly_type", "intended_test", "n", "caught", "by_intended_test", "by_other_tests",
            "only_by_other_tests", "missed"] in columns
    assert ["anomaly_type", "n", "rule_caught", "model_in_budget", "chance", "model_recall"] in columns


def test_a_ratio_over_nothing_reads_undefined_on_the_quality_tab(data_dir, tmp_path):
    """Labels that mark no anomaly gave Recall 0.000 and F1 0.000: figures that are not figures."""
    folder = tmp_path / "ledger"
    folder.mkdir()
    shutil.copy(data_dir / "ledger.csv", folder / "ledger.csv")
    (folder / "labels.csv").write_text(re.sub(
        r"(?m)^([^,\n]+),True,[^\n]*$", r"\1,False,", (data_dir / "labels.csv").read_text()))
    at = _run(folder, tmp_path / "review.sqlite")
    shown = {m.label: m.value for m in at.metric}
    assert re.fullmatch(r"0\.\d{3}", shown["Precision"])  # something was flagged: a figure
    assert (shown["Recall"], shown["F1"]) == ("undefined", "undefined")


def test_labels_written_for_another_ledger_are_set_aside_not_scored(data_dir, tmp_path):
    other = tmp_path / "other"
    main(["generate", "--start", "2024-01-01", "--end", "2024-02-29", "--out-dir", str(other)])
    (other / "labels.csv").write_text((data_dir / "labels.csv").read_text())  # four months' labels
    at = _run(other, tmp_path / "review.sqlite")
    assert any("Labels set aside" in w.value for w in at.warning)
    assert any("name no ledger entry" in t.value for t in at.text)
    assert "Precision" not in [m.label for m in at.metric]
    assert any("Provide a labels CSV" in i.value for i in at.info)


def test_the_reason_labels_were_set_aside_is_shown_as_text_never_as_markdown(data_dir, tmp_path):
    """The reason quotes an id, and whoever wrote the ledger chose the id: as markdown it was an
    image the browser fetched on load and a link under any words they liked."""
    client = tmp_path / "client"
    client.mkdir()
    ledger = (data_dir / "ledger.csv").read_text()
    first_id = re.search(r"JE-\d{4}-\d{6}", ledger).group(0)
    hostile = "![](http://127.0.0.1:9/beacon.png)[Review complete](http://127.0.0.1:9/login)"
    (client / "ledger.csv").write_text(ledger.replace(first_id, hostile))
    (client / "labels.csv").write_text((data_dir / "labels.csv").read_text())
    at = _run(client, tmp_path / "review.sqlite")
    assert any("Labels set aside" in w.value for w in at.warning)
    assert not any("![](" in w.value for w in at.warning)
    assert any("![](http://127.0.0.1:9/beacon.png)" in t.value for t in at.text)


@pytest.mark.parametrize("labels_text, said", [
    ("entry_id,anomaly_type\nJE-2024-000001,\n", "missing column 'is_anomaly'"),
    ("", "No columns to parse"),
    ("entry_id,is_anomaly,anomaly_type\nJE-2024-000001,maybe,\n", "is_anomaly must be true/false"),
])
def test_a_label_file_that_cannot_be_used_is_set_aside_with_its_reason(
        data_dir, tmp_path, labels_text, said):
    import shutil

    folder = tmp_path / "ledger"
    folder.mkdir()
    shutil.copy(data_dir / "ledger.csv", folder / "ledger.csv")
    (folder / "labels.csv").write_text(labels_text)
    at = _run(folder, tmp_path / "review.sqlite")  # _run asserts nothing was raised
    assert any("Labels set aside" in w.value for w in at.warning)
    assert any(said in t.value for t in at.text)
    assert "Precision" not in [m.label for m in at.metric]


def test_flag_reasons_render_ledger_text_as_text_never_as_markdown(data_dir, tmp_path):
    """Reasons quote who keyed an entry, an account's name, another entry's id, and whoever wrote
    the ledger chose them. As markdown, a created_by was an image the browser fetched when the
    entry was shown and a link under any words they liked. A reason is now a code span, where
    nothing acts or is redrawn; the note beside it is prose built from the same cells, escaped
    so that no construct forms (Streamlit still links a bare URL in prose: app.md says so)."""
    import pandas as pd

    from ledgerlens import jets
    from ledgerlens.ingest import load_csv

    client = tmp_path / "client"
    client.mkdir()
    hostile = ("![](http://127.0.0.1:9/beacon.png)[Review complete](http://127.0.0.1:9/login) "
               "# not a heading ana@corp.example")
    raw = pd.read_csv(data_dir / "ledger.csv", dtype=str, keep_default_na=False)
    raw["created_by"] = hostile
    raw.to_csv(client / "ledger.csv", index=False)
    (client / "labels.csv").write_text((data_dir / "labels.csv").read_text())
    flags = jets.run_all(load_csv(client / "ledger.csv"))
    picked = flags[flags["reason"].str.contains("beacon.png", regex=False)]["entry_id"].iloc[0]
    db = tmp_path / "review.sqlite"
    ReviewStore(db, _identity(client)).save_narrative(picked, {
        "summary": f"Keyed by {hostile}. Debits $7,428.45 and credits $7,284.88; {string.punctuation}",
        "why_flagged": "w", "evidence_to_request": ["e"], "suggested_control": "c",
        "confidence": "low"}, model="m")
    at = _run(client, db)
    at.selectbox(key="picked").select(picked).run()
    assert not at.exception, at.exception
    shown = [m.value for m in at.markdown if "beacon" in m.value]
    reasons = [v for v in shown if v.startswith("- `JET-")]
    notes = [v for v in shown if v not in reasons]
    assert reasons and notes
    for value in reasons:  # one code span, as the summary's markdown writes every ledger string
        reason = value.split(") - ", 1)[1]
        assert hostile in reason and reason.startswith("`") and reason.endswith("`")
        assert "`" not in hostile  # so the span above is the only fence on the line's right half
        assert chr(0x200B) not in reason  # the GitHub-body measure of md_code is not planted here
    for value in notes:  # escaped prose: every ASCII punctuation character, "$" among them
        assert "![](" not in value and "](http" not in value
        assert r"\!\[\]\(http" in value and r"\# not a heading" in value
        assert r"\$7\,428\.45" in value and "".join("\\" + c for c in string.punctuation) in value


def test_a_ledger_the_model_cannot_be_fitted_on_is_a_message_not_an_exception_page(data_dir, tmp_path):
    import pandas as pd

    folder = tmp_path / "one"
    folder.mkdir()
    raw = pd.read_csv(data_dir / "ledger.csv", dtype=str, keep_default_na=False)
    raw[raw["entry_id"] == raw["entry_id"].iloc[0]].to_csv(folder / "ledger.csv", index=False)
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state["ledger_path"] = str(folder / "ledger.csv")
    at.session_state["labels_path"] = ""
    at.session_state["db_path"] = str(tmp_path / "review.sqlite")
    at.session_state["reviewer"] = ""
    at.run()
    assert not at.exception, at.exception
    assert at.error and any("cannot be fitted on 1 entry" in t.value for t in at.text)
    assert "Entries" not in [m.label for m in at.metric]


def test_a_sidecar_written_after_the_first_load_is_read_on_the_next_run(data_dir, tmp_path):
    """load() reads three files; the cache key named two. A QuickBooks pull's sidecar landing
    a moment after its CSV, or one corrected by hand, left the page keyed to the old identity."""
    side = tmp_path / "side"
    side.mkdir()
    shutil.copy(data_dir / "ledger.csv", side / "ledger.csv")
    at = _run(side, tmp_path / "review.sqlite")
    assert any("keyed by `csv:" in c.value for c in at.caption)
    (side / "ledger.identity.json").write_text('{"ledger_id": "qbo:9130350000"}')
    at.run()
    assert not at.exception, at.exception
    assert any("keyed by `qbo:9130350000`" in c.value for c in at.caption)


def _identity(folder):
    from ledgerlens.ingest import ledger_identity, load_csv

    return ledger_identity(load_csv(folder / "ledger.csv"), folder / "ledger.csv")


def test_a_label_file_corrected_on_disk_is_read_again(data_dir, tmp_path):
    """load() is cached by path: a mismatch it caught stayed on screen after the file was fixed,
    and an unreadable file's reason would have too. The key says what the file is as well."""
    folder = tmp_path / "ledger"
    folder.mkdir()
    shutil.copy(data_dir / "ledger.csv", folder / "ledger.csv")
    good = (data_dir / "labels.csv").read_text()
    (folder / "labels.csv").write_text("".join(good.splitlines(keepends=True)[:-1]))  # one unlabelled
    at = _run(folder, tmp_path / "review.sqlite")
    assert any("Labels set aside" in w.value for w in at.warning)
    (folder / "labels.csv").write_text(good)
    at.run()
    assert not at.exception, at.exception
    assert not any("Labels set aside" in w.value for w in at.warning)
    assert "Precision" in [m.label for m in at.metric]


def test_an_unreadable_label_file_is_set_aside_with_its_reason(data_dir, tmp_path):
    """The OSError half of the handler: a file that is there and cannot be read."""
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    folder = tmp_path / "ledger"
    folder.mkdir()
    shutil.copy(data_dir / "ledger.csv", folder / "ledger.csv")
    shutil.copy(data_dir / "labels.csv", folder / "labels.csv")
    (folder / "labels.csv").chmod(0o000)
    try:
        at = _run(folder, tmp_path / "review.sqlite")
    finally:
        (folder / "labels.csv").chmod(0o644)
    assert any("Labels set aside" in w.value for w in at.warning)
    assert any("Permission denied" in t.value for t in at.text)
    assert "Precision" not in [m.label for m in at.metric]


def test_a_labels_path_the_filesystem_rejects_is_set_aside_and_a_pasted_space_is_forgiven(
        data_dir, tmp_path):
    """A name too long raised from the is_file() outside the handler and took the page down; a
    path pasted with a trailing space was silently "no labels"."""
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state["ledger_path"] = str(data_dir / "ledger.csv")
    at.session_state["labels_path"] = str(tmp_path / ("x" * 300))
    at.session_state["db_path"] = str(tmp_path / "review.sqlite")
    at.session_state["reviewer"] = ""
    at.run()
    assert not at.exception, at.exception
    assert any("Labels set aside" in w.value for w in at.warning)
    assert any("too long" in t.value for t in at.text)
    at.text_input(key="labels_path").set_value(str(data_dir / "labels.csv") + "  ").run()
    assert not at.exception, at.exception
    assert not any("Labels set aside" in w.value for w in at.warning)
    assert "Precision" in [m.label for m in at.metric]


@pytest.mark.parametrize("labels_path", ["", "   ", "a-directory"])
def test_the_labels_field_is_optional_blank_or_a_directory_is_no_labels(data_dir, tmp_path, labels_path):
    (tmp_path / "a-directory").mkdir()
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state["ledger_path"] = str(data_dir / "ledger.csv")
    at.session_state["labels_path"] = str(tmp_path / labels_path) if labels_path.strip() else labels_path
    at.session_state["db_path"] = str(tmp_path / "review.sqlite")
    at.session_state["reviewer"] = ""
    at.run()
    assert not at.exception, at.exception
    assert "Entries" in [m.label for m in at.metric]
    assert not any("Labels set aside" in w.value for w in at.warning)


def test_a_ledger_path_that_cannot_be_read_is_a_message_not_an_exception_page(tmp_path):
    """A directory exists, so it passed the "no ledger found" check, and reading it raised an
    OSError the page did not catch."""
    folder = tmp_path / "folder"
    (folder / "ledger.csv").mkdir(parents=True)
    at = _run(folder, tmp_path / "review.sqlite", reviewer="ana")
    assert at.error and any("ledger.csv" in t.value for t in at.text)


def test_a_controls_file_that_cannot_be_used_is_a_message_not_the_defaults(data_dir, tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    shutil.copy(data_dir / "ledger.csv", bad / "ledger.csv")
    (bad / "ledger.controls.json").write_text('{"version": 1, "manual_entry_approvers": [], "approval_limit": 10000}')
    at = _run(bad, tmp_path / "review.sqlite", reviewer="ana")  # _run asserts nothing was raised
    assert [e.value for e in at.error] == ["The controls file beside this ledger cannot be used."]
    assert any("ledger.controls.json: manual_entry_approvers is empty" in t.value for t in at.text)
    assert not (tmp_path / "review.sqlite").exists()  # stopped before any store was opened


def test_a_controls_file_written_after_the_first_load_is_read_on_the_next_run(data_dir, tmp_path):
    """load() is cached by what its files are; the controls file is one of them."""
    import json

    from ledgerlens import jets
    from ledgerlens.ingest import load_csv

    side = tmp_path / "side"
    side.mkdir()
    shutil.copy(data_dir / "ledger.csv", side / "ledger.csv")
    at = _run(side, tmp_path / "review.sqlite")
    raised = next(m.value for m in at.metric if m.label == "Flags raised")
    df = load_csv(side / "ledger.csv")
    jet12 = jets.run_all(df, only=["JET-12"])
    assert len(jet12) and str(len(jets.run_all(df))) == str(raised)
    users = sorted(set(df.loc[df["entry_id"].isin(jet12["entry_id"]), "created_by"]))
    (side / "ledger.controls.json").write_text(json.dumps(
        {"version": 1, "manual_entry_approvers": ["controller", "mchen", *users], "approval_limit": 10000}))
    at.run()
    assert not at.exception, at.exception
    assert next(m.value for m in at.metric if m.label == "Flags raised") == str(len(jets.run_all(df)) - len(jet12))


def test_the_sidebar_names_the_controls_and_draws_a_name_from_the_file_as_code(data_dir, tmp_path):
    import json

    from ledgerlens.summary import code_span

    at = _run(data_dir, tmp_path / "review.sqlite")
    # the limit's "$" and "," escaped (app.md), so no pair of dollars can start maths
    assert ("Controls: approvers `controller`, `mchen` (JET-12) \u00b7 approval limit \\$10\\,000 "
            "(JET-06) \u00b7 the synthetic company's (no controls file beside the ledger)") in [
        c.value for c in at.sidebar.caption]
    own = tmp_path / "own"
    own.mkdir()
    shutil.copy(data_dir / "ledger.csv", own / "ledger.csv")
    name = "[x](http://example.invalid/beacon.png)"
    (own / "ledger.controls.json").write_text(json.dumps(
        {"version": 1, "manual_entry_approvers": ["controller", name], "approval_limit": 2500.5}))
    at = _run(own, tmp_path / "review.sqlite")
    caption = next(c.value for c in at.sidebar.caption if c.value.startswith("Controls:"))
    assert code_span(name) in caption and code_span(str(own / "ledger.controls.json")) in caption
    assert "\\$2\\,500\\.50" in caption


def test_a_link_to_nothing_written_after_the_first_load_is_refused_on_the_next_run(data_dir, tmp_path):
    """No file and a link to nothing both had no stamp, so the cached defaults stood while every
    other surface refused the file."""
    side = tmp_path / "side"
    side.mkdir()
    shutil.copy(data_dir / "ledger.csv", side / "ledger.csv")
    at = _run(side, tmp_path / "review.sqlite")
    assert not at.error
    os.symlink(side / "gone.json", side / "ledger.controls.json")
    at.run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == ["The controls file beside this ledger cannot be used."]
    assert any("a link to a file that does not exist" in t.value for t in at.text)



def test_a_link_loop_written_after_the_first_load_is_refused_on_the_next_run(data_dir, tmp_path):
    side = tmp_path / "side"
    side.mkdir()
    shutil.copy(data_dir / "ledger.csv", side / "ledger.csv")
    at = _run(side, tmp_path / "review.sqlite")
    assert not at.error
    os.symlink("ledger.controls.json", side / "ledger.controls.json")  # points at itself
    at.run()
    assert not at.exception, at.exception
    assert [e.value for e in at.error] == ["The controls file beside this ledger cannot be used."]


def _run_with(data_dir, db, **state):
    at = AppTest.from_file(str(APP), default_timeout=300)
    at.session_state["ledger_path"] = str(data_dir / "ledger.csv")
    at.session_state["labels_path"] = str(data_dir / "labels.csv")
    at.session_state["db_path"] = str(db)
    at.session_state["reviewer"] = ""
    for key, value in state.items():
        at.session_state[key] = value
    at.run()
    assert not at.exception, at.exception
    return at


def _queue_frame(at):
    return next(f.value for f in at.dataframe
                if {"entry_id", "risk_score", "decision"} <= set(f.value.columns))


def test_a_materiality_in_the_sidebar_puts_each_entry_s_band_beside_its_scores(data_dir, tmp_path):
    from ledgerlens.materiality import BANDS, Materiality, band_of_cents, to_cents

    at = _run_with(data_dir, tmp_path / "review.sqlite", materiality="50000")
    queue = _queue_frame(at)
    columns = list(queue.columns)
    assert columns.index("materiality_band") == columns.index("entry_amount") + 1
    assert set(queue["materiality_band"]) <= set(BANDS)
    m = Materiality("50000")
    for amount, band in zip(queue["entry_amount"], queue["materiality_band"]):
        assert band == band_of_cents(int(to_cents(__import__("pandas").Series([amount]))[0]), m)
    assert {m.label: m.value for m in at.metric}["Materiality band"] in BANDS
    assert "Materiality band" in [w.label for w in at.multiselect]
    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "overall" in captions and "clearly trivial" in captions and "never part of it" in captions
    assert "calendar year" in captions
    assert "Rule score" in [m.label for m in at.metric]  # the two scores are still the two scores
    # the band filter is the reviewer's, like the amount: choosing one band leaves that band
    kept = [b for b in ("below performance", "clearly trivial") if b in set(queue["materiality_band"])]
    band_filter = next(w for w in at.multiselect if w.label == "Materiality band")
    assert band_filter.options == [b for b in (*BANDS, "other year") if b in set(queue["materiality_band"])]
    band_filter.set_value(kept[:1]).run()
    assert not at.exception
    assert set(_queue_frame(at)["materiality_band"]) == set(kept[:1])


def test_a_materiality_that_cannot_be_used_is_said_as_text_and_changes_nothing(data_dir, tmp_path):
    at = _run_with(data_dir, tmp_path / "review.sqlite", materiality="-5")
    assert any("materiality must be above 0" in t.value for t in at.sidebar.text)
    assert "materiality_band" not in _queue_frame(at).columns
    assert "Materiality band" not in [m.label for m in at.metric]
    plain = _run_with(data_dir, tmp_path / "review.sqlite")
    assert "materiality_band" not in _queue_frame(plain).columns
    assert "Materiality band" not in [m.label for m in plain.metric]
    assert not any(w.label == "Materiality band" for w in plain.multiselect)


def test_a_ledger_of_several_years_asks_which_year_the_figures_are_set_for(tmp_path):
    from ledgerlens.materiality import OTHER_YEAR

    folder = tmp_path / "two-years"
    main(["generate", "--start", "2024-12-01", "--end", "2025-01-31", "--out-dir", str(folder)])
    at = _run_with(folder, tmp_path / "review.sqlite", materiality="50000")
    chooser = at.selectbox(key="materiality_year")
    assert chooser.options == ["2024", "2025"] and chooser.value == 2025  # the latest year first
    assert OTHER_YEAR in set(_queue_frame(at)["materiality_band"])  # December's entries
    assert any("fiscal year 2025" in c.value for c in at.sidebar.caption)
    chooser.set_value(2024).run()
    assert not at.exception
    assert OTHER_YEAR in set(_queue_frame(at)["materiality_band"])  # now January's


def test_a_band_chosen_for_one_ledger_does_not_empty_the_queue_of_the_next(data_dir, tmp_path):
    folder = tmp_path / "two-years"
    main(["generate", "--start", "2024-12-01", "--end", "2025-01-31", "--out-dir", str(folder)])
    at = _run_with(folder, tmp_path / "review.sqlite", materiality="50000")
    band_filter = next(w for w in at.multiselect if w.label == "Materiality band")
    band_filter.set_value(["other year"]).run()
    assert set(_queue_frame(at)["materiality_band"]) == {"other year"}
    at.text_input(key="ledger_path").set_value(str(data_dir / "ledger.csv")).run()  # one year: no "other year"
    assert not at.exception
    band_filter = next(w for w in at.multiselect if w.label == "Materiality band")
    assert "other year" not in band_filter.options and band_filter.value == band_filter.options
    assert len(_queue_frame(at)) > 0


def _benford_tab(at):
    tab = next(t for t in at.tabs if t.label == "Benford")
    return tab, [type(node).__name__ for node in tab]


def test_the_benford_tab_gives_no_band_below_300_amounts_and_warns_first(data_dir, tmp_path):
    """Its Conformity metric showed a band for any n, with no warning: the 297 amounts of a
    sandbox pull read "nonconformity"."""
    from ledgerlens.benford import SMALL_SAMPLE_NOTE

    folder = tmp_path / "ten-days"
    main(["generate", "--start", "2024-01-01", "--end", "2024-01-10", "--out-dir", str(folder)])
    at = _run(folder, tmp_path / "review.sqlite")
    tab, kinds = _benford_tab(at)
    assert {m.label: m.value for m in tab.metric}["Conformity"] == "insufficient data"
    assert [w.value for w in tab.warning] == [SMALL_SAMPLE_NOTE]
    assert kinds.index("Warning") < kinds.index("Metric")  # ahead of the figures it qualifies

    tab, kinds = _benford_tab(_run(data_dir, tmp_path / "other.sqlite"))  # 4 months: enough
    assert "Warning" not in kinds
    assert {m.label: m.value for m in tab.metric}["Conformity"] != "insufficient data"

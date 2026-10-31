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
    assert ReviewStore(db, identity).get_narrative(picked) is None


def test_a_refusal_that_quotes_the_response_is_drawn_inert(
        data_dir, tmp_path, identity, monkeypatch, llm):
    """st.error draws markdown, and ascii() leaves ASCII markup as it is: an image
    named as the model would be fetched when the error is drawn."""
    _narrating_with(monkeypatch, [llm.response(model=" ![x](http://127.0.0.1:9/x.png)")], llm)
    db = tmp_path / "review.sqlite"
    at = _run(data_dir, db, reviewer="ana")
    picked = at.selectbox(key="picked").value

    _click_write(at)

    errors = [e.value for e in at.error]
    assert any("\\!\\[x\\]\\(http\\:\\/\\/127\\.0\\.0\\.1\\:9\\/x\\.png\\)" in e for e in errors)
    assert not any("![x](" in e for e in errors)
    assert ReviewStore(db, identity).get_narrative(picked) is None


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

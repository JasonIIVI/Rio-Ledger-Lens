import time

import openpyxl
import pandas as pd
import pytest

from ledgerlens import evaluate, jets
from ledgerlens.benford import SMALL_SAMPLE_NOTE, segmented_benford
from ledgerlens.model import combine, score_ledger
from ledgerlens.report import build_workpaper


def _workpaper(ledger, labels, tmp_path, **kw):
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    scores, report = score_ledger(ledger)
    combined = combine(scored, scores)
    metrics = evaluate.evaluate(flags, labels, ledger["entry_id"].unique())
    path = build_workpaper(
        combined, flags, tmp_path / "wp.xlsx",
        benford=segmented_benford(ledger, by="account_code"),
        metrics=metrics, model_report=report.describe(), **kw)
    return path, flags


def test_workpaper_has_expected_sheets(ledger, labels, tmp_path):
    path, _ = _workpaper(ledger, labels, tmp_path)
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Summary", "Exceptions", "All flags", "Benford", "Methodology"]


def test_summary_states_the_caveat(ledger, labels, tmp_path):
    path, _ = _workpaper(ledger, labels, tmp_path)
    wb = openpyxl.load_workbook(path)
    text = " ".join(
        str(c.value) for row in wb["Summary"].iter_rows() for c in row if c.value
    )
    assert "a question, not a finding" in text


def test_exceptions_sheet_only_contains_flagged_entries(ledger, labels, tmp_path):
    path, flags = _workpaper(ledger, labels, tmp_path)
    wb = openpyxl.load_workbook(path)
    ws = wb["Exceptions"]
    header = [c.value for c in ws[1]]
    col = header.index("entry_id") + 1
    ids = {ws.cell(row=r, column=col).value for r in range(2, ws.max_row + 1)}
    assert ids <= set(flags["entry_id"])


def test_top_n_limits_the_exception_tab(ledger, labels, tmp_path):
    path, _ = _workpaper(ledger, labels, tmp_path, top_n=5)
    wb = openpyxl.load_workbook(path)
    assert wb["Exceptions"].max_row <= 6  # header + 5


def test_methodology_records_the_model_fit(ledger, labels, tmp_path):
    path, _ = _workpaper(ledger, labels, tmp_path)
    wb = openpyxl.load_workbook(path)
    text = " ".join(
        str(c.value) for row in wb["Methodology"].iter_rows() for c in row if c.value
    )
    assert "Isolation Forest" in text
    assert "not an audit procedure" in text
    # It said "count of distinct tests"; JET-09 and JET-11 judge lines, so a test can count twice.
    assert "sum over the flags raised" in text and "counts twice" in text
    assert "distinct tests" not in text
    # The Benford sheet reads "insufficient data" for a segment under 300 amounts; say why, under
    # Benford's heading. Wrapped at 100 as the other notes are, so read with the breaks undone.
    flat = " ".join(text.split())
    assert flat.index("Benford's Law") < flat.index(SMALL_SAMPLE_NOTE) < flat.index("Limitations")


def _note(summary, confidence="medium"):
    return {"summary": summary, "why_flagged": "w", "evidence_to_request": ["x"],
            "suggested_control": "c", "confidence": confidence}


def test_review_columns_show_the_note_the_reviewer_saw(ledger, labels, tmp_path):
    from ledgerlens.review import Decision, ReviewStore

    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    top, second, third, fourth, fifth = scored[scored["risk_score"] > 0]["entry_id"].iloc[:5]

    # Decided against the first note, which was then rewritten.
    seen = store.save_narrative(top, _note("A round-thousand manual entry."), model="claude-test")
    store.record(Decision(top, "escalate", "ana", "needs a senior", narrative_id=seen))
    store.save_narrative(top, _note("Rewritten after the decision.", "low"), model="claude-test")
    # Narrated twice, never decided: the latest note is the right one to show.
    store.save_narrative(second, _note("First draft."))
    newest = store.save_narrative(second, _note("Latest draft."))
    # Decided before any note existed.
    store.record(Decision(third, "dismiss", "ben", "routine"))
    # Decided with no note, narrated a second later: the reviewer never saw it.
    store.record(Decision(fourth, "accept", "ben", "checked the invoice"))
    time.sleep(1.1)  # the timestamps carry seconds
    later = store.save_narrative(fourth, _note("Written after the decision."))
    # Narrated, then decided without recording the note (how pre-version decisions look).
    existing = store.save_narrative(fifth, _note("Already there."))
    store.record(Decision(fifth, "dismiss", "ana", "fine"))

    path, _ = _workpaper(ledger, labels, tmp_path, store=store)
    wb = openpyxl.load_workbook(path)
    ws = wb["Exceptions"]
    header = [c.value for c in ws[1]]
    for col in ("narrative_id", "narrative", "narrative_confidence", "narrative_superseded",
                "narrative_seen_by_reviewer", "decision", "reviewer", "note"):
        assert col in header
    rows = {r[header.index("entry_id")]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    col = header.index

    assert rows[top][col("decision")] == "escalate"
    assert rows[top][col("narrative_id")] == seen
    assert rows[top][col("narrative")] == "A round-thousand manual entry."
    assert rows[top][col("narrative_confidence")] == "medium"
    assert rows[top][col("narrative_superseded")] is True
    assert rows[top][col("narrative_seen_by_reviewer")] == "yes"

    assert rows[second][col("decision")] is None
    assert rows[second][col("narrative_id")] == newest
    assert rows[second][col("narrative")] == "Latest draft."
    assert rows[second][col("narrative_superseded")] is False
    assert rows[second][col("narrative_seen_by_reviewer")] in (None, "")

    assert rows[third][col("decision")] == "dismiss"
    assert rows[third][col("narrative_id")] is None
    assert rows[third][col("narrative")] is None
    assert rows[third][col("narrative_superseded")] is False
    assert rows[third][col("narrative_seen_by_reviewer")] in (None, "")

    # The later note is shown, but not passed off as the basis of the decision.
    assert rows[fourth][col("narrative_id")] == later
    assert rows[fourth][col("narrative")] == "Written after the decision."
    assert rows[fourth][col("narrative_seen_by_reviewer")] == "no"

    assert rows[fifth][col("narrative_id")] == existing
    assert rows[fifth][col("narrative_seen_by_reviewer")] == "unknown"

    summary = " ".join(str(c.value) for row in wb["Summary"].iter_rows() for c in row if c.value)
    assert "Decisions recorded" in summary


def test_summary_names_the_ledger_and_counts_other_ledgers(ledger, labels, tmp_path):
    import sqlite3

    from ledgerlens.review import Decision, ReviewStore

    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    store.record(Decision("JE-2024-000001", "dismiss", "ana"))
    path, _ = _workpaper(ledger, labels, tmp_path, store=store)
    cells = _summary_cells(path)
    assert "Other ledgers in this database (not shown)" not in cells  # nothing else in the file

    with sqlite3.connect(str(store.path)) as raw:  # notes from before ledgers were keyed
        for entry in ("JE-2024-000002", "JE-2024-000003", "JE-2024-000004"):
            raw.execute("INSERT INTO narratives (entry_id, summary, generated_at, ledger_id) "
                        "VALUES (?, 'old', '2026-09-23T00:00:00+00:00', 'legacy')", (entry,))
    ReviewStore(store.path, "csv:b").record(Decision("JE-2024-000009", "accept", "bo"))
    path, _ = _workpaper(ledger, labels, tmp_path, store=store)
    cells = _summary_cells(path)
    assert cells["Review rows for ledger"] == "csv:test"
    assert cells["Decisions recorded"] == 1 and cells["Narratives cached"] == 0
    # Summed over the other ledgers, not counted: two ledgers, three notes, one decision.
    assert cells["Other ledgers in this database (not shown)"] == \
        "2 ledger(s): 3 narrated, 1 decided entries"


def _summary_cells(path):
    wb = openpyxl.load_workbook(path)
    return {row[0].value: row[1].value for row in wb["Summary"].iter_rows() if row[0].value}


def test_the_summary_sheet_puts_the_circularity_caveat_beside_precision_and_recall(
        ledger, labels, tmp_path):
    path, flags = _workpaper(ledger, labels, tmp_path)
    cells = [[c.value for c in row] for row in openpyxl.load_workbook(path)["Summary"].iter_rows()]
    labels_in_column_a = [row[0] for row in cells]
    assert ["Precision", "Recall"] == [v for v in labels_in_column_a if v in ("Precision", "Recall")]
    caveat_row = labels_in_column_a.index("Read these as")
    assert cells[caveat_row][1] == evaluate.DETECTION_CAVEAT
    sheet = openpyxl.load_workbook(path)["Summary"]
    caveat_cell = sheet.cell(row=caveat_row + 1, column=2)
    assert caveat_cell.alignment.wrap_text is True  # a sentence, wrapped inside its column
    assert not sheet.cell(row=caveat_row, column=2).alignment.wrap_text  # the numbers are left alone
    assert 0 < caveat_row - labels_in_column_a.index("Recall") <= 3  # the same block, not a footnote

    scored = jets.score_entries(ledger, flags)
    bare = build_workpaper(scored, flags, tmp_path / "bare.xlsx")  # no labels: no number, no caveat
    text = " ".join(str(c.value) for row in openpyxl.load_workbook(bare)["Summary"].iter_rows()
                    for c in row if c.value)
    assert "Precision" not in text and "eight of eleven" not in text
    # the sentence is wrapped inside its column and does not widen it: the counts stay beside
    # their labels, where a reader of the printed sheet looks for them
    sheet = openpyxl.load_workbook(path)["Summary"]
    caveat_cell = sheet.cell(row=caveat_row + 1, column=2)
    assert caveat_cell.alignment.wrap_text is True
    assert not sheet.cell(row=caveat_row, column=2).alignment.wrap_text  # the numbers are left alone
    widths = sheet.column_dimensions["B"].width, openpyxl.load_workbook(bare)["Summary"].column_dimensions["B"].width
    assert widths[0] == widths[1] < 30, widths
    # the column is sized to its longest unwrapped value, not to nothing: with no controls
    # file, the "Controls file" row's "none (synthetic defaults)" gives 27
    longest = max(len(str(row[1].value)) for row in sheet.iter_rows()
                  if row[1].value is not None and not row[1].alignment.wrap_text)
    assert widths[0] == longest + 2 == 27
    assert sheet.column_dimensions["A"].width >= len("Population (entries)") + 2


def test_an_identifier_in_the_summary_sheet_stays_on_one_line_and_widens_the_column(ledger, labels, tmp_path):
    """Only a sentence is wrapped. The ledger's identity (68 characters, no space) is compared
    character by character, so it stays whole and the column grows to the cap for it."""
    from ledgerlens.review import Decision, ReviewStore

    store = ReviewStore(tmp_path / "review.sqlite", "csv:" + "f" * 64)
    store.record(Decision(ledger["entry_id"].iloc[0], "dismiss", "ana"))
    path, _ = _workpaper(ledger, labels, tmp_path, store=store)
    sheet = openpyxl.load_workbook(path)["Summary"]
    identity = next(row[1] for row in sheet.iter_rows() if row[0].value == "Review rows for ledger")
    assert identity.value == "csv:" + "f" * 64 and not identity.alignment.wrap_text
    assert sheet.column_dimensions["B"].width == 60  # the cap
    caveat = next(row[1] for row in sheet.iter_rows() if row[0].value == "Read these as")
    assert caveat.alignment.wrap_text is True


def test_a_ratio_over_nothing_flagged_is_written_as_undefined_too(ledger, labels, tmp_path):
    flags = jets.run_all(ledger).iloc[0:0]
    scored = jets.score_entries(ledger, flags).assign(risk_score=0.0)
    metrics = evaluate.evaluate(flags, labels, ledger["entry_id"].unique())
    assert metrics["flagged"] == 0
    cells = _summary_cells(build_workpaper(scored, flags, tmp_path / "quiet.xlsx", metrics=metrics))
    assert cells["Precision"] == "undefined (nothing was flagged)"
    assert cells["Recall"] == 0  # 0 of 77 found: a figure


def test_a_ratio_over_nothing_is_written_as_undefined_not_zero(ledger, labels, tmp_path):
    """Labels that mark no anomaly gave "Recall 0" beside "False negatives 0"."""
    path, _ = _workpaper(ledger, labels.assign(is_anomaly=False, anomaly_type=""), tmp_path)
    cells = _summary_cells(path)
    assert cells["Precision"] == 0  # something was flagged, every flag a false positive: a figure
    assert cells["Recall"] == "undefined (the labels mark no anomaly)"
    assert cells["False negatives"] == 0


def test_ledger_text_is_written_as_text_never_as_a_formula(ledger, tmp_path):
    """openpyxl stores a string that begins with "=" as a live formula, and one that reads like
    an error code as an error. The Exceptions and All flags sheets carry ids, users and sources
    as whoever wrote the ledger typed them, and the reasons quote them; a workpaper is handed
    over to someone who opens it in Excel."""
    hostile = '=HYPERLINK("https://example.invalid/x","open the support")'
    tainted = ledger.copy()
    first = tainted["entry_id"].iloc[0]
    tainted.loc[tainted["entry_id"] == first, "created_by"] = hostile
    tainted.loc[tainted["entry_id"] == first, "source"] = "#REF!"  # read as an error code
    flags = jets.run_all(tainted)
    scored = jets.score_entries(tainted, flags)
    scored.loc[scored["entry_id"] == first, "risk_score"] = 99.0  # on the Exceptions sheet for sure
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx")
    wb = openpyxl.load_workbook(path)  # not data_only: a formula cell reads back with data_type "f"
    typed = [(ws.title, c.coordinate, c.data_type) for ws in wb.worksheets
             for row in ws.iter_rows() for c in row if c.data_type in ("f", "e")]
    assert typed == []
    ws = wb["Exceptions"]
    header = [c.value for c in ws[1]]
    rows = {r[header.index("entry_id")]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows[first][header.index("created_by")] == hostile  # the text is there, as text
    assert rows[first][header.index("source")] == "#REF!"
    source = header.index("source") + 1
    assert next(c for c in ws[2:ws.max_row] for c in c if c.column == source and c.value == "#REF!").data_type == "s"


def test_the_summary_sheet_names_the_controls_and_keeps_a_name_from_the_file_as_text(ledger, tmp_path):
    from ledgerlens.controls import Controls

    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    cells = _summary_cells(build_workpaper(scored, flags, tmp_path / "d.xlsx"))
    assert (cells["JET-12 approvers"], cells["JET-06 approval limit"], cells["Controls file"]) == (
        "controller, mchen", "$10,000", "none (synthetic defaults)")
    own = Controls(("=1+1", "controller"), 2_500.5, source="data/x.controls.json")
    path = build_workpaper(scored, flags, tmp_path / "o.xlsx", controls=own)
    sheet = openpyxl.load_workbook(path)["Summary"]  # not data_only: a formula reads back as "f"
    cell = next(row[1] for row in sheet.iter_rows() if row[0].value == "JET-12 approvers")
    assert cell.value == "=1+1, controller" and cell.data_type == "s"
    cells = _summary_cells(path)
    assert (cells["JET-06 approval limit"], cells["Controls file"]) == ("$2,500.50", "data/x.controls.json")
    methodology = " ".join(str(c.value) for row in openpyxl.load_workbook(path)["Methodology"].iter_rows()
                           for c in row if c.value)
    assert "They are facts about the company, not thresholds tuned against data." in methodology
    # only JET-11's threshold was chosen on the generator's ledger (docs/tuning.md)
    assert "Thresholds are tuned" not in methodology and "only JET-11's chosen there" in methodology


def test_the_band_sits_beside_the_amount_and_the_figures_on_the_summary_sheet(ledger, labels, tmp_path):
    from ledgerlens.materiality import BAND_NOTE, BANDS, OTHER_YEAR, Materiality

    path, _ = _workpaper(ledger, labels, tmp_path, materiality=Materiality("50000"), fiscal_year=2025)
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Summary", "Exceptions", "All flags", "Benford", "Methodology"]
    ws = wb["Exceptions"]
    header = [c.value for c in ws[1]]
    assert header.index("materiality_band") == header.index("entry_amount") + 1
    band, amount = header.index("materiality_band"), header.index("entry_amount")
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert {row[band] for row in rows} <= set(BANDS) | {OTHER_YEAR}
    assert OTHER_YEAR in {row[band] for row in rows}  # the default ledger spans 2024 and 2025
    for row in rows:
        if row[band] == "material":
            assert row[amount] >= 49999.995
        if row[band] == "clearly trivial":
            assert 0 < row[amount] < 2500.005
    cells = [str(c.value) for row in wb["Summary"].iter_rows() for c in row if c.value]
    for expected in ("Overall materiality", "$50,000.00", "Performance materiality",
                     "$37,500.00 (75% of overall)", "Clearly trivial", "$2,500.00 (5% of overall)",
                     "Fiscal year", "2025", "Flagged entries by band", "Read the band as", BAND_NOTE):
        assert expected in cells, expected
    text = " ".join(str(c.value) for row in wb["Methodology"].iter_rows() for c in row if c.value)
    assert "Materiality" in text and "never part of it" in text and "calendar year" in text


def test_the_workpaper_names_the_year_itself_for_one_year_and_refuses_several(ledger, labels, tmp_path, small_ledger):
    from ledgerlens.materiality import Materiality, SamplingError

    with pytest.raises(SamplingError) as refused:  # the default ledger spans 2024 and 2025
        _workpaper(ledger, labels, tmp_path, materiality=Materiality("50000"))
    assert "spans fiscal years 2024, 2025" in str(refused.value)
    path, _ = _workpaper(small_ledger[0], small_ledger[1], tmp_path, materiality=Materiality("50000"))
    cells = [str(c.value) for row in openpyxl.load_workbook(path)["Summary"].iter_rows() for c in row if c.value]
    assert "2024" in cells[cells.index("Fiscal year"):cells.index("Fiscal year") + 2]


def test_exceptions_tied_on_risk_and_model_score_come_in_id_order(ledger, tmp_path):
    """With a model score the Exceptions sheet is re-sorted by risk and model score. Tied on
    both (entries with identical features score alike), the id breaks the tie: a one-key sort
    keeps tied rows in input order up to 16 rows and scrambles them from 17."""
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    flagged = scored["risk_score"] > 0
    tied = scored.assign(risk_score=scored["risk_score"].where(~flagged, 4.0), model_score=0.5)
    path = build_workpaper(tied, flags, tmp_path / "tied.xlsx")
    rows = list(openpyxl.load_workbook(path)["Exceptions"].iter_rows(values_only=True))
    ids = [row[rows[0].index("entry_id")] for row in rows[1:]]
    assert len(ids) == int(flagged.sum()) > 16
    assert ids == sorted(ids)


# --- text a cell cannot hold -------------------------------------------------------------------
# openpyxl refuses a control character in a cell (IllegalCharacterError), writes U+FFFE, U+FFFF and
# half of a surrogate pair into a file that then fails to open, and cut a text over 32,767
# characters there without a word. A workpaper is evidence (rules 6 and 8): what a cell cannot
# hold is shown where it stood, counted on the Summary sheet and stated on the Methodology sheet.
# The characters are built with chr(): typed as escapes, the edit tools would decode them.

VT, FF, US, TAB, LF = chr(11), chr(12), chr(31), chr(9), chr(10)
SHOWN = "each shown as its code point (see Methodology)"


def _sheet(path, name):
    rows = list(openpyxl.load_workbook(path)[name].iter_rows(values_only=True))
    return rows[0], rows[1:]


def _cell(path, entry, column):
    header, rows = _sheet(path, "Exceptions")
    return next(r[header.index(column)] for r in rows if r[header.index("entry_id")] == entry)


def _flat(text):
    """Whitespace run together: the Methodology's notes are wrapped at 100 characters."""
    return " ".join(text.split())


def _methodology(path):
    return _flat(" ".join(str(c.value) for row in openpyxl.load_workbook(path)["Methodology"].iter_rows()
                          for c in row if c.value))


def _flagged(ledger):
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    return scored, flags, list(scored.loc[scored["risk_score"] > 0, "entry_id"])


def test_a_ledger_cell_a_cell_cannot_hold_is_shown_as_its_code_point(small_ledger, tmp_path):
    """A vertical tab in a user's name made the workpaper raise IllegalCharacterError: the
    Exceptions sheet carries the user, and the reasons that quote it (JET-12's for a manual
    entry by anyone off the approver list) are on that sheet and on All flags."""
    from ledgerlens.report import CUT_NOTE, UNWRITABLE_NOTE

    ledger = small_ledger[0].copy()
    manual = ledger.loc[ledger["source"] == "Manual", "entry_id"].iloc[0]
    ledger.loc[ledger["entry_id"] == manual, "created_by"] = "ana" + VT + "x"
    scored, flags, _ = _flagged(ledger)
    reasons = flags.loc[flags["entry_id"] == manual, "reason"]
    n, quoting = int(reasons.str.count(VT).sum()), int(reasons.str.contains(VT).sum())
    assert n >= 1 and quoting >= 1  # JET-12 at least
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx", top_n=len(scored))

    shown = [reason.replace(VT, "[U+000B]") for reason in reasons]
    assert _cell(path, manual, "created_by") == "ana[U+000B]x"
    assert _cell(path, manual, "reasons") == " | ".join(shown)  # as written but for the tab
    header, rows = _sheet(path, "All flags")
    assert [r[header.index("reason")] for r in rows if r[header.index("entry_id")] == manual] == shown
    cells = _summary_cells(path)
    # The user's cell, the reasons cell and each reason on All flags that quotes the user.
    assert cells["Characters a cell cannot hold"] == f"{1 + 2 * n} in {2 + quoting} cell(s), {SHOWN}"
    assert "Texts cut to fit a cell" not in cells
    labels = list(cells)  # column A, top to bottom
    assert labels.index("Characters a cell cannot hold") == labels.index("Shown in this workpaper") + 1
    assert _flat(UNWRITABLE_NOTE) in _methodology(path) and _flat(CUT_NOTE) not in _methodology(path)


def test_a_decision_and_a_note_holding_a_control_character_are_shown_with_its_code_point(
        small_ledger, tmp_path):
    """The review store is append-only, so a decision or a note holding a control character (a
    vertical tab is Word's line break, pasted into the note field) kept that store from ever
    writing its workpaper again."""
    from ledgerlens.review import Decision, ReviewStore

    scored, flags, ids = _flagged(small_ledger[0])
    first, second, third = ids[:3]
    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    store.record(Decision(first, "dismiss", "a" + FF + "na", "checked" + VT + "twice" + VT + "fine"))
    store.save_narrative(second, _note("Keyed" + US + "late"), model="claude-test")
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx", store=store)

    assert _cell(path, first, "note") == "checked[U+000B]twice[U+000B]fine"
    assert _cell(path, first, "reviewer") == "a[U+000C]na"
    assert _cell(path, second, "narrative") == "Keyed[U+001F]late"
    # Blank stays blank in a column that had a cell changed: nothing undecided reads "nan".
    assert _cell(path, second, "note") is None and _cell(path, third, "note") is None
    assert _cell(path, third, "narrative") is None
    # Characters, then cells: the note holds two.
    assert _summary_cells(path)["Characters a cell cannot hold"] == f"4 in 3 cell(s), {SHOWN}"


def test_a_character_xml_leaves_out_no_longer_makes_a_file_that_will_not_open(small_ledger, tmp_path):
    """openpyxl writes U+FFFE, U+FFFF and half of a surrogate pair without a word (a CSV in UTF-8
    and a reply in JSON can carry the first two), and the file then fails to load. The store
    cannot hold a surrogate on its own, but a path can (Linux turns an undecodable byte of a file
    name into one), and the controls file's path is on the Summary sheet. The Methodology's
    lines are fitted too: a caller's model report is text like any other."""
    from ledgerlens.controls import Controls
    from ledgerlens.review import Decision, ReviewStore

    scored, flags, ids = _flagged(small_ledger[0])
    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    store.record(Decision(ids[0], "accept", "ana", "seen" + chr(0xFFFE)))
    store.save_narrative(ids[1], _note("odd" + chr(0xFFFF) + "text"))
    controls = Controls(("controller", "mchen"), 10_000, source="data/" + chr(0xDC80) + ".controls.json")
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx", store=store, controls=controls,
                           model_report="fitted" + chr(0xFFFF))

    assert _cell(path, ids[0], "note") == "seen[U+FFFE]"  # the file opens
    assert _cell(path, ids[1], "narrative") == "odd[U+FFFF]text"
    cells = _summary_cells(path)
    assert cells["Controls file"] == "data/[U+DC80].controls.json"
    assert cells["Characters a cell cannot hold"] == f"4 in 4 cell(s), {SHOWN}"
    # The note on what changed follows the limitations, ahead of the blocks a run adds.
    text = _methodology(path)
    assert text.index("Limitations") < text.index("Text changed to fit a cell") < text.index(
        "Model fit") < text.index("fitted[U+FFFF]")


#: The code points on either side of each edge of what a cell cannot hold, and its own ends.
_EDGES = (8, 9, 10, 11, 12, 13, 14, 31, 32, 0x7F, 0xD7FF, 0xD800, 0xDFFF, 0xE000, 0xFFFD,
          0xFFFE, 0xFFFF, 0x10000, 0x10FFFF)


def test_each_edge_of_what_a_cell_cannot_hold(small_ledger, tmp_path):
    """Shown as its code point: 0-8, 11, 12, 14-31, a surrogate on its own, U+FFFE and U+FFFF,
    what XML 1.0 leaves out. Everything else as written; a carriage return reads back as a line
    feed (XML's line-end handling), a line break either way. A Benford label is ledger text."""
    scored, flags, _ = _flagged(small_ledger[0])
    # As objects: pandas 3 keeps a text column in Arrow where pyarrow is installed, and Arrow
    # cannot hold half of a surrogate pair at all.
    labels = pd.Series([f"a{chr(cp)}b" for cp in _EDGES], dtype=object)
    benford = pd.DataFrame({"label": labels, "n": range(len(_EDGES))})
    path = build_workpaper(scored, flags, tmp_path / "edges.xlsx", benford=benford)

    replaced = {8, 11, 12, 14, 31, 0xD800, 0xDFFF, 0xFFFE, 0xFFFF}
    expected = [f"a[U+{cp:04X}]b" if cp in replaced else "a" + (LF if cp == 13 else chr(cp)) + "b"
                for cp in _EDGES]
    assert [row[0] for row in _sheet(path, "Benford")[1]] == expected
    assert _summary_cells(path)["Characters a cell cannot hold"] == f"9 in 9 cell(s), {SHOWN}"


def test_what_a_cell_cannot_hold_includes_every_character_openpyxl_refuses():
    """openpyxl's own list, over every code point: a version that refused more would bring the
    crash back without this."""
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    from ledgerlens.report import _UNWRITABLE

    refused = [cp for cp in range(0x110000) if ILLEGAL_CHARACTERS_RE.match(chr(cp))]
    assert refused and all(_UNWRITABLE.match(chr(cp)) for cp in refused)


def _tail(units):
    return f" [cut: the text runs to {units:,} characters; a cell holds 32,767]"


def test_a_text_longer_than_a_cell_holds_is_cut_with_its_length_never_silently(small_ledger, tmp_path):
    """openpyxl cut a text over 32,767 characters there without a word (pandas 3 warns on stderr,
    pandas 2 not at all): a reviewer's long note, or the reasons of an entry with hundreds of
    flagged lines. Excel counts in UTF-16, so an emoji takes two of the 32,767."""
    from ledgerlens.report import CUT_NOTE, UNWRITABLE_NOTE
    from ledgerlens.review import Decision, ReviewStore

    scored, flags, ids = _flagged(small_ledger[0])
    smile = chr(0x1F600)
    notes = {ids[0]: "x" * 40_000, ids[1]: "y" * 32_767, ids[2]: "z" * 32_768, ids[3]: smile * 20_000}
    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    for entry, note in notes.items():
        store.record(Decision(entry, "dismiss", "ana", note))
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx", store=store)

    assert _cell(path, ids[1], "note") == notes[ids[1]]  # exactly a cell's worth: whole
    for entry, units in ((ids[0], 40_000), (ids[2], 32_768)):
        assert _cell(path, entry, "note") == notes[entry][:32_767 - len(_tail(units))] + _tail(units)
    room = 32_767 - len(_tail(40_000))  # 20,000 emoji are 40,000 UTF-16 units
    assert _cell(path, ids[3], "note") == smile * (room // 2) + _tail(40_000)
    cells = _summary_cells(path)
    assert cells["Texts cut to fit a cell"] == "3 (see Methodology)"
    assert "Characters a cell cannot hold" not in cells
    assert _flat(CUT_NOTE) in _methodology(path) and _flat(UNWRITABLE_NOTE) not in _methodology(path)


def test_a_cut_never_splits_a_code_point_shown_in_brackets(small_ledger, tmp_path):
    from ledgerlens.review import Decision, ReviewStore

    scored, flags, ids = _flagged(small_ledger[0])
    room = 32_767 - len(_tail(40_000))
    note = "w" * (room - 12) + VT * (40_000 - room + 12)  # room for one [U+000B] (8), not two
    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    store.record(Decision(ids[0], "dismiss", "ana", note))
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx", store=store)

    assert _cell(path, ids[0], "note") == "w" * (room - 12) + "[U+000B]" + _tail(40_000)
    cells = _summary_cells(path)
    assert cells["Texts cut to fit a cell"] == "1 (see Methodology)"
    assert cells["Characters a cell cannot hold"] == f"1 in 1 cell(s), {SHOWN}"  # the one shown


def test_text_a_cell_can_hold_is_written_as_it_is_and_nothing_is_said(small_ledger, tmp_path):
    from ledgerlens.review import Decision, ReviewStore

    scored, flags, ids = _flagged(small_ledger[0])
    note = "line one" + LF + "line two" + TAB + "end, [U+000B] as typed"
    store = ReviewStore(tmp_path / "review.sqlite", "csv:test")
    store.record(Decision(ids[0], "dismiss", "ana", note))
    path = build_workpaper(scored, flags, tmp_path / "wp.xlsx", store=store)

    assert _cell(path, ids[0], "note") == note
    cells = _summary_cells(path)
    assert "Characters a cell cannot hold" not in cells and "Texts cut to fit a cell" not in cells
    assert "Text changed to fit a cell" not in _methodology(path)

import time

import openpyxl

from ledgerlens import evaluate, jets
from ledgerlens.benford import segmented_benford
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
    assert "Precision" not in text and "nine of eleven" not in text
    # the sentence is wrapped inside its column and does not widen it: the counts stay beside
    # their labels, where a reader of the printed sheet looks for them
    sheet = openpyxl.load_workbook(path)["Summary"]
    caveat_cell = sheet.cell(row=caveat_row + 1, column=2)
    assert caveat_cell.alignment.wrap_text is True
    assert not sheet.cell(row=caveat_row, column=2).alignment.wrap_text  # the numbers are left alone
    widths = sheet.column_dimensions["B"].width, openpyxl.load_workbook(bare)["Summary"].column_dimensions["B"].width
    assert widths[0] == widths[1] < 30, widths
    # the column is sized to its longest unwrapped value, not to nothing: "Prepared" gives 18
    longest = max(len(str(row[1].value)) for row in sheet.iter_rows()
                  if row[1].value is not None and not row[1].alignment.wrap_text)
    assert widths[0] == longest + 2 == 18
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

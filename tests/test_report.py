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


def test_review_columns_appear_when_a_store_is_supplied(ledger, labels, tmp_path):
    from ledgerlens.review import Decision, ReviewStore

    store = ReviewStore(tmp_path / "review.sqlite")
    flags = jets.run_all(ledger)
    top = jets.score_entries(ledger, flags).iloc[0]["entry_id"]
    store.save_narrative(top, {
        "summary": "A round-thousand manual entry.", "why_flagged": "w",
        "evidence_to_request": ["x"], "suggested_control": "c", "confidence": "medium",
    }, model="claude-test")
    store.record(Decision(top, "escalate", "ana", "needs a senior"))

    path, _ = _workpaper(ledger, labels, tmp_path, store=store)
    wb = openpyxl.load_workbook(path)
    ws = wb["Exceptions"]
    header = [c.value for c in ws[1]]
    for col in ("narrative", "narrative_confidence", "decision", "reviewer", "note"):
        assert col in header
    rows = {r[header.index("entry_id")]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows[top][header.index("decision")] == "escalate"
    assert rows[top][header.index("narrative")] == "A round-thousand manual entry."
    undecided = next(r for eid, r in rows.items() if eid != top)
    assert undecided[header.index("decision")] is None

    summary = " ".join(str(c.value) for row in wb["Summary"].iter_rows() for c in row if c.value)
    assert "Decisions recorded" in summary

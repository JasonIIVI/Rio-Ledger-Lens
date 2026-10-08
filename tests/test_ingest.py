import pandas as pd
import pytest

from ledgerlens.ingest import SchemaError, entry_level, prepare
from ledgerlens.schema import DERIVED_COLUMNS


def test_derived_columns_are_added(ledger):
    for column in DERIVED_COLUMNS:
        assert column in ledger.columns


def test_amount_is_debit_minus_credit(ledger):
    expected = ledger["debit"] - ledger["credit"]
    pd.testing.assert_series_equal(ledger["amount"], expected, check_names=False)


def test_weekend_flag_matches_day_of_week(ledger):
    assert (ledger["is_weekend"] == (ledger["entered_dow"] >= 5)).all()


def test_missing_column_is_rejected(ledger):
    broken = ledger.drop(columns=["debit"])
    with pytest.raises(SchemaError) as excinfo:
        prepare(broken)
    assert "debit" in str(excinfo.value)


def test_entry_level_collapses_to_one_row_per_entry(ledger):
    collapsed = entry_level(ledger)
    assert len(collapsed) == ledger["entry_id"].nunique()
    assert collapsed["entry_id"].is_unique


def test_entry_level_imbalance_is_signed(ledger):
    collapsed = entry_level(ledger)
    recomputed = (collapsed["total_debit"] - collapsed["total_credit"]).round(2)
    pd.testing.assert_series_equal(collapsed["imbalance"], recomputed, check_names=False)


# --- ledger identity -----------------------------------------------------------


def test_ledger_identity_is_the_csv_digest_without_a_sidecar(ledger, tmp_path):
    from ledgerlens import narrative_eval
    from ledgerlens.ingest import ledger_digest, ledger_identity

    assert ledger_identity(ledger) == "csv:" + narrative_eval.DEFAULT_LEDGER_SHA256
    assert ledger_identity(ledger, tmp_path / "ledger.csv") == "csv:" + ledger_digest(ledger)
    assert narrative_eval.ledger_digest is ledger_digest  # one digest, re-exported


def test_a_qbo_sidecar_names_the_ledger_whatever_the_frame_holds(small_ledger, tmp_path):
    import json

    from ledgerlens.ingest import identity_path, ledger_identity

    lines, _ = small_ledger
    csv = tmp_path / "qbo-ledger.csv"
    assert identity_path(csv) == tmp_path / "qbo-ledger.identity.json"
    identity_path(csv).write_text(json.dumps({"ledger_id": "qbo:4620816365", "pulled_at": "x"}))
    assert ledger_identity(lines, csv) == "qbo:4620816365"
    assert ledger_identity(lines, str(csv)) == "qbo:4620816365"


@pytest.mark.parametrize("payload", [
    "not json", "[]", '{"ledger_id": null}', '{"ledger_id": "csv:abc"}', '{"ledger_id": "qbo:"}',
    '{"ledger_id": "qbo:a b"}', '{"other": "qbo:1"}', '{"ledger_id": "qbo:1\\n"}', "",
])
def test_a_malformed_sidecar_is_refused_not_ignored(small_ledger, tmp_path, payload):
    from ledgerlens.ingest import IdentityError, identity_path, ledger_identity

    lines, _ = small_ledger
    csv = tmp_path / "ledger.csv"
    identity_path(csv).write_text(payload)
    with pytest.raises(IdentityError, match="identity.json"):
        ledger_identity(lines, csv)


def test_load_labels_reads_entry_ids_as_text(tmp_path):
    """Ids that look like numbers keep their zeros, as load_csv keeps the ledger's."""
    from ledgerlens.ingest import load_labels

    path = tmp_path / "labels.csv"
    path.write_text("entry_id,is_anomaly,anomaly_type\n000123,True,round_amount\n000124,False,\n")
    labels = load_labels(path)
    assert labels["entry_id"].tolist() == ["000123", "000124"]
    assert labels["is_anomaly"].tolist() == [True, False]
    assert labels["anomaly_type"].tolist() == ["round_amount", ""]


@pytest.mark.parametrize("cells, expected", [
    (["True", "False"], [True, False]),
    (["TRUE", " false "], [True, False]),
    (["1", "0"], [True, False]),
    (["1.0", "0.0"], [True, False]),  # what pandas writes for a boolean column that held a blank
])
def test_load_labels_reads_is_anomaly_as_written(tmp_path, cells, expected):
    from ledgerlens.ingest import load_labels

    path = tmp_path / "labels.csv"
    path.write_text("entry_id,is_anomaly,anomaly_type\n" + "".join(
        f"JE-{i},{cell},\n" for i, cell in enumerate(cells)))
    assert load_labels(path)["is_anomaly"].tolist() == expected


@pytest.mark.parametrize("cells, row", [(["True", "", "False"], 2), (["no", "yes"], 1),
                                        (["False", "False", "1.5"], 3)])
def test_load_labels_refuses_a_cell_it_would_have_had_to_guess(tmp_path, cells, row):
    """astype(bool) made a blank cell and "no" both True: every such row an injected anomaly."""
    from ledgerlens.ingest import load_labels

    path = tmp_path / "labels.csv"
    path.write_text("entry_id,is_anomaly,anomaly_type\n" + "".join(
        f"JE-{i},{cell},\n" for i, cell in enumerate(cells)))
    with pytest.raises(ValueError, match=f"is_anomaly must be true/false or 1/0.*data row {row}$"):
        load_labels(path)


def test_load_labels_counts_data_rows_not_file_lines(tmp_path):
    """pandas skips a blank line and a quoted cell can span two: a file line number sent the
    reader to a line that was fine (here the bad row is file line 6)."""
    from ledgerlens.ingest import load_labels

    path = tmp_path / "labels.csv"
    path.write_text('entry_id,is_anomaly,anomaly_type\nJE-1,False,"two\nlines"\n\n\nJE-2,maybe,x\n')
    with pytest.raises(ValueError, match="1 row\\(s\\) are blank or something else, the first is data row 2$"):
        load_labels(path)


# --- cells that cannot be read are refused, by column and data row ------------------
# A blank account code was a TypeError in the rules; a blank entered time a TypeError
# inside the load; a blank entry id dropped its line, so the entry looked unbalanced; an
# amount of "abc" or "1,234.00" became 0.00 and "inf" was taken as an amount; a line
# number of 1.5 became 1. Each is now one SchemaError that names the column and the row.


def _small_csv(tmp_path):
    import datetime as dt

    from ledgerlens.generate import generate_ledger

    lines, _ = generate_ledger(start=dt.date(2024, 1, 1), end=dt.date(2024, 1, 31), entries_per_day=4)
    path = tmp_path / "ledger.csv"
    lines.to_csv(path, index=False)
    return path


def _damage(path, cells):
    """Overwrite cells in place: ``{(data_row, column): text}``, data rows counted from 1."""
    import csv

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    for (row, column), value in cells.items():
        rows[row][rows[0].index(column)] = value
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


KEYED_COLUMNS = ("entry_id", "line_no", "posting_date", "entered_at", "fiscal_year", "period",
                 "account_code", "account_type", "source", "created_by")


@pytest.mark.filterwarnings("ignore:Could not infer format")
@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("column", KEYED_COLUMNS)
def test_a_blank_cell_a_test_keys_on_is_refused_by_column_and_row(tmp_path, column, blank):
    from ledgerlens.ingest import load_csv

    path = _damage(_small_csv(tmp_path), {(2, column): blank})
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert column + " is blank on 1 row, the first is data row 2" in message, message
    assert "\n" not in message


@pytest.mark.filterwarnings("ignore:Could not infer format")
@pytest.mark.parametrize("column, value, words", [
    ("posting_date", "2024-13-45", "is not a readable date"),
    ("posting_date", "yesterday", "is not a readable date"),
    ("entered_at", "2024-01-02 09:15:00Z", "carries a time zone offset"),
    ("entered_at", "2024-01-02T09:15:00+05:30", "carries a time zone offset"),
    ("line_no", "1.5", "is not a whole number"),
    ("fiscal_year", "x", "is not a whole number"),
    ("period", "inf", "is not a whole number"),
    ("debit", "abc", "is not a number"),
    ("credit", "1,234.00", "is not a number"),
    ("debit", "inf", "is not a number"),
    ("credit", "-inf", "is not a number"),
])
def test_a_cell_that_cannot_be_read_is_refused_and_quoted(tmp_path, column, value, words):
    from ledgerlens.ingest import load_csv

    path = _damage(_small_csv(tmp_path), {(3, column): value})
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert f"{column} {words} on 1 row, the first is data row 3 ({ascii(value)})" in message, message


def test_the_quoted_cell_shows_what_the_eye_would_miss(tmp_path):
    from ledgerlens.ingest import load_csv

    value = "1" + chr(0xA0) + "234.00" + chr(0x1B) + "[31m"
    path = _damage(_small_csv(tmp_path), {(4, "debit"): value})
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert "'1\\xa0234.00\\x1b[31m'" in message and chr(0x1B) not in message and chr(0xA0) not in message


@pytest.mark.filterwarnings("ignore:Could not infer format")
def test_every_problem_is_named_at_once_in_column_order(tmp_path):
    from ledgerlens.ingest import load_csv

    path = _damage(_small_csv(tmp_path), {(5, "debit"): "abc", (7, "debit"): "x", (2, "account_code"): "",
                                          (9, "posting_date"): "never"})
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    parts = [message.index(name) for name in ("posting_date", "account_code", "debit")]
    assert parts == sorted(parts), message
    assert "debit is not a number on 2 rows, the first is data row 5 ('abc')" in message


def test_a_blank_amount_is_still_zero_and_a_blank_text_cell_is_still_allowed(tmp_path):
    from ledgerlens.ingest import load_csv

    path = _damage(_small_csv(tmp_path), {(2, "debit"): "", (2, "credit"): "", (3, "description"): "",
                                          (4, "account_name"): ""})
    lines = load_csv(path)
    assert (lines.loc[1, "debit"], lines.loc[1, "credit"]) == (0.0, 0.0)
    assert pd.isna(lines.loc[2, "description"]) and pd.isna(lines.loc[3, "account_name"])


def test_a_frame_built_in_memory_is_held_to_the_same_rule(small_ledger):
    from ledgerlens.schema import REQUIRED_COLUMNS

    raw = small_ledger[0][list(REQUIRED_COLUMNS)].copy()
    raw.loc[raw.index[3], "account_code"] = None
    raw.loc[raw.index[6], "debit"] = float("inf")
    with pytest.raises(SchemaError) as excinfo:
        prepare(raw)
    message = str(excinfo.value)
    assert "account_code is blank on 1 row, the first is data row 4" in message
    # quoted as text, the way it is shown for a CSV, whatever type it arrived as
    assert "debit is not a number on 1 row, the first is data row 7 ('inf')" in message

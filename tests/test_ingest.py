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


# --- the review of the first commit -------------------------------------------------


@pytest.mark.parametrize("token", ["NA", "N/A", "null", "None", "nan"])
def test_a_word_pandas_would_read_as_missing_stays_as_written_in_a_text_cell(tmp_path, token):
    """read_csv's default list made "NA" (a user's initials, or "N/A" on system postings)
    missing, and the check then called a visibly filled cell blank."""
    from ledgerlens.ingest import load_csv

    lines = load_csv(_damage(_small_csv(tmp_path), {(2, "created_by"): token, (3, "account_code"): token}))
    assert (lines.loc[1, "created_by"], lines.loc[2, "account_code"]) == (token, token)


@pytest.mark.parametrize("token", ["#N/A", "NA", "null", "nan", "NaN"])
def test_a_word_pandas_would_read_as_missing_is_not_a_zero_amount(tmp_path, token):
    """The same list made Excel's #N/A in an amount 0.00 without a word."""
    from ledgerlens.ingest import load_csv

    with pytest.raises(SchemaError) as excinfo:
        load_csv(_damage(_small_csv(tmp_path), {(3, "debit"): token}))
    assert f"debit is not a number on 1 row, the first is data row 3 ({ascii(token)})" in str(excinfo.value)


def _rewrite_column(path, column, change):
    import csv

    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    i = rows[0].index(column)
    for row in rows[1:]:
        row[i] = change(row[i])
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


def test_an_all_digit_date_is_the_date_it_spells_not_nanoseconds_since_1970(tmp_path):
    """read_csv read 20240101 as a number, and to_datetime took a number for nanoseconds."""
    from ledgerlens.ingest import load_csv

    plain = load_csv(_small_csv(tmp_path))
    path = _rewrite_column(tmp_path / "ledger.csv", "posting_date", lambda v: v.replace("-", ""))
    path = _rewrite_column(path, "entered_at", lambda v: v.replace("-", "").replace(" ", "").replace(":", ""))
    digits = load_csv(path)
    # values, not the unit: pandas 3 gives text and numbers different units
    assert digits["posting_date"].tolist() == plain["posting_date"].tolist()
    assert digits["entered_at"].tolist() == plain["entered_at"].tolist()


def test_an_all_digit_date_built_in_memory_is_read_the_same_way(small_ledger):
    from ledgerlens.schema import REQUIRED_COLUMNS

    raw = small_ledger[0][list(REQUIRED_COLUMNS)].copy()
    expected = raw["posting_date"].copy()
    raw["posting_date"] = raw["posting_date"].dt.strftime("%Y%m%d").astype("int64")
    # values, not the unit: pandas 3 parses text at a finer unit than the generator's dates
    assert prepare(raw)["posting_date"].tolist() == expected.tolist()
    # a column of Python ints held as objects (a list once padded with pd.NA) too: it was
    # still parsed as nanoseconds since 1970, past the dtype check
    raw["posting_date"] = pd.Series([int(v) for v in raw["posting_date"]], index=raw.index, dtype=object)
    assert prepare(raw)["posting_date"].tolist() == expected.tolist()


@pytest.mark.parametrize("row, value", [(1, "today"), (3, "now"), (1, "now")])
def test_a_word_for_the_clock_is_not_a_date(tmp_path, row, value):
    """pandas read "today" and "now" as the time the ledger was loaded."""
    from ledgerlens.ingest import load_csv

    with pytest.raises(SchemaError) as excinfo:
        load_csv(_damage(_small_csv(tmp_path), {(row, "posting_date"): value}))
    assert f"posting_date is not a readable date on 1 row, the first is data row {row} ({ascii(value)})" in str(
        excinfo.value)


@pytest.mark.parametrize("spell, words", [
    (lambda d: str((d - pd.Timestamp("1899-12-30")).days), "an Excel serial"),
    (lambda d: d.strftime("%y%m%d"), "six digits, YYMMDD"),
    (lambda d: d.strftime("%m%d%Y").lstrip("0"), "MMDDYYYY without its leading zero"),
])
def test_a_date_written_as_a_number_of_another_length_is_refused_not_guessed(tmp_path, spell, words):
    """A date column of numbers was nanoseconds since 1970; then, read as digit text, pandas
    guessed: six-digit YYMMDD dates loaded as plausible days between 2002 and 2031."""
    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "posting_date", lambda v: spell(pd.Timestamp(v)))
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert "posting_date is a number, not a date (write YYYY-MM-DD) on " in message, (words, message)
    assert "the first is data row 1 (" in message


def test_digits_held_as_text_are_read_by_their_count_too(tmp_path, small_ledger):
    """One cell of another kind makes a CSV column text, and so does a string column built in
    memory: the digits were then left to pandas, which read six-digit YYMMDD as DDMMYY
    (240101 as 24 January 2001), and the ledger loaded on the wrong days."""
    from ledgerlens.ingest import load_csv
    from ledgerlens.schema import REQUIRED_COLUMNS

    path = _rewrite_column(_small_csv(tmp_path), "posting_date", lambda v: pd.Timestamp(v).strftime("%y%m%d"))
    _damage(path, {(6, "posting_date"): "2024-01-02"})  # its own day, written as a user fixing one cell would
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    assert ("posting_date is a number, not a date (write YYYY-MM-DD) on 205 rows, the first is data row 1 "
            "('240101')") in str(excinfo.value), str(excinfo.value)

    raw = small_ledger[0][list(REQUIRED_COLUMNS)].copy()
    raw["posting_date"] = raw["posting_date"].dt.strftime("%y%m%d")
    with pytest.raises(SchemaError) as excinfo:
        prepare(raw)
    assert "posting_date is a number, not a date (write YYYY-MM-DD) on " in str(excinfo.value), str(excinfo.value)


def test_eight_digits_as_text_beside_an_iso_cell_read_as_the_days_they_spell(tmp_path):
    """Both cells say which day they are, so neither is guessed; the column was refused as
    written in two formats."""
    from ledgerlens.ingest import load_csv

    plain = load_csv(_small_csv(tmp_path))
    path = _rewrite_column(tmp_path / "ledger.csv", "posting_date", lambda v: v.replace("-", ""))
    _damage(path, {(6, "posting_date"): plain["posting_date"].iloc[5].strftime("%Y-%m-%d")})
    assert load_csv(path)["posting_date"].tolist() == plain["posting_date"].tolist()


def test_a_zone_named_by_its_abbreviation_is_found_whatever_the_machine_s_own_zone(tmp_path, monkeypatch):
    """On pandas 2, a cell ending in the machine's own zone abbreviation (EST on a New York
    machine) made the re-parse that tells formats apart raise an AttributeError: a traceback
    where the load had printed one line. On every row, such a zone was dropped without a word."""
    import time

    from ledgerlens.ingest import load_csv

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        one = _damage(_small_csv(tmp_path), {(3, "entered_at"): "2024-01-02 09:15:00 EST"})
        with pytest.raises(SchemaError) as excinfo:
            load_csv(one)
        assert "entered_at carries a time zone offset on 1 row, the first is data row 3 ('2024-01-02 09:15:00 EST')" in str(
            excinfo.value)
        every = _rewrite_column(one, "entered_at", lambda v: v.replace(" EST", "") + " PST")
        with pytest.raises(SchemaError) as excinfo:
            load_csv(every)
        assert "entered_at carries a time zone offset on " in str(excinfo.value)
        assert "mixes" not in str(excinfo.value)
    finally:
        # undo, not delenv: a suite run under TZ=... must go on in that zone, not the system's
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("machine", ["UTC", "America/New_York", "Europe/Paris"])
@pytest.mark.parametrize("spell", [
    lambda t: t.strftime("%m/%d/%Y %I:%M:%S %p") + " EST",
    lambda t: t.strftime("%m/%d/%Y %I:%M:%S %p") + " PST",
    lambda t: t.strftime("%a %b %d %H:%M:%S CET %Y"),
], ids=["am-pm-EST", "am-pm-PST", "java-CET"])
def test_a_zone_after_am_pm_or_before_a_year_is_found_whatever_the_machine_s_zone(tmp_path, monkeypatch, spell,
                                                                                    machine):
    """The pattern saw a zone name only right after the time. After AM or PM, or before the
    year Java writes last, it reached pandas: pandas 2 dropped it on every machine but the
    zone's own (there the column "mixes" zones), and pandas 3 called the time unreadable."""
    import time

    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "entered_at", lambda v: spell(pd.Timestamp(v)))
    monkeypatch.setenv("TZ", machine)
    time.tzset()
    try:
        with pytest.raises(SchemaError) as excinfo:
            load_csv(path)
    finally:
        monkeypatch.undo()
        time.tzset()
    message = str(excinfo.value)
    assert "entered_at carries a time zone offset on " in message and "the first is data row 1 (" in message, message
    assert "mixes" not in message and "not a readable date" not in message, message


@pytest.mark.parametrize("value", ["2024-01-02 09:15:00 UTC+1", "2024-01-02 09:15:00 GMT-5", "2024-01-02 09:15 CET"])
def test_a_zone_written_with_a_name_and_an_offset_is_an_offset(tmp_path, value):
    from ledgerlens.ingest import load_csv

    with pytest.raises(SchemaError) as excinfo:
        load_csv(_damage(_small_csv(tmp_path), {(3, "entered_at"): value}))
    assert f"entered_at carries a time zone offset on 1 row, the first is data row 3 ({ascii(value)})" in str(
        excinfo.value)


def test_times_of_day_that_end_in_letters_are_not_zones(tmp_path):
    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "entered_at", lambda v: pd.Timestamp(v).strftime("%Y-%m-%d %I:%M %p"))
    lines = load_csv(path)
    assert lines["entered_at"].dt.hour.max() >= 12  # read as the afternoon it says


def test_a_weekday_after_the_time_is_not_a_zone(tmp_path):
    """e19d210 took any three to five letters after a time for a zone, so a ledger 039a3ad
    read, with the weekday written after each time, was refused as carrying an offset."""
    from ledgerlens.ingest import load_csv

    plain = load_csv(_small_csv(tmp_path))
    path = _rewrite_column(tmp_path / "ledger.csv", "entered_at",
                           lambda v: pd.Timestamp(v).strftime("%Y-%m-%d %H:%M:%S %a"))
    assert load_csv(path)["entered_at"].tolist() == plain["entered_at"].tolist()


def test_hrs_after_the_time_is_not_taken_for_a_zone(tmp_path):
    """Military time's HRS, also taken for a zone since e19d210. pandas 2 reads it and pandas 3
    does not, as before; neither calls it a zone."""
    from ledgerlens.ingest import load_csv

    plain = load_csv(_small_csv(tmp_path))
    path = _rewrite_column(tmp_path / "ledger.csv", "entered_at", lambda v: v + " HRS")
    try:
        lines = load_csv(path)
    except SchemaError as exc:
        assert "entered_at is not a readable date on " in str(exc) and "zone" not in str(exc), str(exc)
    else:
        assert lines["entered_at"].tolist() == plain["entered_at"].tolist()


def test_a_day_first_ledger_is_told_which_format_was_read_and_why_it_failed(tmp_path):
    """pandas reads 02/01/2024 as 1 February and then cannot read 15/01/2024: the message
    blamed the rows that were written correctly, and dropped pandas' clue, the format."""
    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "posting_date", lambda v: pd.Timestamp(v).strftime("%d/%m/%Y"))
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert "posting_date is in a different date format from data row 1 ('01/01/2024', read as %m/%d/%Y)" in message
    assert "every cell reads as %d/%m/%Y (day first): write dates as YYYY-MM-DD" in message, message


def test_a_day_first_ledger_with_two_digit_years_is_told_so_too(tmp_path):
    """pandas guesses no format for 02/01/24 and then let dateutil choose cell by cell: month
    first where it could be, day first where it had to be. A dd/mm/yy ledger loaded with every
    day of 12 or under swapped with its month, and nothing said."""
    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "posting_date", lambda v: pd.Timestamp(v).strftime("%d/%m/%y"))
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert "posting_date is in a different date format from data row 1 ('01/01/24', read as %m/%d/%y)" in message, message
    assert "every cell reads as %d/%m/%y (day first): write dates as YYYY-MM-DD" in message, message


@pytest.mark.parametrize("fmt", ["%m/%d/%y", " %m/%d/%y"], ids=["mm/dd/yy", "leading-space"])
def test_a_month_first_ledger_with_two_digit_years_still_loads(tmp_path, fmt):
    """The format given to pandas is the first cell's as written: guessed on a stripped copy,
    it would not read one cell of a column whose every cell starts with a space."""
    from ledgerlens.ingest import load_csv

    plain = load_csv(_small_csv(tmp_path))
    path = _rewrite_column(tmp_path / "ledger.csv", "posting_date", lambda v: pd.Timestamp(v).strftime(fmt))
    assert load_csv(path)["posting_date"].tolist() == plain["posting_date"].tolist()


def test_a_date_in_another_format_is_named_against_the_row_that_set_the_format(tmp_path):
    """pandas takes a column's format from its first cell, so one fraction of a second in row 1
    (Python's isoformat writes one only when it is not zero) made every other row unreadable,
    and the message quoted a correct cell and never named row 1."""
    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "entered_at", lambda v: v.replace(" ", "T"))
    first = "2024-01-01T11:17:28.500000"
    _damage(path, {(1, "entered_at"): first})
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert f"entered_at is in a different date format from data row 1 ({ascii(first)}, read as %Y-%m-%dT%H:%M:%S.%f) on " in message, message
    assert "rows, the first is data row 2 (" in message and "not a readable date" not in message


def test_an_odd_date_further_down_is_named_and_a_bad_one_beside_it_is_told_apart(tmp_path):
    from ledgerlens.ingest import load_csv

    with pytest.raises(SchemaError) as excinfo:
        load_csv(_damage(_small_csv(tmp_path), {(4, "posting_date"): "01/02/2024", (6, "posting_date"): "2024-02-30"}))
    message = str(excinfo.value)
    assert "posting_date is in a different date format from data row 1 ('2024-01-01', read as %Y-%m-%d) on 1 row, the first is data row 4 ('01/02/2024')" in message, message
    assert "posting_date is not a readable date on 1 row, the first is data row 6 ('2024-02-30')" in message


@pytest.mark.parametrize("value", ["2024-01-02 09:15:00-08", "2024-01-02 09:15:00 UTC", "2024-01-02T09:15:00+00",
                                   "2024-01-02 09:15:00 GMT"])
def test_an_hour_only_offset_or_a_named_zone_is_an_offset(tmp_path, value):
    from ledgerlens.ingest import load_csv

    with pytest.raises(SchemaError) as excinfo:
        load_csv(_damage(_small_csv(tmp_path), {(3, "entered_at"): value}))
    assert f"entered_at carries a time zone offset on 1 row, the first is data row 3 ({ascii(value)})" in str(
        excinfo.value)


def test_offsets_that_change_across_daylight_saving_are_named_with_everything_else(tmp_path):
    """On pandas 3, -08 beside -07 raised pandas' own "Mixed timezones detected" from inside the
    check: no column named, and every other problem in the file dropped."""
    from ledgerlens.ingest import load_csv

    path = _rewrite_column(_small_csv(tmp_path), "entered_at", lambda v: v + "-08")
    _damage(path, {(4, "entered_at"): "2024-01-02 09:15:00-07", (2, "account_code"): ""})
    with pytest.raises(SchemaError) as excinfo:
        load_csv(path)
    message = str(excinfo.value)
    assert "entered_at carries a time zone offset on " in message
    assert "account_code is blank on 1 row" in message


def test_a_zoned_datetime_built_in_memory_is_found_before_pandas_coerces_its_neighbours(small_ledger):
    """pandas coerced a column of naive datetimes with one zoned value into NaT for hundreds of
    rows, and the check then called them unreadable."""
    import datetime as dt

    from ledgerlens.schema import REQUIRED_COLUMNS

    raw = small_ledger[0][list(REQUIRED_COLUMNS)].copy()
    values = [stamp.to_pydatetime() for stamp in raw["entered_at"]]
    values[2] = values[2].replace(tzinfo=dt.timezone(dt.timedelta(hours=-8)))
    raw["entered_at"] = pd.Series(values, index=raw.index, dtype=object)
    with pytest.raises(SchemaError) as excinfo:
        prepare(raw)
    message = str(excinfo.value)
    assert "entered_at carries a time zone offset on 1 row, the first is data row 3 (" in message, message
    assert "not a readable date" not in message and "different date format" not in message


@pytest.mark.parametrize("error", [ValueError("Mixed timezones detected"),
                                   AttributeError("'NoneType' object has no attribute 'total_seconds'")])
def test_zones_pandas_reads_that_the_checks_do_not_see_are_one_problem_not_a_count(small_ledger, monkeypatch, error):
    """A zone the pattern does not know gives objects on pandas 2 and an error on pandas 3, and
    pandas 2 once raised an AttributeError on the machine's own zone name: each is one problem
    naming the first cell, never pandas' own message or a traceback."""
    from ledgerlens import ingest
    from ledgerlens.schema import REQUIRED_COLUMNS

    raw = small_ledger[0][list(REQUIRED_COLUMNS)].copy()
    real = pd.to_datetime

    def mixed_zones(cells, **how):
        if cells.name == "entered_at":
            raise error
        return real(cells, **how)

    monkeypatch.setattr(ingest.pd, "to_datetime", mixed_zones)
    with pytest.raises(SchemaError) as excinfo:
        prepare(raw)
    assert ("entered_at mixes times with and without a time zone, or in several zones; the first is data "
            "row 1 (") in str(excinfo.value)



def test_a_column_built_in_memory_with_one_zone_is_one_problem(small_ledger):
    """Every row skipped for its zone left nothing to parse, and the check then also said the
    column mixed zones, quoting row 1 against nothing."""
    from ledgerlens.schema import REQUIRED_COLUMNS

    raw = small_ledger[0][list(REQUIRED_COLUMNS)].copy()
    raw["entered_at"] = raw["entered_at"].dt.tz_localize("UTC")
    with pytest.raises(SchemaError) as excinfo:
        prepare(raw)
    message = str(excinfo.value)
    assert f"entered_at carries a time zone offset on {len(raw)} rows, the first is data row 1" in message
    assert "mixes" not in message


def test_a_label_file_keeps_an_id_pandas_would_read_as_missing(tmp_path):
    """load_csv keeps "NA" as an entry id; load_labels made it missing, so a ledger the load
    accepts could not be paired with its own label file."""
    from ledgerlens.ingest import load_labels

    path = tmp_path / "labels.csv"
    path.write_text("entry_id,is_anomaly,anomaly_type\nNA,false,\nJE-1,true,round_amount\n", encoding="utf-8")
    assert load_labels(path)["entry_id"].tolist() == ["NA", "JE-1"]

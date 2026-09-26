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
    '{"ledger_id": "qbo:a b"}', '{"other": "qbo:1"}',
])
def test_a_malformed_sidecar_is_refused_not_ignored(small_ledger, tmp_path, payload):
    from ledgerlens.ingest import IdentityError, identity_path, ledger_identity

    lines, _ = small_ledger
    csv = tmp_path / "ledger.csv"
    identity_path(csv).write_text(payload)
    with pytest.raises(IdentityError, match="identity.json"):
        ledger_identity(lines, csv)

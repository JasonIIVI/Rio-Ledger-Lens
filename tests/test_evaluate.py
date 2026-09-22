import pandas as pd

from ledgerlens import evaluate, jets


def test_perfect_detection_scores_one():
    labels = pd.DataFrame({
        "entry_id": ["A", "B", "C"],
        "is_anomaly": [True, False, False],
        "anomaly_type": ["round_amount", "", ""],
    })
    flags = pd.DataFrame({"entry_id": ["A"], "test_id": ["JET-01"]})
    m = evaluate.evaluate(flags, labels)
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["f1"] == 1.0


def test_missed_anomaly_lowers_recall():
    labels = pd.DataFrame({
        "entry_id": ["A", "B"],
        "is_anomaly": [True, True],
        "anomaly_type": ["round_amount", "weekend_entry"],
    })
    flags = pd.DataFrame({"entry_id": ["A"], "test_id": ["JET-01"]})
    m = evaluate.evaluate(flags, labels)
    assert m["recall"] == 0.5
    assert m["false_negatives"] == 1


def test_false_positive_lowers_precision():
    labels = pd.DataFrame({
        "entry_id": ["A", "B"],
        "is_anomaly": [True, False],
        "anomaly_type": ["round_amount", ""],
    })
    flags = pd.DataFrame({"entry_id": ["A", "B"], "test_id": ["JET-01", "JET-01"]})
    m = evaluate.evaluate(flags, labels)
    assert m["precision"] == 0.5
    assert m["false_positives"] == 1


def test_no_flags_does_not_divide_by_zero():
    labels = pd.DataFrame({
        "entry_id": ["A"], "is_anomaly": [True], "anomaly_type": ["round_amount"],
    })
    m = evaluate.evaluate(pd.DataFrame(columns=["entry_id"]), labels)
    assert m["precision"] == 0.0
    assert m["recall"] == 0.0


def test_end_to_end_recall_is_high(ledger, labels):
    flags = jets.run_all(ledger)
    m = evaluate.evaluate(flags, labels, ledger["entry_id"].unique())
    # The rule layer is definitional for most archetypes, so recall should be
    # near total. If this regresses, a test has stopped firing.
    assert m["recall"] >= 0.90
    assert m["precision"] >= 0.70


def test_recall_by_archetype_covers_all_injected_types(ledger, labels):
    flags = jets.run_all(ledger)
    table = evaluate.recall_by_archetype(flags, labels)
    injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
    assert set(table["anomaly_type"]) == injected
    assert (table["caught"] + table["missed"] == table["n"]).all()


def test_precision_by_test_is_bounded(ledger, labels):
    flags = jets.run_all(ledger)
    table = evaluate.precision_by_test(flags, labels)
    assert ((table["precision"] >= 0) & (table["precision"] <= 1)).all()


def test_format_report_is_readable():
    m = {
        "population": 100, "true_anomalies": 5, "flagged": 6, "flag_rate": 0.06,
        "true_positives": 4, "false_positives": 2, "false_negatives": 1,
        "true_negatives": 93, "precision": 0.667, "recall": 0.8, "f1": 0.727,
    }
    text = evaluate.format_report(m)
    assert "Precision" in text and "Recall" in text

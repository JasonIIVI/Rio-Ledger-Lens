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


# --- two-tier comparison ----------------------------------------------------


def test_compare_tiers_partitions_the_population(ledger, labels):
    from ledgerlens.model import combine, score_ledger

    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    scores, _ = score_ledger(ledger)
    combined = combine(scored, scores)

    table = evaluate.compare_tiers(combined, labels)
    assert set(table["segment"]) == {"rules only", "model only", "both", "neither"}
    # every entry lands in exactly one segment
    assert table["entries"].sum() == len(combined)


def test_agreement_segment_is_the_most_precise(ledger, labels):
    """When both tiers agree, the flag should be far more trustworthy."""
    from ledgerlens.model import combine, score_ledger

    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    scores, _ = score_ledger(ledger)
    combined = combine(scored, scores)

    table = evaluate.compare_tiers(combined, labels).set_index("segment")
    assert table.loc["both", "precision"] > table.loc["model only", "precision"]
    assert table.loc["both", "precision"] > table.loc["neither", "precision"]


def test_model_lift_beats_random(ledger, labels):
    from ledgerlens.model import score_ledger

    scores, _ = score_ledger(ledger)
    table = evaluate.model_lift(scores, labels)
    assert not table.empty
    assert (table["lift_vs_random"] > 1).all()
    assert table["precision"].is_monotonic_decreasing


def test_model_lift_skips_oversized_n():
    import pandas as pd

    scores = pd.Series([0.9, 0.5, 0.1], index=["A", "B", "C"])
    labels = pd.DataFrame({
        "entry_id": ["A", "B", "C"],
        "is_anomaly": [True, False, False],
        "anomaly_type": ["round_amount", "", ""],
    })
    table = evaluate.model_lift(scores, labels, tops=(2, 100))
    assert list(table["top_n"]) == [2]


def test_score_by_archetype_covers_every_type(ledger, labels):
    from ledgerlens.model import score_ledger

    scores, _ = score_ledger(ledger)
    table = evaluate.score_by_archetype(scores, labels)
    injected = set(labels.loc[labels["is_anomaly"], "anomaly_type"])
    assert set(table["anomaly_type"]) == injected
    assert table["mean_model_score"].is_monotonic_decreasing


def test_duplicates_are_invisible_to_entry_level_features(ledger, labels):
    """A duplicate only exists by comparison, so per-entry features cannot see it.

    This asserts a known design limitation rather than a capability - if it ever
    starts failing, the feature set has gained cross-entry context and the
    README's claims need revisiting.
    """
    from ledgerlens.model import score_ledger

    scores, _ = score_ledger(ledger)
    table = evaluate.score_by_archetype(scores, labels).set_index("anomaly_type")
    assert table.loc["duplicate_entry", "vs_normal"] < table.loc["round_amount", "vs_normal"]

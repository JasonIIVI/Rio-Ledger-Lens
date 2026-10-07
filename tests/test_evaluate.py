import pandas as pd
import pytest

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


# --- the caveats, and labels that belong to another ledger -------------------


def test_the_detection_caveat_counts_the_archetypes_it_names():
    from ledgerlens.schema import AnomalyType

    circular = len(AnomalyType.ALL) - len(evaluate.NON_CIRCULAR_ARCHETYPES)
    assert (circular, len(AnomalyType.ALL)) == (9, 11)  # the text says "nine of eleven"
    assert "nine of eleven" in evaluate.DETECTION_CAVEAT
    assert set(evaluate.NON_CIRCULAR_ARCHETYPES) <= set(AnomalyType.ALL)
    for name in evaluate.NON_CIRCULAR_ARCHETYPES:
        assert name in evaluate.DETECTION_CAVEAT
    # Neither caveat carries a figure: the numbers beside it move with the ledger.
    for text in (evaluate.DETECTION_CAVEAT, evaluate.MODEL_TIER_CAVEAT):
        assert not any(ch.isdigit() for ch in text)
    assert "re-ranking" in evaluate.MODEL_TIER_CAVEAT


def test_check_labels_accepts_the_generators_own_pair(ledger, labels):
    evaluate.check_labels(labels, ledger["entry_id"].unique())


def _labels(ids):
    return pd.DataFrame({"entry_id": ids, "is_anomaly": [False] * len(ids),
                         "anomaly_type": [""] * len(ids)})


@pytest.mark.parametrize("label_ids, message", [
    (["A", "B"], "1 ledger entry has no label (e.g. 'C')"),            # a flag on C would be a false positive
    (["A", "B", "C", "D"], "1 label names no ledger entry (e.g. 'D')"),  # an anomaly nobody could catch
    (["A", "B", "B", "C"], "lists 1 entry id more than once (e.g. 'B')"),  # recall by archetype counts rows
    (["X", "Y", "Z"], "3 ledger entries have no label (e.g. 'A'); 3 labels name no ledger entry (e.g. 'X')"),
])
def test_check_labels_refuses_labels_that_do_not_cover_exactly_the_ledger(label_ids, message):
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels(label_ids), ["A", "B", "C"])
    assert message in str(refused.value)


def test_check_labels_compares_the_values_evaluate_joins_on_and_needs_the_column():
    """An id read as a number never meets the same id read as text in evaluate's join, so a
    check that compared their text would pass a pairing that then scores as all misses."""
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels([1, 2]), ["1", "2"])  # a csv read without a dtype
    assert "(e.g. '1')" in str(refused.value) and "(e.g. 1)" in str(refused.value)
    evaluate.check_labels(_labels([1, 2]), [2, 1])
    with pytest.raises(evaluate.LabelsMismatchError, match="no entry_id column"):
        evaluate.check_labels(pd.DataFrame({"is_anomaly": [True]}), ["A"])


def test_check_labels_names_blank_ids_whatever_else_is_wrong():
    """pandas 2 reads a blank id as the text '<NA>', pandas 3 keeps it missing: sorting the
    missing value beside real ids was a TypeError on the newer one only."""
    for frame in (
        _labels(["A", None, "B"]),
        pd.DataFrame({"entry_id": pd.array(["A", pd.NA, "X", "X"], dtype="string"),
                      "is_anomaly": [False] * 4, "anomaly_type": [""] * 4}),
    ):
        with pytest.raises(evaluate.LabelsMismatchError, match="1 row with no entry id"):
            evaluate.check_labels(frame, ["A", "B"])


def test_a_refusal_quotes_ids_so_they_can_be_told_apart_and_cannot_act():
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels(["A", " B"]), ["A", "B"])  # they would print alike
    assert "(e.g. 'B')" in str(refused.value) and "(e.g. ' B')" in str(refused.value)

    hostile = "x\x1b]0;title\x07\n::warning::from a ledger cell" + chr(0x202E) + "y" * 200
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels(["A"]), ["A", hostile])
    message = str(refused.value)
    assert message.isascii() and "\x1b" not in message and "\n" not in message
    assert r"x\x1b]0;title\x07\n::warning::" in message  # escaped, still there to read
    quoted = message.split("(e.g. ")[1]
    assert len(quoted) < 110 and quoted.count("'") == 2  # cut to a line's worth; the quote closes
    assert quoted.endswith(f"... ({len(hostile)} characters))")

    # two long ids alike for their first 48 characters are not printed as the same id
    long_a, long_b = "JE-" + "9" * 60 + "-A", "JE-" + "9" * 70 + "-B"
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels([long_a]), [long_b])
    assert "(65 characters)" in str(refused.value) and "(75 characters)" in str(refused.value)
    assert evaluate._example("short id") == "'short id'" and evaluate._example(7) == "7"

    # an id that differs from another by a character no eye sees is not printed as the same id
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels(["A" + chr(0xFE0F)]), ["A"])  # a variation selector: printable to Python
    message = str(refused.value)
    assert message.isascii() and "(e.g. 'A')" in message and "(e.g. 'A\\ufe0f')" in message
    assert evaluate._example("caf" + chr(0xE9)) == "'caf\\xe9'"  # the same text on every Python's tables
    # the cut is by the escaped text's length: a CJK character is six of it, an emoji ten
    for wide in (chr(0x4E2D) * 49, chr(0x1F600) * 49, chr(0xE9) * 49, ("A" + chr(0xFE0F)) * 30):
        quoted = evaluate._example(wide)
        assert quoted.isascii() and len(quoted) < 80, quoted
        assert quoted.endswith(f"... ({len(wide)} characters)") and quoted.count("'") == 2, quoted


def test_check_labels_orders_ids_of_two_types_without_a_type_error():
    """key=repr in the three sorts: an int and a text id cannot be ordered against each other."""
    with pytest.raises(evaluate.LabelsMismatchError) as refused:
        evaluate.check_labels(_labels([1, "2"]), ["1", 2])
    assert "2 ledger entries have no label" in str(refused.value)
    with pytest.raises(evaluate.LabelsMismatchError, match="more than once"):
        evaluate.check_labels(_labels([1, "1", 1, "1"]), [1, "1"])


def test_score_by_archetype_of_labels_that_mark_no_anomaly_is_an_empty_table(ledger, labels):
    from ledgerlens.model import score_ledger

    scores, _ = score_ledger(ledger)
    table = evaluate.score_by_archetype(scores, labels.assign(is_anomaly=False))
    assert table.empty
    assert list(table.columns) == ["anomaly_type", "n", "mean_model_score", "vs_normal"]


def test_model_lift_breaks_a_tie_at_the_cut_by_entry_id():
    """Entries with identical features score identically, so a cut can fall inside a tie.
    The scores decide first, and a tie goes to the lower entry ids, whatever order the scores
    arrive in: the default sort is not stable, so left to it the order of a tie is the sort's."""
    ids = [f"JE-{i:03d}" for i in range(60)]
    # Ten entries score above a fifty-way tie, so the cut at 25 takes them and the tie's 15
    # lowest ids. The anomalies are those ten and the first ten ids: 20 at the cut.
    scores = pd.Series([2.0 if i >= 50 else 1.0 for i in range(60)], index=ids)
    anomalous = [i < 10 or i >= 50 for i in range(60)]
    labels = pd.DataFrame({
        "entry_id": ids,
        "is_anomaly": anomalous,
        "anomaly_type": ["round_amount" if a else "" for a in anomalous],
    })
    for order in (scores, scores.iloc[::-1], scores.sample(frac=1, random_state=1)):
        table = evaluate.model_lift(order, labels, tops=(25,)).set_index("top_n")
        assert int(table.loc[25, "true_anomalies"]) == 20

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


def test_attribution_says_which_test_caught_each_archetype():
    labels = pd.DataFrame({
        "entry_id": ["A", "B", "C", "D", "E", "F"],
        "is_anomaly": [True, True, True, True, True, False],
        "anomaly_type": ["round_amount", "round_amount", "weekend_entry", "benford_drift",
                         "made_up", ""],
    })
    flags = pd.DataFrame({
        "entry_id": ["A", "A", "A", "B", "C", "D", "F"],
        "test_id": ["JET-01", "JET-01", "JET-12", "JET-12", "JET-12", "JET-12", "JET-01"],
    })
    table = evaluate.attribution(flags, labels).set_index("anomaly_type")
    # the generator's archetypes in their own order, then any other type by name
    assert list(table.index) == ["round_amount", "weekend_entry", "benford_drift", "made_up"]
    assert table.loc["round_amount"].to_dict() == {
        "intended_test": "JET-01", "n": 2, "caught": 2, "by_intended_test": 1,
        "by_other_tests": 2, "only_by_other_tests": 1, "missed": 0}
    assert table.loc["weekend_entry", "intended_test"] == "JET-02"
    assert table.loc["weekend_entry", ["caught", "by_intended_test", "only_by_other_tests"]].tolist() == [1, 0, 1]
    # no test was written for benford_drift, or for a type the generator does not know
    assert table.loc["benford_drift", "intended_test"] == ""
    assert table.loc["benford_drift", ["by_intended_test", "only_by_other_tests"]].tolist() == [0, 1]
    assert table.loc["made_up", ["intended_test", "caught", "missed"]].tolist() == ["", 0, 1]
    # a flag on an entry the labels call normal is no archetype's
    assert table["by_other_tests"].sum() == 4
    held = labels.assign(anomaly_type=labels["anomaly_type"].replace("made_up", "token_amount"))
    assert list(evaluate.attribution(flags, held)["anomaly_type"])[0] == "token_amount"  # as elsewhere
    empty = evaluate.attribution(pd.DataFrame(columns=["entry_id", "test_id"]), labels)
    assert empty["caught"].sum() == 0 and empty["missed"].sum() == 5


def test_eight_archetypes_are_caught_by_their_own_test_and_three_mostly_by_others(ledger, labels):
    """What DETECTION_CAVEAT says, measured on the default ledger."""
    from ledgerlens.schema import AnomalyType

    assert list(evaluate.INTENDED_TEST) == list(AnomalyType.ALL)
    assert {t for t in evaluate.INTENDED_TEST.values() if t} <= set(jets.REGISTRY)
    table = evaluate.attribution(jets.run_all(ledger), labels).set_index("anomaly_type")
    for archetype, row in table.iterrows():
        if archetype in evaluate.NOT_DEFINITIONAL:
            assert row.by_intended_test * 2 <= row.caught, archetype  # half or less of them
            assert row.by_other_tests == row.caught > row.by_intended_test, archetype
        else:
            assert row.by_intended_test == row.caught == row.n, archetype


def test_precision_by_test_is_bounded(ledger, labels):
    flags = jets.run_all(ledger)
    table = evaluate.precision_by_test(flags, labels)
    assert ((table["precision"] >= 0) & (table["precision"] <= 1)).all()


def test_recall_by_archetype_breaks_a_tie_by_name():
    """However many rows tie on recall, they come back by name. On recall alone pandas' default
    sort, which is not stable, kept tied rows in order on a Mac in a table of up to 16 rows and
    could scramble them in a longer one, and on CI's Linux runners it scrambled shorter tables
    too, so `test` printed tied rows in another order there. The archetypes' sizes run against
    their names, so a tie broken on any other column comes out in another order as well."""
    names = [f"type_{i:02d}" for i in range(40)]
    sizes = [1 + (39 - i) % 4 for i in range(40)]
    entries = [(name, f"{name}-{k}") for name, size in zip(names, sizes) for k in range(size)]
    labels = pd.DataFrame({
        "entry_id": [entry for _, entry in entries] + ["N"],
        "is_anomaly": [True] * len(entries) + [False],
        "anomaly_type": [name for name, _ in entries] + [""],
    })
    caught = set(names[0::2])
    flags = pd.DataFrame({"entry_id": [entry for name, entry in entries if name in caught],
                          "test_id": "JET-01"})
    table = evaluate.recall_by_archetype(flags, labels)
    assert list(table["anomaly_type"]) == names[1::2] + names[0::2]  # missed, then caught
    assert list(table["recall"]) == [0.0] * 20 + [1.0] * 20
    assert table["n"].nunique() > 1


def test_precision_by_test_breaks_a_tie_by_test_id():
    """However many tests tie on precision, they come back by test id, for the same reason. The
    counts and the names run against the ids, so a tie broken on either comes out in another
    order as well."""
    ids = [f"JET-{i:02d}" for i in range(1, 41)]
    names = {test: f"name {(i * 7) % 40:02d}" for i, test in enumerate(ids)}
    sizes = [1 + (40 - i) % 4 for i in range(1, 41)]
    flagged = [(test, f"{test}-{k}") for test, size in zip(ids, sizes) for k in range(size)]
    flags = pd.DataFrame({
        "entry_id": [entry for _, entry in flagged],
        "test_id": [test for test, _ in flagged],
        "test_name": [names[test] for test, _ in flagged],
    })
    anomalous = set(ids[0::2])
    labels = pd.DataFrame({
        "entry_id": [entry for _, entry in flagged],
        "is_anomaly": [test in anomalous for test, _ in flagged],
        "anomaly_type": ["round_amount" if test in anomalous else "" for test, _ in flagged],
    })
    table = evaluate.precision_by_test(flags, labels)
    assert list(table["test_id"]) == ids[0::2] + ids[1::2]  # every 1.0, then every 0.0
    assert list(table["precision"]) == [1.0] * 20 + [0.0] * 20
    assert table["entries_flagged"].nunique() > 1


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

    circular = len(AnomalyType.ALL) - len(evaluate.NOT_DEFINITIONAL)
    assert (circular, len(AnomalyType.ALL)) == (8, 11)  # the text says "eight of eleven"
    assert "eight of eleven" in evaluate.DETECTION_CAVEAT
    assert set(evaluate.NOT_DEFINITIONAL) <= set(AnomalyType.ALL)
    for name in evaluate.NOT_DEFINITIONAL:
        assert name in evaluate.DETECTION_CAVEAT
    # which test catches those three depends on the draw: JET-08 catches every rare pair on
    # other seeds, so "mostly by other tests" is said of the default ledger only
    assert "depends on the draw: on the default ledger" in evaluate.DETECTION_CAVEAT
    # Neither caveat carries a figure: the numbers beside it move with the ledger.
    for text in (evaluate.DETECTION_CAVEAT, evaluate.MODEL_TIER_CAVEAT):
        assert not any(ch.isdigit() for ch in text)
    assert "re-ranking" in evaluate.MODEL_TIER_CAVEAT
    # of anomalies, not of entries: most of the entries the model flags, no rule did
    assert "almost every anomaly the model ranks highly" in evaluate.MODEL_TIER_CAVEAT


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


def test_precision_by_test_counts_entries_and_says_so(ledger, labels):
    """Its "flags" column counted entries, while `ledgerlens test` printed a "flags" column
    counting flags just above it: JET-11 read 8 in one and 7 in the other."""
    from ledgerlens import jets

    table = evaluate.precision_by_test(jets.run_all(ledger), labels).set_index("test_id")
    assert list(table.columns) == ["test_name", "entries_flagged", "true_positives", "precision"]
    assert (table.loc["JET-11", "entries_flagged"], table.loc["JET-11", "true_positives"]) == (7, 6)
    assert list(evaluate.precision_by_test(jets.run_all(ledger).iloc[:0], labels).columns) == [
        "test_id", "test_name", "entries_flagged", "true_positives", "precision"]


def _tiers_and_labels():
    combined = pd.DataFrame({
        "entry_id": list("ABCDEFGHIJ"),
        "rule_flag": [True] + [False] * 9,
        "model_flag": [False, True, True] + [False] * 7,
    })
    labels = pd.DataFrame({
        "entry_id": list("ABCDEFGHIJ"),
        "is_anomaly": [True, True, True, True] + [False] * 6,
        "anomaly_type": ["round_amount", "token_amount", "token_amount", "made_up"] + [""] * 6,
    })
    return combined, labels


def test_budget_recall_counts_each_archetype_against_chance():
    combined, labels = _tiers_and_labels()
    table = evaluate.budget_recall_by_archetype(combined, labels).set_index("anomaly_type")
    assert list(table.index) == ["token_amount", "round_amount", "made_up"]  # held out first
    # a budget of 2 of 10 entries: an archetype of n puts n * 2 / 10 there by chance
    assert table.loc["token_amount"].to_dict() == {
        "n": 2, "rule_caught": 0, "model_in_budget": 2, "chance": 0.4, "model_recall": 1.0}
    assert table.loc["round_amount", ["rule_caught", "model_in_budget", "chance"]].tolist() == [1, 0, 0.2]
    assert table.loc["made_up", ["n", "model_in_budget", "model_recall"]].tolist() == [1, 0, 0.0]


def test_budget_recall_rounds_chance_to_the_cent_and_counts_an_id_the_ledger_lacks_in_neither():
    combined = pd.DataFrame({"entry_id": ["A", "B", "C"], "rule_flag": [False] * 3,
                             "model_flag": [True, True, False]})
    labels = pd.DataFrame({"entry_id": ["A", "Z"], "is_anomaly": [True, True],
                           "anomaly_type": ["token_amount", "token_amount"]})
    row = evaluate.budget_recall_by_archetype(combined, labels).iloc[0]
    assert (row.n, row.rule_caught, row.model_in_budget, row.chance) == (2, 0, 1, 1.33)


def test_the_held_out_archetypes_together_against_the_budget():
    combined, labels = _tiers_and_labels()
    assert evaluate.held_out_budget(combined, labels) == {
        "n": 2, "in_budget": 2, "chance": 0.4, "recall": 1.0, "lift": 5.0, "budget": 2,
        "population": 10}
    # a held-out type on a row the labels call normal is not one of them
    normal = labels.assign(anomaly_type=labels["anomaly_type"].where(labels["is_anomaly"], "token_amount"))
    assert evaluate.held_out_budget(combined, normal)["n"] == 2
    none_held_out = labels.assign(anomaly_type=labels["anomaly_type"].replace("token_amount", "made_up"))
    assert evaluate.held_out_budget(combined, none_held_out) is None
    empty_budget = combined.assign(model_flag=False)
    assert evaluate.held_out_budget(empty_budget, labels)["lift"] == 0.0  # chance of nothing


def test_the_model_tier_caveat_says_what_measures_it_as_a_detector():
    assert ("Only the held-out archetypes, which no rule describes, measure it as a detector"
            in evaluate.MODEL_TIER_CAVEAT)

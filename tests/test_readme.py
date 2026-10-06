"""The README quotes numbers. Each one here is computed from the default ledger and then looked
for in the README's text, so a figure that moves fails a test instead of going stale on the page
(the QuickBooks paragraph's figures are pinned beside the fixture replay, in tests/test_cli.py).

It checks that the figures are the measured ones. It cannot check that they mean what a
reader takes them to mean: that is what the caveat beside each of them is for.
"""

import inspect
import re
from pathlib import Path

import pytest

from ledgerlens import evaluate, jets
from ledgerlens.features import FEATURE_COLUMNS
from ledgerlens.model import combine, score_ledger
from ledgerlens.schema import AnomalyType

README = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").split())

#: The README writes small counts as words; a pinned figure has to be looked for as one.
WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve")


@pytest.fixture(scope="module")
def measured(ledger, labels):
    flags = jets.run_all(ledger)
    scores, _ = score_ledger(ledger)
    combined = combine(jets.score_entries(ledger, flags), scores)
    return {
        "metrics": evaluate.evaluate(flags, labels, ledger["entry_id"].unique()),
        "recall": evaluate.recall_by_archetype(flags, labels).set_index("anomaly_type"),
        "by_test": evaluate.precision_by_test(flags, labels).set_index("test_id"),
        "tiers": evaluate.compare_tiers(combined, labels).set_index("segment"),
        "lift": evaluate.model_lift(scores, labels).set_index("top_n"),
    }


def test_the_results_table_is_what_the_default_ledger_measures(measured):
    m = measured["metrics"]
    assert f"({m['population']:,} entries, {m['true_anomalies']} injected anomalies" in README
    assert f"| Precision | **{m['precision']:.3f}** | {m['true_positives']} of the {m['flagged']} flagged" in README
    assert f"| Recall | **{m['recall']:.3f}** | {m['true_positives']} of {m['true_anomalies']} found" in README
    assert f"| F1 | **{m['f1']:.3f}** |" in README
    assert f"| Flag rate | {m['flag_rate']:.2%} of population | {m['flagged']} of {m['population']:,} entries" in README
    drift, pair = measured["recall"].loc["benford_drift"], measured["recall"].loc["rare_account_pair"]
    assert (f"`benford_drift` {int(drift.caught)} of {int(drift.n)} ({drift.recall:.2f}) and "
            f"`rare_account_pair` {int(pair.caught)} of {int(pair.n)} ({pair.recall:.2f})") in README
    jet12 = measured["by_test"].loc["JET-12"]
    false_flags = int(jet12["flags"] - jet12["true_positives"])  # .flags is pandas' own
    assert f"{false_flags} of the {m['false_positives']} that were not come from JET-12" in README
    # the caveat is in the table's own rows, not only in the paragraph after it
    assert "and partly circular: nine of the eleven archetypes are injected by the definition" in README


def test_the_tier_and_lift_tables_are_what_the_default_ledger_measures(measured):
    names = {"both": r"\*\*both tiers agree\*\*", "rules only": "rules only",
             "model only": "model only", "neither": "neither"}
    for segment, row in measured["tiers"].iterrows():
        cells = (rf"\| {names[segment]} \| {int(row.entries):,} \| {int(row.true_anomalies)} "
                 rf"\| \**{row.precision:.3f}\** \|")
        assert re.search(cells, README), segment
    m = measured["metrics"]
    assert f"The base rate ({m['true_anomalies']} of {m['population']:,} is {m['true_anomalies'] / m['population']:.3f})" in README
    for top_n in (25, 50, 100):
        row = measured["lift"].loc[top_n]
        cells = (rf"\| {top_n} \| {int(row.true_anomalies)} \| {row.precision:.2f} "
                 rf"\| \**{row.lift_vs_random:.0f}x\** \|")
        assert re.search(cells, README), top_n
    assert "Read the lift as re-ranking, not as detection" in README


def test_the_archetype_table_and_the_benford_example_are_what_the_default_ledger_measures(
        ledger, labels, measured):
    from ledgerlens.benford import benford_test

    scores, _ = score_ledger(ledger)
    by_archetype = evaluate.score_by_archetype(scores, labels).set_index("anomaly_type")
    for archetype in ("round_amount", "unbalanced_entry", "benford_drift", "weekend_entry",
                      "duplicate_entry"):
        row = by_archetype.loc[archetype]
        assert f"| {archetype} | {row.mean_model_score:.3f} | {row.vs_normal:+.2f} |" in README, archetype

    digits = benford_test(ledger["abs_amount"], "population")
    assert f"MAD {digits['mad']:.5f} ({digits['conformity'].lower()})" in README.lower().replace("mad", "MAD")
    assert f"chi-square {digits['chi_square']:.2f} (critical 15.507 at 5%, 8 df) -> exceeds" in README
    assert digits["exceeds_critical"]
    assert (f"(observed {digits['observed_prop'][1]:.2%} of leading 1s against an expected "
            f"{digits['expected_prop'][1]:.2%})") in README

    m, jet12 = measured["metrics"], measured["by_test"].loc["JET-12"]
    false_flags = int(jet12["flags"] - jet12["true_positives"])
    assert f"JET-12 produces {false_flags} of the {m['false_positives']} false positives" in README
    assert m["false_positives"] - false_flags == 1  # "exactly **one** false positive" without it
    assert f"exactly **one** false positive across {m['population']:,} entries" in README


def test_the_prose_repeats_the_tables_figures_and_computes_its_counts(measured, ledger):
    """The bullets and paragraphs restate the tables' figures in words; each is looked for as
    the measured value spells it, and "nine of the eleven" comes from the archetype lists."""
    m = measured["metrics"]
    drift, pair = measured["recall"].loc["benford_drift"], measured["recall"].loc["rare_account_pair"]
    jet12 = measured["by_test"].loc["JET-12"]
    false_flags = int(jet12["flags"] - jet12["true_positives"])
    rate = m["true_anomalies"] / m["population"]
    assert f"({m['population']:,} entries, {m['true_anomalies']} injected anomalies at {rate:.1%}):" in README
    assert f"**Benford drift: {drift.recall:.2f} recall.**" in README
    assert f"{WORDS[int(drift.missed)].capitalize()} of {WORDS[int(drift.n)]} slipped through." in README
    assert f"**Rare account pairs: {pair.recall:.2f} recall.**" in README
    assert f"**JET-12 produces {false_flags} of the {m['false_positives']} false positives.**" in README
    assert m["false_positives"] - false_flags == 1
    assert f"exactly **one** false positive across {m['population']:,} entries" in README
    circular = len(AnomalyType.ALL) - len(evaluate.NON_CIRCULAR_ARCHETYPES)
    assert f"For {WORDS[circular]} of the {WORDS[len(AnomalyType.ALL)]} anomaly archetypes" in README
    assert f"{WORDS[circular]} of the {WORDS[len(AnomalyType.ALL)]} archetypes are injected by the definition" in README
    assert f"over {len(FEATURE_COLUMNS)} engineered per-entry features" in README
    _, report = score_ledger(ledger)
    names = ", ".join(f"`{column}`" for column in report.dropped_constant)
    assert f"{WORDS[len(report.dropped_constant)].capitalize()} features ({names}) are constant" in README
    budget = inspect.signature(combine).parameters["model_top_pct"].default
    assert f"a budget (the top {budget:.0%} by rank, ties included)" in README

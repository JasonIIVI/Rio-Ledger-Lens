"""The README quotes numbers. Each one here is computed from the default ledger, or from a
replay of the recorded QuickBooks fixtures, and then looked for in the README's text, so a
figure that moves fails a test instead of going stale on the page.

It checks that the figures are the measured ones. It cannot check that they mean what a
reader takes them to mean: that is what the caveat beside each of them is for.
"""

import re
from pathlib import Path

import pytest

from ledgerlens import evaluate, jets
from ledgerlens.model import combine, score_ledger

README = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").split())


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

"""docs/limitations.md quotes the one held-out measurement as `test` and `score` printed it, and
the README repeats its figures. This regenerates the measured ledger (HELD_OUT_SEED, pinned in
test_generate) and runs both commands again on whatever machine runs the tests: a platform or a
dependency that reads it differently fails here, and its figures would be re-measured ones, not
the held-out measurement (CLAUDE.md, rule 10)."""

import contextlib
import io
import re
from pathlib import Path

import pytest

from ledgerlens import evaluate, jets
from ledgerlens.cli import main
from ledgerlens.ingest import prepare
from ledgerlens.model import combine, score_ledger
from ledgerlens.schema import AnomalyType, HeldOutType

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")
README = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())


def _printed(command):
    """The text block the document pastes after the line naming ``command``."""
    pattern = re.escape(f"`ledgerlens {command} data/heldout-ledger.csv --labels data/heldout-labels.csv`")
    match = re.search(pattern + r"[^`]*?\n\n```text\n(.*?)\n```", DOC, re.S)
    assert match, command
    return match.group(1)


@pytest.fixture(scope="module")
def rerun(held_out_raw, tmp_path_factory):
    lines, labels = held_out_raw
    folder = tmp_path_factory.mktemp("held-out")
    lines.to_csv(folder / "heldout-ledger.csv", index=False)  # as `generate` writes them
    labels.to_csv(folder / "heldout-labels.csv", index=False)
    out = {}
    for command in ("test", "score"):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main([command, str(folder / "heldout-ledger.csv"),
                         "--labels", str(folder / "heldout-labels.csv")])
        assert code == 0, command
        out[command] = buffer.getvalue().rstrip("\n")
    return out


def test_the_measurement_prints_again_as_it_was_printed(rerun):
    assert rerun["test"] == _printed("test")
    assert rerun["score"] == _printed("score")


@pytest.fixture(scope="module")
def measured(held_out_raw):
    lines, labels = held_out_raw
    df = prepare(lines.copy())
    flags = jets.run_all(df)
    scores, _ = score_ledger(df)
    combined = combine(jets.score_entries(df, flags), scores)
    return {
        "recall": evaluate.recall_by_archetype(flags, labels).set_index("anomaly_type"),
        "budget": evaluate.budget_recall_by_archetype(combined, labels).set_index("anomaly_type"),
        "held": evaluate.held_out_budget(combined, labels),
        "tiers": evaluate.compare_tiers(combined, labels).set_index("segment"),
        "metrics": evaluate.evaluate(flags, labels, df["entry_id"].unique()),
    }


def test_the_tables_carry_the_measured_figures(measured):
    budget, held = measured["budget"], measured["held"]
    for kind in HeldOutType.ALL:
        row = budget.loc[kind]
        cells = (f"| `{kind}` | {int(row.n)} | {int(row.rule_caught)} | {int(row.model_in_budget)} "
                 f"| {row.chance:.2f} |")
        assert cells in DOC and cells in README, kind
    total = f"| all held-out | {held['n']} | 0 | {held['in_budget']} | {held['chance']:.2f} |"
    assert total in DOC and total in README
    assert budget.loc[list(HeldOutType.ALL), "rule_caught"].sum() == 0


def test_the_reading_is_the_registered_one(measured):
    """At least half of an archetype's entries in the budget: the model catches it."""
    budget, held = measured["budget"], measured["held"]
    caught = [k for k in HeldOutType.ALL if budget.loc[k, "model_in_budget"] * 2 >= budget.loc[k, "n"]]
    assert caught == ["token_amount"]
    assert "By the registered reading (at least half of an archetype's" in " ".join(DOC.split())
    for text in (" ".join(DOC.split()), README):
        assert (f"holds **{held['in_budget']} of the {held['n']}**" in text
                or f"holds {held['in_budget']} of the {held['n']} held-out entries" in text)
        assert f"against {held['chance']:.2f} by chance" in text
        assert f"{held['lift']:.1f} times chance, a recall of {held['recall']:.2f}" in text
        assert (f"{held['budget']} of {held['population']:,} entries (the top 2% by rank)" in text
                or f"{held['budget']} of the {held['population']:,} entries (the top 2% by rank)" in text)
    assert held["in_budget"] == budget.loc["token_amount", "model_in_budget"] == 6  # all six


def test_the_eleven_and_the_segments_are_the_measured_ones(measured):
    recall = measured["recall"].loc[list(AnomalyType.ALL)]
    caught, n = int(recall["caught"].sum()), int(recall["n"].sum())
    doc = " ".join(DOC.split())
    for text in (doc, README):
        assert f"{caught} of {'the ' if text is README else 'their '}{n}" in text
        assert f"({caught / n:.3f})" in text
    m = measured["metrics"]
    assert f"The overall recall `test` prints, {m['recall']:.3f}" in doc
    assert f"Its precision, {m['precision']:.3f}" in doc
    tiers = measured["tiers"]
    for segment, row in tiers.iterrows():
        assert f"{segment}: {int(row.entries):,} entries, {int(row.true_anomalies)} anomalies" in doc, segment

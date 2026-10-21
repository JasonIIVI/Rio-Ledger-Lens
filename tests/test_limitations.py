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


#: `test` prints these two tables sorted on one key with pandas' default sort, which is not
#: stable: rows that tie print in another order on CI's Linux runners than on the Mac that
#: made the measurement. Found after the freeze and disclosed in docs/limitations.md; the code
#: is not changed until the measurement is merged (rule 10).
TIED_TABLES = ("Recall by archetype:", "Precision by test:")


def _ties_unordered(text):
    """``text`` with the data rows of each table in TIED_TABLES sorted; everything else as it is.

    A table is its title line, its header line, and the rows up to the next blank line.
    """
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        out.append(lines[i])
        if lines[i] in TIED_TABLES:
            out.append(lines[i + 1])  # the header keeps its place
            end = i + 2
            while end < len(lines) and lines[end].strip():
                end += 1
            out.extend(sorted(lines[i + 2:end]))
            i = end
            continue
        i += 1
    return "\n".join(out)


def test_the_tie_order_is_set_aside_in_the_two_tables_and_nowhere_else():
    printed = _printed("test")
    assert all(title in printed.split("\n") for title in TIED_TABLES)
    swapped = printed.replace("JET-01 ", "JET-XX ").replace("JET-02 ", "JET-01 ").replace("JET-XX ", "JET-02 ")
    assert swapped != printed and _ties_unordered(swapped) != _ties_unordered(printed)  # a value moved
    tied = printed.split("\n")
    start = tied.index("Recall by archetype:") + 2
    tied[start], tied[start + 1] = tied[start + 1], tied[start]  # two rows of a tied table swapped
    assert _ties_unordered("\n".join(tied)) == _ties_unordered(printed)
    attribution = printed.split("\n")
    start = attribution.index("Caught by the test written for each archetype, or by others:") + 2
    attribution[start], attribution[start + 1] = attribution[start + 1], attribution[start]
    assert _ties_unordered("\n".join(attribution)) != _ties_unordered(printed)  # its order is fixed


def test_the_measurement_prints_again_as_it_was_printed(rerun):
    assert _ties_unordered(rerun["test"]) == _ties_unordered(_printed("test"))
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

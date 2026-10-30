"""docs/tuning.md quotes a sweep of every test that has a setting. Each row is computed here on
the default ledger and looked for in the document, so a figure that moves fails a test instead of
going stale. The sweep changes no default: it shows what the default ledger can and cannot say
about each one."""

import inspect
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ledgerlens import evaluate, jets
from ledgerlens.benford import benford_test, mad_conformity

DOC = " ".join((Path(__file__).resolve().parents[1] / "docs" / "tuning.md").read_text(encoding="utf-8").split())

#: test id -> (the setting, the values swept, a runner); the default is what the function declares
SWEEPS = {
    "JET-01": ("min_amount", (250, 500, 1_000, 5_000, 10_000),
               lambda df, v: jets.jet_round_amount(df, min_amount=v)),
    "JET-03": ("start_hour, end_hour", ((4, 22), (5, 21), (6, 20), (7, 19), (8, 18)),
               lambda df, v: jets.jet_after_hours(df, *v)),
    "JET-05": ("days", (1, 2, 3, 4, 5),
               lambda df, v: jets.jet_period_end_manual_revenue(df, days=v)),
    "JET-06": ("window", (0.01, 0.02, 0.03, 0.05, 0.10),
               lambda df, v: jets.jet_just_under_threshold(df, window=v)),
    "JET-07": ("days", (1, 3, 7, 14, 30),
               lambda df, v: jets.jet_duplicate_entries(df, days=v)),
    "JET-08": ("max_occurrences", (1, 2, 3, 5, 10),
               lambda df, v: jets.jet_rare_account_pair(df, max_occurrences=v)),
    "JET-09": ("dormant_days", (30, 60, 90, 120, 180),
               lambda df, v: jets.jet_dormant_account(df, dormant_days=v)),
    "JET-10": ("tolerance", (0.001, 0.005, 0.01, 0.1, 1.0),
               lambda df, v: jets.jet_unbalanced_entry(df, tolerance=v)),
    "JET-11": ("z_threshold", (2.5, 3.0, 3.5, 4.0, 4.5, 5.0),
               lambda df, v: jets.jet_large_value_outlier(df, z_threshold=v)),
}

FUNCTIONS = {test_id: fn for test_id, (_, fn) in jets.REGISTRY.items()}


def _default(test_id, setting):
    params = inspect.signature(FUNCTIONS[test_id]).parameters
    names = [name.strip() for name in setting.split(",")]
    values = tuple(params[name].default for name in names)
    return values if len(values) > 1 else values[0]


def _cell(value):
    return "{}-{}".format(*value) if isinstance(value, tuple) else f"{value:g}"


def row_text(test_id, setting, row):
    """One table row as docs/tuning.md writes it, the default in bold."""
    value = _cell(row.value)
    if row.value == _default(test_id, setting):
        value = f"**{value}**"
    precision = "—" if row.entries_flagged == 0 else f"{row.precision:.2f}"
    caught = "—" if row.intended_n is None else f"{int(row.intended_caught)} of {int(row.intended_n)}"
    return (f"| {value} | {row.flags_raised} | {row.entries_flagged} | {row.true_positives} "
            f"| {precision} | {caught} |")


@pytest.fixture(scope="module")
def swept(ledger, labels):
    return {test_id: evaluate.sweep(lambda v, run=run: run(ledger, v), values, labels, test_id)
            for test_id, (_, values, run) in SWEEPS.items()}


def test_every_test_with_a_setting_is_swept_at_its_default():
    for test_id, (setting, values, _) in SWEEPS.items():
        assert _default(test_id, setting) in values, test_id
    # JET-02 and JET-04 read the calendar, JET-12 the approver list (a company fact, not a
    # threshold); JET-06's limit is the company's too, so only its window is swept
    assert set(FUNCTIONS) - set(SWEEPS) == {"JET-02", "JET-04", "JET-12"}


def test_every_swept_row_is_in_the_tuning_notes(swept):
    for test_id, (setting, _, _) in SWEEPS.items():
        assert f"## {test_id}" in DOC or f"| {test_id} |" in DOC or f"### {test_id}" in DOC, test_id
        for row in swept[test_id].itertuples():
            assert row_text(test_id, setting, row) in DOC, (test_id, row_text(test_id, setting, row))


def test_the_sweep_counts_what_it_says(ledger, labels):
    table = evaluate.sweep(lambda v: jets.jet_dormant_account(ledger, dormant_days=v), (120,),
                           labels, "JET-09")
    flags = jets.jet_dormant_account(ledger)
    row = table.iloc[0]
    assert row.flags_raised == len(flags) and row.entries_flagged == flags["entry_id"].nunique()
    no_intent = evaluate.sweep(lambda v: jets.jet_large_value_outlier(ledger, z_threshold=v), (3.5,),
                               labels, "JET-11")
    assert no_intent.iloc[0].intended_n is None


def _benford_draws(rng, n, draws):
    """What Nigrini's bands read for ``draws`` samples of ``n`` amounts that follow the law."""
    mads = [benford_test(pd.Series(100 * 10 ** rng.random(n)))["mad"] for _ in range(draws)]
    return float(np.mean(mads)), Counter(mad_conformity(m) for m in mads)


def _pct(count, draws):
    return f"{round(100 * count / draws)}%"


def test_the_benford_floor_simulation_is_the_one_the_notes_quote():
    """The floor is justified by amounts drawn from Benford's law itself; every figure the notes
    give for it is recomputed here, from the seed and the order of draws they name."""
    rng = np.random.default_rng(20261030)
    mean, bands = _benford_draws(rng, 297, 5000)
    assert (f"297 at a time (the size of the recorded sandbox pull), give a mean MAD of {mean:.4f}, "
            f"and {_pct(bands['nonconformity'], 5000)} of those draws fall in \"nonconformity\" and "
            f"{_pct(bands['close conformity'], 5000)} in \"close conformity\"") in DOC
    assert "5,000 samples of 297" in DOC
    mean, bands = _benford_draws(rng, 1000, 2000)
    assert (f"At 1,000 amounts drawn from the law the mean MAD is {mean:.4f} and "
            f"{_pct(bands['close conformity'], 2000)} of 2,000 draws read \"close conformity\"") in DOC
    mean, bands = _benford_draws(rng, 10170, 500)
    assert bands == Counter({"close conformity": 500})
    assert f"at the default ledger's 10,170 it is {mean:.4f}, and all 500 draws read close" in DOC

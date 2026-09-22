"""Measure how well the tests actually perform against known ground truth.

This module is the reason the project generates its own data. Any tool can
produce a list of flags; the question a reviewer (or an interviewer) should
ask is how many of those flags were worth opening, and how much the tool
missed. Without labels neither number can be computed.

Definitions used here, at the *entry* level:

- true positive: an entry that was injected as an anomaly and got flagged
- false positive: an entry that was flagged but was ordinary
- false negative: an injected anomaly that no test caught

Precision answers "when it speaks up, is it right?". Recall answers "how much
does it miss?". For audit work recall usually matters more - a missed
misstatement is worse than a wasted half hour - but precision is what decides
whether anyone keeps using the tool.
"""

from __future__ import annotations

import pandas as pd


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def evaluate(
    flags: pd.DataFrame,
    labels: pd.DataFrame,
    all_entry_ids: pd.Series | None = None,
) -> dict[str, float]:
    """Overall precision, recall and F1 at the entry level."""
    flagged = set(flags["entry_id"]) if not flags.empty else set()
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])

    population = set(all_entry_ids) if all_entry_ids is not None else set(labels["entry_id"])

    tp = len(flagged & truth)
    fp = len(flagged - truth)
    fn = len(truth - flagged)
    tn = len(population - flagged - truth)

    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)

    return {
        "population": len(population),
        "true_anomalies": len(truth),
        "flagged": len(flagged),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "flag_rate": round(_safe_div(len(flagged), len(population)), 4),
    }


def recall_by_archetype(flags: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Which kinds of anomaly get caught, and which slip through.

    The aggregate recall number hides the useful detail. A tool that catches
    every weekend posting and no revenue cut-off issue has the same headline
    recall as one with the opposite profile, and they are not equally useful.
    """
    flagged = set(flags["entry_id"]) if not flags.empty else set()
    anomalies = labels[labels["is_anomaly"]].copy()
    if anomalies.empty:
        return pd.DataFrame(columns=["anomaly_type", "n", "caught", "missed", "recall"])

    anomalies["caught"] = anomalies["entry_id"].isin(flagged)
    out = anomalies.groupby("anomaly_type").agg(
        n=("entry_id", "count"),
        caught=("caught", "sum"),
    ).reset_index()
    out["missed"] = out["n"] - out["caught"]
    out["recall"] = (out["caught"] / out["n"]).round(4)
    return out.sort_values("recall").reset_index(drop=True)


def precision_by_test(flags: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """How trustworthy each individual test is.

    A test with low precision is not necessarily a bad test - JET-12 is
    expected to fire on legitimate entries, because it describes the control
    environment rather than the entry. But a reviewer deserves to know which
    is which before they spend an afternoon on a queue.
    """
    if flags.empty:
        return pd.DataFrame(columns=["test_id", "test_name", "flags", "true_positives",
                                     "precision"])
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    work = flags.drop_duplicates(subset=["test_id", "entry_id"]).copy()
    work["hit"] = work["entry_id"].isin(truth)

    out = work.groupby(["test_id", "test_name"]).agg(
        flags=("entry_id", "count"),
        true_positives=("hit", "sum"),
    ).reset_index()
    out["precision"] = (out["true_positives"] / out["flags"]).round(4)
    return out.sort_values("precision", ascending=False).reset_index(drop=True)


def format_report(metrics: dict[str, float]) -> str:
    """A short text block suitable for a console run or a CI log."""
    return (
        "Population           {population:,} entries\n"
        "Injected anomalies   {true_anomalies}\n"
        "Flagged              {flagged} ({flag_rate:.2%} of population)\n"
        "  true positives     {true_positives}\n"
        "  false positives    {false_positives}\n"
        "  false negatives    {false_negatives}\n"
        "Precision            {precision:.3f}\n"
        "Recall               {recall:.3f}\n"
        "F1                   {f1:.3f}"
    ).format(**metrics)

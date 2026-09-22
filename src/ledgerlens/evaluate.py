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


def compare_tiers(
    combined: pd.DataFrame,
    labels: pd.DataFrame,
) -> pd.DataFrame:
    """How each tier performs alone, and what their overlap looks like.

    The honest question this answers: is the model earning its place? On a
    population whose anomalies were designed as rule violations, the answer is
    "partly" - and saying so is worth more than quietly reporting the combined
    number.
    """
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    total_anomalies = len(truth)

    rows = []
    for name, mask in (
        ("rules only", combined["rule_flag"] & ~combined["model_flag"]),
        ("model only", ~combined["rule_flag"] & combined["model_flag"]),
        ("both", combined["rule_flag"] & combined["model_flag"]),
        ("neither", ~combined["rule_flag"] & ~combined["model_flag"]),
    ):
        group = combined[mask]
        ids = set(group["entry_id"])
        hits = len(ids & truth)
        rows.append({
            "segment": name,
            "entries": len(group),
            "true_anomalies": hits,
            "precision": round(_safe_div(hits, len(group)), 4),
            "share_of_all_anomalies": round(_safe_div(hits, total_anomalies), 4),
        })
    return pd.DataFrame(rows)


def model_lift(
    model_scores: pd.Series,
    labels: pd.DataFrame,
    tops=(25, 50, 100, 200),
) -> pd.DataFrame:
    """Precision among the top-N entries the model considers most unusual.

    Compared against the base rate, this is the clearest statement of whether
    the model is better than opening entries at random.
    """
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    base_rate = _safe_div(len(truth), len(model_scores))
    ordered = model_scores.sort_values(ascending=False)

    rows = []
    for n in tops:
        if n > len(ordered):
            continue
        top = set(ordered.head(n).index)
        hits = len(top & truth)
        precision = _safe_div(hits, n)
        rows.append({
            "top_n": n,
            "true_anomalies": hits,
            "precision": round(precision, 4),
            "base_rate": round(base_rate, 4),
            "lift_vs_random": round(_safe_div(precision, base_rate), 1),
        })
    return pd.DataFrame(rows)


def score_by_archetype(model_scores: pd.Series, labels: pd.DataFrame) -> pd.DataFrame:
    """Mean model score per anomaly type, against the normal baseline.

    Shows which patterns the unsupervised tier can actually perceive. Entry-level
    features cannot see a duplicate - that only exists by comparison with another
    entry - so a low score there is a design consequence, not a failure.
    """
    anomalies = labels[labels["is_anomaly"]]
    normal_ids = labels.loc[~labels["is_anomaly"], "entry_id"]
    baseline = model_scores[model_scores.index.isin(set(normal_ids))].mean()

    rows = []
    for archetype, group in anomalies.groupby("anomaly_type"):
        ids = [i for i in group["entry_id"] if i in model_scores.index]
        if not ids:
            continue
        mean_score = float(model_scores.loc[ids].mean())
        rows.append({
            "anomaly_type": archetype,
            "n": len(ids),
            "mean_model_score": round(mean_score, 4),
            "vs_normal": round(mean_score - float(baseline), 4),
        })
    out = pd.DataFrame(rows).sort_values("mean_model_score", ascending=False)
    return out.reset_index(drop=True)

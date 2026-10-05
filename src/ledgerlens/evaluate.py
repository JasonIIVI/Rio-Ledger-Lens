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

from .schema import AnomalyType

#: The archetypes the generator does not inject by the very rule a test checks,
#: so their recall is a measurement rather than the definition read back.
NON_CIRCULAR_ARCHETYPES = (AnomalyType.BENFORD_DRIFT, AnomalyType.RARE_ACCOUNT_PAIR)

#: Printed beside every rule-tier precision or recall the tool shows: `test`,
#: `summary`, the dashboard, the workpaper and CI's detection gate. It carries no
#: figure: the numbers beside it move with the ledger, the reason to distrust
#: them does not.
DETECTION_CAVEAT = (
    "Read these sceptically. For nine of eleven archetypes the generator injects the anomaly "
    "using the same definition the test looks for, so recall on those is close to "
    "tautological. The honest figures are the archetypes where detection is not definitional: "
    "benford_drift and rare_account_pair."
)

#: The same caveat for the model tier's segment and lift tables: `score` and the
#: dashboard's tier tab.
MODEL_TIER_CAVEAT = (
    "The same circularity applies to the model tier: these anomalies were defined as rule "
    "violations, so almost everything the model ranks highly the rules had already caught. "
    "Read the lift as re-ranking of the rule tier's queue, not as independent detection."
)


class LabelsMismatchError(ValueError):
    """A label file that does not describe the ledger it was given with."""


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _example(value: object) -> str:
    """An id as it is quoted in a refusal: ``repr``, cut to a line's worth.

    Whoever wrote the ledger chose this text, and the refusal is shown in a
    terminal, a CI log and the dashboard. ``repr`` escapes control and invisible
    characters, and it shows what the eye would miss: a stray space, or an id
    read as a number beside the same id read as text.
    """
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def check_labels(labels: pd.DataFrame, all_entry_ids) -> None:
    """Refuse labels that do not cover exactly the ledger's entries.

    A ledger entry with no label becomes a false positive the moment it is
    flagged, and a label with no entry an anomaly nobody could have caught:
    either way the precision and recall printed would describe a pairing that
    does not exist. Ids are compared as the values ``evaluate`` joins on, not
    as their text, so an id read as a number does not pass for the same id
    read as text. This can show that a file does not match, never that it
    belongs: generated ids are sequential, so two runs of the generator can
    share every id, and a label file carries no digest of its ledger.
    """
    if "entry_id" not in labels.columns:
        raise LabelsMismatchError("the label file has no entry_id column")
    ids = labels["entry_id"]
    blank = int(ids.isna().sum())
    if blank:
        raise LabelsMismatchError(
            "the label file has {} with no entry id".format(_count(blank, "row", "rows")))
    repeated = sorted(set(ids[ids.duplicated()]), key=repr)
    if repeated:
        # recall_by_archetype counts rows, so a repeated id is counted twice
        raise LabelsMismatchError(
            "the label file lists {} more than once (e.g. {})".format(
                _count(len(repeated), "entry id", "entry ids"), _example(repeated[0])))
    labelled, wanted = set(ids), set(all_entry_ids)
    # key=repr: ids of two types cannot be ordered against each other
    unlabelled = sorted(wanted - labelled, key=repr)
    unknown = sorted(labelled - wanted, key=repr)
    problems = []
    if unlabelled:
        problems.append("{} no label (e.g. {})".format(
            _count(len(unlabelled), "ledger entry has", "ledger entries have"),
            _example(unlabelled[0])))
    if unknown:
        problems.append("{} no ledger entry (e.g. {})".format(
            _count(len(unknown), "label names", "labels name"), _example(unknown[0])))
    if problems:
        raise LabelsMismatchError(
            "the labels do not cover exactly this ledger's entries: " + "; ".join(problems))


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
    # The columns are named so that labels marking no anomaly give an empty table, as
    # recall_by_archetype does, and not a frame with nothing to sort by.
    columns = ["anomaly_type", "n", "mean_model_score", "vs_normal"]
    out = pd.DataFrame(rows, columns=columns).sort_values("mean_model_score", ascending=False)
    return out.reset_index(drop=True)

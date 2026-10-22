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

from .ingest import plural, quote
from .schema import AnomalyType, HeldOutType

#: The test written to find each archetype, in the generator's order. Benford drift
#: has none: Benford analysis is reported beside the queue, never raised as a flag.
INTENDED_TEST = {
    AnomalyType.ROUND_AMOUNT: "JET-01",
    AnomalyType.WEEKEND_ENTRY: "JET-02",
    AnomalyType.AFTER_HOURS_ENTRY: "JET-03",
    AnomalyType.HOLIDAY_ENTRY: "JET-04",
    AnomalyType.PERIOD_END_MANUAL_REVENUE: "JET-05",
    AnomalyType.JUST_UNDER_THRESHOLD: "JET-06",
    AnomalyType.DUPLICATE_ENTRY: "JET-07",
    AnomalyType.RARE_ACCOUNT_PAIR: "JET-08",
    AnomalyType.DORMANT_ACCOUNT: "JET-09",
    AnomalyType.BENFORD_DRIFT: None,
    AnomalyType.UNBALANCED_ENTRY: "JET-10",
}

#: The archetypes the generator does not inject by exactly the definition their test
#: checks (benford_drift has none), so which test catches them depends on the draw. On
#: the default ledger the test written for them catches half of them or fewer and tests
#: written for something else catch most of the others; on another seed JET-08 can catch every rare
#: pair. Their recall is incidental either way: not the definition read back, and not a
#: measure of detection.
NOT_DEFINITIONAL = (
    AnomalyType.BENFORD_DRIFT,
    AnomalyType.DORMANT_ACCOUNT,
    AnomalyType.RARE_ACCOUNT_PAIR,
)

#: Printed beside every rule-tier precision or recall the tool shows: `test`,
#: `score` (whose segment table's "rules only" and "both" rows are the rule tier's
#: flagged entries), `summary`, the dashboard, the workpaper and CI's detection
#: gate. It carries no figure: the numbers beside it move with the ledger, the
#: reason to distrust them does not.
DETECTION_CAVEAT = (
    "Read these sceptically. For eight of eleven archetypes the generator injects the anomaly "
    "using the same definition the test looks for, so recall on those is close to "
    "tautological. The other three, benford_drift, dormant_account and rare_account_pair, are "
    "not injected by exactly that definition, so which test catches them depends on the draw: "
    "on the default ledger, mostly tests written for something else. Their recall says how "
    "they happened to be injected, not how well anything detects them."
)

#: The same caveat for the model tier's segment and lift tables: `score` and the
#: dashboard's tier tab, where it follows DETECTION_CAVEAT ("the same" points at it).
MODEL_TIER_CAVEAT = (
    "The same circularity applies to the model tier: these anomalies were defined as rule "
    "violations, so almost every anomaly the model ranks highly, the rules had already caught. "
    "Read the lift as re-ranking of the rule tier's queue, not as independent detection. Only "
    "the held-out archetypes, which no rule describes, measure it as a detector: how many of "
    "them its budget holds, against chance."
)


class LabelsMismatchError(ValueError):
    """A label file that does not describe the ledger it was given with."""


# One quoting rule for every value a message takes from a file (ingest.quote).
_count, _example = plural, quote


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
    # A tie on recall goes by name. On recall alone pandas' default sort, which is not stable,
    # left tied rows in another order on CI's Linux runners than on a Mac.
    return out.sort_values(["recall", "anomaly_type"]).reset_index(drop=True)


def attribution(flags: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Which tests caught each archetype: the one written for it, or others.

    Recall cannot tell a test doing its job from one written for something else
    that happened to fire. ``by_other_tests`` counts the entries any other test
    flagged, whether or not the intended one did too; ``only_by_other_tests``
    the caught entries the intended test missed. ``intended_test`` is "" where
    no test was written for the type. Rows: held-out archetypes, then the
    generator's, then any other type by name, as in the budget table.
    """
    columns = ["anomaly_type", "intended_test", "n", "caught", "by_intended_test",
               "by_other_tests", "only_by_other_tests", "missed"]
    anomalies = labels[labels["is_anomaly"]]
    if flags.empty:  # no flags may also mean no columns
        flags = pd.DataFrame(columns=["entry_id", "test_id"])
    rows = []
    for archetype in _archetype_order(anomalies["anomaly_type"]):
        ids = set(anomalies.loc[anomalies["anomaly_type"] == archetype, "entry_id"])
        intended = INTENDED_TEST.get(archetype) or ""
        hit = flags[flags["entry_id"].isin(ids)]
        caught = set(hit["entry_id"])
        mine = set(hit.loc[hit["test_id"] == intended, "entry_id"]) if intended else set()
        other = set(hit.loc[hit["test_id"] != intended, "entry_id"])
        rows.append({
            "anomaly_type": archetype,
            "intended_test": intended,
            "n": len(ids),
            "caught": len(caught),
            "by_intended_test": len(mine),
            "by_other_tests": len(other),
            "only_by_other_tests": len(caught - mine),
            "missed": len(ids - caught),
        })
    return pd.DataFrame(rows, columns=columns)


def sweep(run, values, labels: pd.DataFrame, test_id: str | None = None) -> pd.DataFrame:
    """One test at several settings: what it raises there, and how much of that is anomalous.

    ``run(value)`` gives the test's flags at that setting; the caller runs the test, so
    detection never sees the labels (rule 2). Precision is per entry, as everywhere
    else. When ``test_id`` is the test written for an archetype (``INTENDED_TEST``),
    ``intended_caught`` counts that archetype's entries it flags, of
    ``intended_n``; otherwise both are None.
    """
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    archetype = next((a for a, t in INTENDED_TEST.items() if t and t == test_id), None)
    intended = (set(labels.loc[labels["anomaly_type"] == archetype, "entry_id"])
                if archetype else None)
    rows = []
    for value in values:
        flags = run(value)
        flagged = set(flags["entry_id"]) if not flags.empty else set()
        rows.append({
            "value": value,
            "flags_raised": len(flags),
            "entries_flagged": len(flagged),
            "true_positives": len(flagged & truth),
            "precision": round(_safe_div(len(flagged & truth), len(flagged)), 4),
            "intended_caught": None if intended is None else len(flagged & intended),
            "intended_n": None if intended is None else len(intended),
        })
    return pd.DataFrame(rows)


def precision_by_test(flags: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """How trustworthy each individual test is.

    A test with low precision is not necessarily a bad test - JET-12 is
    expected to fire on legitimate entries, because it describes the control
    environment rather than the entry. But a reviewer deserves to know which
    is which before they spend an afternoon on a queue.
    """
    if flags.empty:
        return pd.DataFrame(columns=["test_id", "test_name", "entries_flagged", "true_positives",
                                     "precision"])
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    work = flags.drop_duplicates(subset=["test_id", "entry_id"]).copy()
    work["hit"] = work["entry_id"].isin(truth)

    # Entries, not flags: a test that flags two lines of one entry (JET-09, JET-11) counts the
    # entry once here, while `ledgerlens test` prints the flags it raised beside this table.
    out = work.groupby(["test_id", "test_name"]).agg(
        entries_flagged=("entry_id", "count"),
        true_positives=("hit", "sum"),
    ).reset_index()
    out["precision"] = (out["true_positives"] / out["entries_flagged"]).round(4)
    # A tie on precision goes by test id, for the reason recall_by_archetype gives.
    return out.sort_values(["precision", "test_id"], ascending=[False, True]).reset_index(drop=True)


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

    Ties at the cut go to the lower entry id, so a top-N depends neither on
    the order the scores arrive in nor on how a sort orders a tie.
    """
    truth = set(labels.loc[labels["is_anomaly"], "entry_id"])
    base_rate = _safe_div(len(truth), len(model_scores))
    # Entries with identical features score identically, so a cut can fall
    # inside a tie. The default sort is not stable; this one is, after an index
    # sort, so such a tie goes to the lower entry id.
    ordered = model_scores.sort_index().sort_values(ascending=False, kind="mergesort")

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


def _archetype_order(present) -> list[str]:
    """Held-out archetypes, then the generator's, each in its own order, then any other by name."""
    present = set(present)
    known = [a for a in (*HeldOutType.ALL, *AnomalyType.ALL) if a in present]
    return known + sorted(present - set(known))


def budget_recall_by_archetype(combined: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    """Each archetype against the model tier's budget, and against chance.

    The model tier flags a fixed share of the entries by rank, its budget: B of P.
    An archetype of n entries puts n * B / P of them there by chance alone, so
    ``chance`` is the count to beat. Only an archetype no rule describes measures
    the model as a detector; ``rule_caught`` stands beside it to say which those
    are. Rows: held-out archetypes first, then the generator's, then any other.
    """
    columns = ["anomaly_type", "n", "rule_caught", "model_in_budget", "chance", "model_recall"]
    budget, population = int(combined["model_flag"].sum()), len(combined)
    tiers = combined.set_index("entry_id")[["rule_flag", "model_flag"]]
    anomalies = labels[labels["is_anomaly"]]
    rows = []
    for archetype in _archetype_order(anomalies["anomaly_type"]):
        ids = anomalies.loc[anomalies["anomaly_type"] == archetype, "entry_id"]
        hit = tiers.reindex(ids)  # an id the ledger lacks is in neither tier: eq(True) is False
        n, in_budget = len(ids), int(hit["model_flag"].eq(True).sum())
        rows.append({
            "anomaly_type": archetype,
            "n": n,
            "rule_caught": int(hit["rule_flag"].eq(True).sum()),
            "model_in_budget": in_budget,
            "chance": round(_safe_div(n * budget, population), 2),
            "model_recall": round(_safe_div(in_budget, n), 4),
        })
    return pd.DataFrame(rows, columns=columns)


def held_out_budget(combined: pd.DataFrame, labels: pd.DataFrame) -> dict | None:
    """The held-out archetypes together against the model's budget; None if the labels have none.

    ``lift`` is how many times chance the budget holds (0.0 when chance is nothing).
    """
    held = labels[labels["is_anomaly"] & labels["anomaly_type"].isin(HeldOutType.ALL)]
    if held.empty:
        return None
    budget, population = int(combined["model_flag"].sum()), len(combined)
    in_budget = int(combined.loc[combined["entry_id"].isin(set(held["entry_id"])), "model_flag"].sum())
    chance = _safe_div(len(held) * budget, population)
    return {
        "n": len(held),
        "in_budget": in_budget,
        "chance": round(chance, 2),
        "recall": round(_safe_div(in_budget, len(held)), 4),
        "lift": round(_safe_div(in_budget, chance), 1),
        "budget": budget,
        "population": population,
    }


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

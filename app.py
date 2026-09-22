"""Streamlit exception review dashboard.

The queue a reviewer actually works: sorted by risk, filterable, and every flag
shown with the reason that produced it. Run with:

    streamlit run app.py
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ledgerlens import evaluate, jets
from ledgerlens.benford import benford_test
from ledgerlens.ingest import load_csv, load_labels
from ledgerlens.model import combine, score_ledger

st.set_page_config(page_title="LedgerLens", layout="wide")

DATA = Path("data")


@st.cache_data(show_spinner=False)
def load(ledger_path: str, labels_path: str):
    df = load_csv(ledger_path)
    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)
    scores, report = score_ledger(df)
    combined = combine(scored, scores)
    labels = load_labels(labels_path) if Path(labels_path).exists() else None
    return df, flags, combined, scores, report, labels


st.title("LedgerLens")
st.caption("Journal entry testing and exception review. A flag is a question, not a finding.")

ledger_path = st.sidebar.text_input("Ledger CSV", str(DATA / "ledger.csv"))
labels_path = st.sidebar.text_input("Labels CSV (optional)", str(DATA / "labels.csv"))

if not Path(ledger_path).exists():
    st.warning("No ledger found. Run `ledgerlens generate` first.")
    st.stop()

df, flags, combined, scores, report, labels = load(ledger_path, labels_path)

# ---- headline numbers ----
c1, c2, c3, c4 = st.columns(4)
c1.metric("Entries", "{:,}".format(df["entry_id"].nunique()))
c2.metric("Flagged", int((combined["risk_score"] > 0).sum()))
c3.metric("Flags raised", len(flags))
c4.metric("Flag rate", "{:.2%}".format((combined["risk_score"] > 0).mean()))

tab_queue, tab_tiers, tab_benford, tab_quality = st.tabs(
    ["Exception queue", "Tier comparison", "Benford", "Detection quality"]
)

# ---- queue ----
with tab_queue:
    left, right = st.columns([1, 3])
    with left:
        severities = sorted(flags["severity"].unique()) if not flags.empty else []
        chosen_sev = st.multiselect("Severity", severities, default=severities)
        test_ids = sorted(flags["test_id"].unique()) if not flags.empty else []
        chosen_tests = st.multiselect("Test", test_ids, default=test_ids)
        agreements = sorted(combined["agreement"].unique())
        chosen_agree = st.multiselect("Tier agreement", agreements, default=agreements)
        min_amount = st.number_input("Minimum amount", value=0.0, step=1000.0)

    matching = flags[
        flags["severity"].isin(chosen_sev) & flags["test_id"].isin(chosen_tests)
    ] if not flags.empty else flags
    ids = set(matching["entry_id"])

    queue = combined[
        combined["entry_id"].isin(ids)
        & combined["agreement"].isin(chosen_agree)
        & (combined["entry_amount"] >= min_amount)
    ]

    with right:
        st.subheader(f"{len(queue):,} entries to review")
        st.dataframe(
            queue[["entry_id", "posting_date", "source", "created_by", "entry_amount",
                   "risk_score", "model_score", "agreement", "tests_fired"]],
            width="stretch", hide_index=True, height=340,
        )

    if not queue.empty:
        st.divider()
        picked = st.selectbox("Inspect entry", queue["entry_id"].tolist())
        row = combined[combined["entry_id"] == picked].iloc[0]

        a, b, c = st.columns(3)
        a.metric("Rule score", "{:.1f}".format(row["risk_score"]))
        b.metric("Model score", "{:.3f}".format(row["model_score"]))
        c.metric("Tier agreement", row["agreement"])

        st.markdown("**Why it was flagged**")
        for f in flags[flags["entry_id"] == picked].itertuples():
            st.markdown(f"- `{f.test_id}` **{f.test_name}** ({f.severity}) - {f.reason}")

        st.markdown("**Journal entry lines**")
        st.dataframe(
            df[df["entry_id"] == picked][
                ["line_no", "account_code", "account_name", "account_type",
                 "description", "debit", "credit"]
            ],
            width="stretch", hide_index=True,
        )

# ---- tiers ----
with tab_tiers:
    st.markdown(
        "The rule tier and the model tier answer different questions, so their scores are kept "
        "separate. The useful signal is where they disagree."
    )
    if labels is not None:
        st.dataframe(evaluate.compare_tiers(combined, labels),
                     width="stretch", hide_index=True)
        st.markdown("**Model lift over random selection**")
        st.dataframe(evaluate.model_lift(scores, labels),
                     width="stretch", hide_index=True)
        st.markdown("**Which anomaly types the model can perceive**")
        st.dataframe(evaluate.score_by_archetype(scores, labels),
                     width="stretch", hide_index=True)
        st.info(
            "Entry-level features cannot see a duplicate - that only exists by comparison with "
            "another entry - so a low score there is a design consequence, not a failure."
        )
    else:
        st.dataframe(combined["agreement"].value_counts().rename("entries"))
    st.code(report.describe(), language="text")

# ---- benford ----
with tab_benford:
    result = benford_test(df["abs_amount"], "population")
    a, b, c = st.columns(3)
    a.metric("MAD", "{:.5f}".format(result["mad"]))
    b.metric("Conformity", result["conformity"])
    c.metric("Chi-square", "{:.2f}".format(result["chi_square"]))

    chart = pd.DataFrame({
        "observed": pd.Series(result["observed_prop"]),
        "expected": pd.Series(result["expected_prop"]),
    })
    st.bar_chart(chart)
    st.caption(
        "Chi-square exceeds its critical value on almost any large population because its power "
        "grows with sample size. MAD with Nigrini's bands is the operative statistic. "
        "Non-conformity is a pointer, not a finding."
    )

# ---- quality ----
with tab_quality:
    if labels is None:
        st.info("Provide a labels CSV to see measured detection quality.")
    else:
        metrics = evaluate.evaluate(flags, labels, df["entry_id"].unique())
        a, b, c = st.columns(3)
        a.metric("Precision", "{:.3f}".format(metrics["precision"]))
        b.metric("Recall", "{:.3f}".format(metrics["recall"]))
        c.metric("F1", "{:.3f}".format(metrics["f1"]))
        st.warning(
            "Read these sceptically. For nine of eleven archetypes the generator injects the "
            "anomaly using the same definition the test looks for, so recall on those is close "
            "to tautological. The honest figures are the archetypes where detection is not "
            "definitional - see the table below."
        )
        st.markdown("**Recall by archetype**")
        st.dataframe(evaluate.recall_by_archetype(flags, labels),
                     width="stretch", hide_index=True)
        st.markdown("**Precision by test**")
        st.dataframe(evaluate.precision_by_test(flags, labels),
                     width="stretch", hide_index=True)

"""Streamlit exception review dashboard.

The queue a reviewer actually works: sorted by risk, filterable, every flag
shown with the reason that produced it, a Claude-written note beside each
entry, and the accept / dismiss / escalate decision recorded under the
reviewer's name. Run with:

    streamlit run app.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from ledgerlens import evaluate, jets
from ledgerlens.benford import benford_test
from ledgerlens.env import load_dotenv
from ledgerlens.ingest import load_csv, load_labels
from ledgerlens.model import combine, score_ledger
from ledgerlens.narrate import NarrativeError, Narrator, build_prompt, entry_context
from ledgerlens.review import DECISIONS, Decision, ReviewStore

load_dotenv()
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


def md(text: str) -> str:
    """Escape dollar signs: Streamlit renders ``$7,428.45 ... $7,284.88`` as LaTeX otherwise."""
    return str(text).replace("$", r"\$")


def render_narrative(narrative: dict) -> None:
    st.markdown(f"**{md(narrative['summary'])}**")
    st.markdown(md(narrative["why_flagged"]))
    st.markdown("Evidence to request:")
    for item in narrative["evidence_to_request"]:
        st.markdown(f"- {md(item)}")
    st.caption(
        "Control: {control} · Confidence: {confidence} · Written by {model} at {when} · "
        "note #{note_id}".format(
            control=narrative["suggested_control"], confidence=narrative["confidence"],
            model=narrative.get("model") or "unknown model",
            when=narrative.get("generated_at", ""),
            note_id=narrative.get("id", "?"),
        )
    )


st.title("LedgerLens")
st.caption("Journal entry testing and exception review. A flag is a question, not a finding.")

ledger_path = st.sidebar.text_input("Ledger CSV", str(DATA / "ledger.csv"), key="ledger_path")
labels_path = st.sidebar.text_input("Labels CSV (optional)", str(DATA / "labels.csv"),
                                    key="labels_path")
db_path = st.sidebar.text_input("Review database", str(DATA / "review.sqlite"), key="db_path")
reviewer = st.sidebar.text_input(
    "Reviewer", key="reviewer", help="Every decision is recorded under this name.",
).strip()

if not Path(ledger_path).exists():
    st.warning("No ledger found. Run `ledgerlens generate` first.")
    st.stop()

df, flags, combined, scores, report, labels = load(ledger_path, labels_path)

# The store is cheap to open and its reads are deliberately never cached: a
# decision recorded a second ago has to show on the very next rerun.
store = ReviewStore(db_path)
current = store.current()

# ---- headline numbers ----
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Entries", "{:,}".format(df["entry_id"].nunique()))
c2.metric("Flagged", int((combined["risk_score"] > 0).sum()))
c3.metric("Flags raised", len(flags))
c4.metric("Flag rate", "{:.2%}".format((combined["risk_score"] > 0).mean()))
c5.metric("Decided", len(current))
progress = store.summary()
if not progress.empty:
    st.caption("Review progress: " + ", ".join(
        f"{r.decision} {r.entries}" for r in progress.itertuples()
    ))

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
        outstanding_only = st.checkbox("Outstanding only", value=False, key="outstanding_only",
                                       help="Hide entries that already have a decision.")

    matching = flags[
        flags["severity"].isin(chosen_sev) & flags["test_id"].isin(chosen_tests)
    ] if not flags.empty else flags
    ids = set(matching["entry_id"])

    queue = combined[
        combined["entry_id"].isin(ids)
        & combined["agreement"].isin(chosen_agree)
        & (combined["entry_amount"] >= min_amount)
    ]
    if outstanding_only:
        queue = queue[~queue["entry_id"].isin(store.decided_ids())]
    queue = queue.merge(current[["entry_id", "decision", "reviewer"]], on="entry_id", how="left")

    with right:
        st.subheader(f"{len(queue):,} entries to review")
        st.dataframe(
            queue[["entry_id", "posting_date", "source", "created_by", "entry_amount",
                   "risk_score", "model_score", "agreement", "tests_fired", "decision",
                   "reviewer"]],
            width="stretch", hide_index=True, height=340,
        )

    if not queue.empty:
        st.divider()
        picked = st.selectbox("Inspect entry", queue["entry_id"].tolist(), key="picked")
        row = combined[combined["entry_id"] == picked].iloc[0]

        flash = st.session_state.pop("flash", None)
        if flash:
            st.success(flash)

        a, b, c = st.columns(3)
        a.metric("Rule score", "{:.1f}".format(row["risk_score"]))
        b.metric("Model score", "{:.3f}".format(row["model_score"]))
        c.metric("Tier agreement", row["agreement"])

        st.markdown("**Why it was flagged**")
        for f in flags[flags["entry_id"] == picked].itertuples():
            st.markdown(f"- `{f.test_id}` **{f.test_name}** ({f.severity}) - {md(f.reason)}")

        st.markdown("**Journal entry lines**")
        st.dataframe(
            df[df["entry_id"] == picked][
                ["line_no", "account_code", "account_name", "account_type",
                 "description", "debit", "credit"]
            ],
            width="stretch", hide_index=True,
        )

        # ---- narrative: advisory text, never a decision ----
        st.markdown("**Reviewer note** (written by Claude, advisory only)")
        narrative = store.get_narrative(picked)
        # The note the reviewer actually read is the one rendered on the
        # *previous* run of this script: a submit reruns everything, and a
        # version written in between (a rewrite, another reviewer) would
        # otherwise be recorded as the one they saw. So the id on screen is
        # remembered per entry, and read back before it is overwritten.
        shown_key = f"shown-{picked}"
        seen_id = st.session_state.get(shown_key)
        st.session_state[shown_key] = narrative["id"] if narrative else None
        if narrative:
            render_narrative(narrative)
        else:
            st.caption("No narrative yet for this entry.")
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if st.button("Rewrite narrative" if narrative else "Write narrative",
                     disabled=not has_key, key=f"narrate-{picked}"):
            try:
                with st.spinner("Asking Claude..."):
                    narrator = Narrator()
                    entry, entry_flags, entry_lines = entry_context(combined, flags, df, picked)
                    fresh = narrator.narrate_one(build_prompt(entry, entry_flags, entry_lines))
                note_id = store.save_narrative(picked, fresh, model=narrator.model)
                st.session_state["flash"] = (
                    f"Narrative written as note #{note_id} ({narrator.usage.describe()})."
                )
                st.rerun()
            except NarrativeError as exc:
                st.error(str(exc))
        if not has_key:
            st.caption("Set ANTHROPIC_API_KEY in .env to write narratives from here, or run "
                       "`ledgerlens narrate` from the command line.")

        # ---- decision: a named human, append-only ----
        st.markdown("**Record a decision**")
        with st.form(key=f"decision-{picked}", clear_on_submit=True):
            choice = st.radio("Decision", DECISIONS, horizontal=True,
                              format_func=str.capitalize)
            note = st.text_area("Note", placeholder="What you checked, or why this is fine.")
            submitted = st.form_submit_button("Record decision", disabled=not reviewer)
        if not reviewer:
            st.caption("Enter your name in the sidebar to record a decision.")
        if submitted:
            latest_id = narrative["id"] if narrative else None
            # A form submits once per click, and this guard absorbs a double click:
            # an append-only log should not carry an accidental duplicate.
            signature = (picked, choice, note.strip())
            if st.session_state.get("last_decision") == signature:
                st.warning("That decision was just recorded.")
            elif seen_id != latest_id:
                # The note changed between the render the reviewer read and
                # this submit. Recording the new id would claim they read it;
                # recording the old one would attach advice that is no longer
                # on screen. Neither is true, so nothing is recorded.
                st.warning(
                    "The reviewer note changed while you were reading it (now note "
                    f"#{latest_id if latest_id is not None else 'none'}). Read the note "
                    "shown above and record the decision again."
                )
            else:
                # The decision records the note that was on screen, so the
                # workpaper can show what the reviewer read even if the note
                # is rewritten later.
                store.record(Decision(
                    entry_id=picked, decision=choice, reviewer=reviewer, note=note.strip(),
                    risk_score=float(row["risk_score"]), model_score=float(row["model_score"]),
                    narrative_id=seen_id,
                ))
                st.session_state["last_decision"] = signature
                st.session_state["flash"] = f"Recorded: {choice} by {reviewer}."
                st.rerun()

        history = store.history(picked)
        if not history.empty:
            st.markdown("**Decision history** (append-only)")
            st.dataframe(history[["decided_at", "decision", "reviewer", "note", "narrative_id"]],
                         width="stretch", hide_index=True)

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

"""Streamlit exception review dashboard.

The queue a reviewer actually works: sorted by risk, filterable, every flag
shown with the reason that produced it, a Claude-written note beside each
entry, and the accept / dismiss / escalate decision recorded under the
reviewer's name. Run with:

    streamlit run app.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from ledgerlens import evaluate, jets
from ledgerlens.benford import benford_test
from ledgerlens.env import load_dotenv
from ledgerlens.ingest import identity_path, ledger_identity, load_csv, load_labels
from ledgerlens.model import combine, score_ledger
from ledgerlens.narrate import NarrativeError, Narrator, build_prompt, entry_context
from ledgerlens.review import DECISIONS, LEGACY_LEDGER_ID, Decision, ReviewStore
from ledgerlens.summary import md_code

load_dotenv()
st.set_page_config(page_title="LedgerLens", layout="wide")

DATA = Path("data")


def _stamp(path: str) -> tuple | None:
    """What a file is, beside where it is: its size and modification time, or None.

    ``load`` is cached by its arguments. Keyed by path alone, a label file corrected
    on disk kept showing the problem the first read found, and a regenerated ledger
    kept its old numbers, until the server was restarted. The key covers the three
    files ``load`` reads: the ledger, the labels and the ledger's identity sidecar.
    """
    try:
        status = os.stat(path)
    except (OSError, ValueError):  # no such file, or a name the filesystem rejects: load says so
        return None
    return (status.st_mtime_ns, status.st_size)


@st.cache_data(show_spinner=False)
def load(ledger_path: str, labels_path: str, stamps: tuple):
    """``stamps`` is part of the cache key and nothing else: see ``_stamp``."""
    df = load_csv(ledger_path)
    # Computed here so it is cached with the frame it describes: the store is
    # bound to the ledger on screen, never to a stale one.
    ledger_id = ledger_identity(df, ledger_path)
    flags = jets.run_all(df)
    scored = jets.score_entries(df, flags)
    scores, report = score_ledger(df)
    combined = combine(scored, scores)
    # Labels are scored only when they cover exactly this ledger's entries: the
    # sidebar's default file sits beside every ledger in data/, a QuickBooks pull
    # included, and numbers from a pairing that does not exist are worse than none.
    labels, labels_problem = None, None
    if labels_path:  # the field is optional: blank is none
        try:
            # The stat is inside the handler: a name the filesystem rejects (too long) is
            # a reason shown in the sidebar, as an unreadable file is; a directory is none.
            if Path(labels_path).is_file():
                labels = load_labels(labels_path)
                evaluate.check_labels(labels, df["entry_id"].unique())
        except KeyError as exc:
            labels, labels_problem = None, f"missing column {exc}"
        except (OSError, ValueError) as exc:  # unreadable, not a label file, or a mismatch
            labels, labels_problem = None, str(exc)
    return df, flags, combined, scores, report, labels, ledger_id, labels_problem


_MARKUP = re.compile(r"([!-/:-@\[-`{-~])")  # every ASCII punctuation character


def md(text: str) -> str:
    """``text`` as markdown that forms no construct of its own.

    The note is written from ledger cells and rendered as prose through
    ``st.markdown``. A backslash before each ASCII punctuation character keeps a
    heading, a list, an image, a link under chosen words, HTML and LaTeX
    (``$7,428.45 ... $7,284.88``) from forming. Two things Streamlit does to prose
    after the escapes are resolved remain: a bare URL or address is drawn as a
    link to itself, and ``->`` or ``--`` as an arrow or a dash (one version draws
    ``:smile:`` as an emoji). A flag's reason, which quotes ledger cells verbatim,
    is drawn as a code span instead (``summary.md_code``), where nothing acts and
    nothing is redrawn; checked in a browser on both Streamlit versions.
    """
    return _MARKUP.sub(r"\\\1", str(text))


def _ratio(value: float, defined: bool) -> str:
    """A ratio over nothing is not a figure, as ``summary`` says too."""
    return f"{value:.3f}" if defined else "undefined"


def render_narrative(narrative: dict) -> None:
    st.markdown(f"**{md(narrative['summary'])}**")
    st.markdown(md(narrative["why_flagged"]))
    st.markdown("Evidence to request:")
    for item in narrative["evidence_to_request"]:
        st.markdown(f"- {md(item)}")
    st.caption(
        "Control: {control} · Confidence: {confidence} · Written by {model} at {when} · "
        "note #{note_id}".format(
            control=md(narrative["suggested_control"]), confidence=md(narrative["confidence"]),
            model=md(narrative.get("model") or "unknown model"),
            when=narrative.get("generated_at", ""),
            note_id=narrative.get("id", "?"),
        )
    )


st.title("LedgerLens")
st.caption("Journal entry testing and exception review. A flag is a question, not a finding.")

ledger_path = st.sidebar.text_input("Ledger CSV", str(DATA / "ledger.csv"), key="ledger_path")
labels_path = st.sidebar.text_input("Labels CSV (optional)", str(DATA / "labels.csv"),
                                    key="labels_path").strip()  # a pasted trailing space is not "no labels"
db_path = st.sidebar.text_input("Review database", str(DATA / "review.sqlite"), key="db_path")
reviewer = st.sidebar.text_input(
    "Reviewer", key="reviewer", help="Every decision is recorded under this name.",
).strip()

if not Path(ledger_path).exists():
    st.warning("No ledger found. Run `ledgerlens generate` first.")
    st.stop()

try:
    df, flags, combined, scores, report, labels, ledger_id, labels_problem = load(
        ledger_path, labels_path,
        (_stamp(ledger_path), _stamp(labels_path), _stamp(str(identity_path(ledger_path)))))
except ValueError as exc:
    # A sidecar that names no ledger (never guess which one it is), a file that is not a
    # ledger, or a population the model cannot be fitted on: a message, not an exception page.
    st.error(str(exc))
    st.stop()
if labels_problem:
    st.sidebar.warning("Labels set aside; detection quality is not shown.")
    # As text, never markdown: the reason can quote an id from the ledger or the label file.
    st.sidebar.text(labels_problem)

# The store is cheap to open and its reads are deliberately never cached: a
# decision recorded a second ago has to show on the very next rerun. It is
# bound to this ledger's identity, so another ledger's notes never show here.
try:
    store = ReviewStore(db_path, ledger_id)
except RuntimeError as exc:  # a file from a newer version, or not a review database at all
    st.error(str(exc))
    st.stop()
st.sidebar.caption(f"Review rows keyed by `{ledger_id}`")
others = store.other_ledgers()
if not others.empty:
    # Surfaced, never shown as this ledger's: a file can hold several ledgers' review.
    st.sidebar.caption(f"This database also holds rows for {len(others)} other ledger(s): " + ", ".join(
        f"`{r.ledger_id[:16]}…` ({int(r.narratives)} narrated, {int(r.decisions)} decided)"
        for r in others.itertuples()))
# adopt_legacy copies only into a ledger with no rows, so on a file upgraded
# from before ledgers were keyed, the first note or decision written here would
# shut the legacy rows out for good, and `ledgerlens narrate` would then buy
# every note again. Writes therefore wait for a choice, as narrate's refusal
# does: adopt the rows from the command line, or start this ledger over. The
# choice is made for one file: keyed like every per-entry widget, it is asked
# again when the sidebar points at another database.
writes_blocked = False
if LEGACY_LEDGER_ID in set(others["ledger_id"]) and ledger_id not in set(store.ledgers()["ledger_id"]):
    st.sidebar.warning(
        "Rows from before review data was keyed by ledger sit under 'legacy', and this ledger "
        "has none yet. If they were written for it, adopt them first: `ledgerlens adopt-legacy "
        f"{ledger_path} --db {db_path}` (no API calls). A note or a decision recorded here first "
        "would make adopting them impossible, and narrating would buy every note again."
    )
    writes_blocked = not st.sidebar.checkbox(
        "Start this ledger's review from scratch", key=f"start-fresh-{db_path}|{ledger_id}",
        help="The dashboard's --ignore-legacy: the legacy rows stay where they are, unadopted.",
    )
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
            # the reason quotes ledger cells: a code span, where a URL is not a link and
            # nothing is redrawn (md() leaves both to Streamlit, see its docstring)
            st.markdown(f"- `{f.test_id}` **{f.test_name}** ({f.severity}) - {md_code(f.reason)}")

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
        # Every per-entry key names the ledger and the file too: two ledgers can
        # share entry ids, and a draft, a remembered note id or the double-click
        # guard for one must never act on the other's entry.
        entry_key = f"{db_path}|{ledger_id}|{picked}"
        shown_key = f"shown-{entry_key}"
        seen_id = st.session_state.get(shown_key)
        st.session_state[shown_key] = narrative["id"] if narrative else None
        if narrative:
            render_narrative(narrative)
        else:
            st.caption("No narrative yet for this entry.")
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if st.button("Rewrite narrative" if narrative else "Write narrative",
                     disabled=not has_key or writes_blocked,
                     key=f"narrate-{entry_key}") and not writes_blocked:
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
        choice_key, note_key = f"choice-{entry_key}", f"note-{entry_key}"
        # The form is not cleared on submit: a refused submit (the note changed
        # while the reviewer was reading it) must not discard what they typed.
        # The draft is cleared here instead, on the rerun after a decision was
        # recorded, and each entry keeps its own draft in the meantime.
        if st.session_state.pop("clear-decision", None) == entry_key:
            for key in (choice_key, note_key):
                st.session_state.pop(key, None)
        with st.form(key=f"decision-{entry_key}"):
            choice = st.radio("Decision", DECISIONS, horizontal=True,
                              format_func=str.capitalize, key=choice_key)
            note = st.text_area("Note", placeholder="What you checked, or why this is fine.",
                                key=note_key)
            submitted = st.form_submit_button("Record decision",
                                              disabled=not reviewer or writes_blocked)
        if writes_blocked:
            st.caption("Adopt the legacy rows first, or choose to start this ledger's review "
                       "from scratch (see the sidebar).")
        elif not reviewer:
            st.caption("Enter your name in the sidebar to record a decision.")
        if submitted and not writes_blocked:
            latest_id = narrative["id"] if narrative else None
            # A form submits once per click, and this guard absorbs a double click:
            # an append-only log should not carry an accidental duplicate.
            signature = (entry_key, choice, note.strip())
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
                st.session_state["clear-decision"] = entry_key
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
        # The segment table carries the rule tier's figures too ("rules only" and "both"
        # are its flagged entries): its caveat first, the model tier's after the lift.
        st.caption(evaluate.DETECTION_CAVEAT)
        st.dataframe(evaluate.compare_tiers(combined, labels),
                     width="stretch", hide_index=True)
        st.markdown("**Model lift over random selection**")
        st.dataframe(evaluate.model_lift(scores, labels),
                     width="stretch", hide_index=True)
        st.caption(evaluate.MODEL_TIER_CAVEAT)
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
        flagged, truth = metrics["flagged"] > 0, metrics["true_anomalies"] > 0
        a.metric("Precision", _ratio(metrics["precision"], flagged))
        b.metric("Recall", _ratio(metrics["recall"], truth))
        c.metric("F1", _ratio(metrics["f1"], flagged and truth))
        st.warning(evaluate.DETECTION_CAVEAT)
        st.markdown("**Recall by archetype**")
        st.dataframe(evaluate.recall_by_archetype(flags, labels),
                     width="stretch", hide_index=True)
        st.markdown("**Precision by test**")
        st.dataframe(evaluate.precision_by_test(flags, labels),
                     width="stretch", hide_index=True)

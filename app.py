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
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from ledgerlens import evaluate, jets
from ledgerlens.benford import SMALL_SAMPLE_NOTE, benford_test
from ledgerlens.controls import ControlsError, controls_path, format_limit, load_controls
from ledgerlens.env import load_dotenv
from ledgerlens.ingest import identity_path, ledger_identity, load_csv, load_labels
from ledgerlens.materiality import (
    BAND_NOTE,
    BANDS,
    FISCAL_YEAR_NOTE,
    OTHER_YEAR,
    Materiality,
    SamplingError,
    bands,
)
from ledgerlens.model import combine, score_ledger
from ledgerlens.narrate import NarrativeError, Narrator, build_prompt, entry_context
from ledgerlens.review import CALLER_FAULTS, DECISIONS, LEGACY_LEDGER_ID, Decision, ReviewStore
from ledgerlens.summary import code_span

load_dotenv()
st.set_page_config(page_title="LedgerLens", layout="wide")

DATA = Path("data")


def _stamp(path: str) -> tuple | None:
    """What a file is, beside where it is: its size and modification time, or None.

    ``load`` is cached by its arguments. Keyed by path alone, a label file corrected
    on disk kept showing the problem the first read found, and a regenerated ledger
    kept its old numbers, until the server was restarted. The key covers the four
    files ``load`` reads: the ledger, the labels, and the ledger's identity and
    controls files.
    """
    try:
        status = os.stat(path)
    except FileNotFoundError:
        try:  # a link to nothing is not "no file": load refuses it, so it keys differently
            status = os.lstat(path)
        except (OSError, ValueError):
            return None
        return ("link to nothing", status.st_mtime_ns)
    except OSError as exc:  # a loop, a name too long: load refuses it, so not "no file" either
        return ("unreadable", exc.errno)
    except ValueError:  # a NUL in the name: load says so, every time (nothing is cached)
        return None
    return (status.st_mtime_ns, status.st_size)


@st.cache_data(show_spinner=False)
def load(ledger_path: str, labels_path: str, stamps: tuple):
    """``stamps`` is part of the cache key and nothing else: see ``_stamp``."""
    df = load_csv(ledger_path)
    # Computed here so it is cached with the frame it describes: the store is
    # bound to the ledger on screen, never to a stale one.
    ledger_id = ledger_identity(df, ledger_path)
    controls = load_controls(ledger_path)  # a file that cannot be used is refused, below
    flags = jets.run_all(df, controls=controls)
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
    return df, flags, combined, scores, report, labels, ledger_id, labels_problem, controls


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
    is drawn as a code span instead (``summary.code_span``), where nothing acts and
    nothing is redrawn; checked in a browser on both Streamlit versions.
    """
    return _MARKUP.sub(r"\\\1", str(text))


def _sdk_errors() -> tuple:
    """The SDK's request errors, or none without the llm extra (Narrator then refuses
    before any request). Looked up when one is raised, not on load: the SDK reads its
    own settings (ANTHROPIC_LOG) when first imported, and .env is loaded after the imports."""
    try:
        from anthropic import APIError
    except ImportError:
        return ()
    return (APIError,)


def _refusal(exc: BaseException) -> sqlite3.Error | None:
    """What the review database itself said behind a failed read, or None if the fault is ours.

    Five of the page's seven reads go through pandas, which raises its own DatabaseError
    (an OSError, not a sqlite3.Error) and keeps what SQLite raised as the cause. The
    driver's errors for a value it cannot bind are sqlite3.Errors too, and they are ours
    (``CALLER_FAULTS``), wrapped or not.
    """
    cause = exc.__cause__ if isinstance(exc, pd.errors.DatabaseError) else exc
    if isinstance(cause, sqlite3.Error) and not isinstance(cause, CALLER_FAULTS):
        return cause
    return None


#: The decision form's two widgets, keyed per entry: what a reviewer was recording.
DRAFT_KEYS = ("choice-", "note-")
#: The controls that say which entry that was, and whether a decision may be written at
#: all. A run that stops before it draws them takes them off the page as well, and a browser
#: starts a control it is shown again from its default: the selector on the queue's first
#: entry, "Outstanding only" and "Start this ledger's review from scratch" unticked.
KEPT_CONTROLS = ("outstanding_only", "picked")
KEPT_PREFIXES = ("start-fresh-",)
#: The controls such a run forgets instead, so the script starts them from their defaults as
#: the browser does: the queue's other filters (see _filter) and the fiscal year. Left in
#: the session, they drew the next run from values the browser no longer showed, and under
#: Streamlit 1.50 the click after that was thrown away. One the run had drawn before it
#: stopped is still on the page with its value, and is kept.
FORGOTTEN_PREFIXES = ("filter-", "materiality_year")
#: What is said under every stop. Rerun, not reload: a reload starts a new session, and
#: what was set aside goes with the old one.
STOPPED = ("When the file can be used again, or another is named in the sidebar, rerun the page: "
           "Rerun, in the menu at the top right. Reloading the browser tab starts a new session "
           "and loses what was being recorded. With this file again, the entry, \"Outstanding "
           "only\" and the decision you submitted come back; a filter or a fiscal year this "
           "page no longer shows starts over.")
_DRAWN: set[str] = set()  # the controls named above that this run has drawn (the script runs afresh each time)
_NOTICE: list = []  # the placeholder above the tab bar, once the run has reached it


def _give_back(key: str, allowed=None) -> None:
    """Before a kept control is drawn: assign what a stopped run set aside for it.

    Assigned, so the browser is told (an assigned key reaches it as the widget's new
    value): it had dropped the control and would show its default, and send that default
    with the next click, whatever the script still held. A value that is no longer one of
    ``allowed`` (an entry that left the queue) is let go.
    """
    waiting = st.session_state.get("drafts-kept", {})
    if key in waiting:
        st.session_state["drafts-kept"] = {k: v for k, v in waiting.items() if k != key}
        if allowed is None or waiting[key] in allowed:
            st.session_state[key] = waiting[key]
    _DRAWN.add(key)


def _filter(name: str, options=()) -> str:
    """The key of one of the queue's filters, marked as drawn. Its options are part of the
    key (the amount has none), so a filter is the control it was without one: another
    ledger's options make it another filter, and a choice is never carried to a queue that
    could not hold it."""
    key = f"filter-{name}|" + "|".join(map(str, options))
    _DRAWN.add(key)
    return key


def _stop_for_the_store(message: str) -> None:
    """Say that the review database failed, and end this run there.

    Two things would otherwise be lost with the rest of the page. A write the run before
    did make is said here, once, as it would have been further down (the flash): an error
    alone, straight after a click, reads as if the click had failed. And the decision a
    reviewer was recording is set aside, with the controls that say which entry it was for
    and whether it may be written: a run that stops before the form takes it all off the
    page, the browser forgets what was in controls it no longer shows, and the next run
    drew the form empty, on the queue's first entry. Each takes its value back when it is
    next drawn. The filters and the fiscal year this run did not draw are forgotten here
    as the browser forgets them, so the next run and the browser start from the same
    queue. ``message`` can quote a path as typed or what SQLite said, so it goes through
    ``md()``. Once the tab bar is drawn the message goes above it, where it shows whichever
    tab is open.
    """
    place = _NOTICE[0].container() if _NOTICE else st
    flash = st.session_state.pop("flash", None)
    if flash:
        place.success(flash)
    place.error(md(message))
    place.caption(STOPPED)
    # A decision recorded on the run before is no draft: its form is waiting to be cleared.
    recorded = st.session_state.get("clear-decision")
    done = {f"{prefix}{recorded}" for prefix in DRAFT_KEYS} if recorded else set()
    kept = dict(st.session_state.get("drafts-kept", {}))  # a second failed run adds to the first's
    kept.update({key: value for key, value in st.session_state.items()
                 if isinstance(key, str) and key not in done and (
                     key.startswith(DRAFT_KEYS)
                     or ((key in KEPT_CONTROLS or key.startswith(KEPT_PREFIXES))
                         and key not in _DRAWN))})
    st.session_state["drafts-kept"] = kept
    for key in [key for key in st.session_state
                if isinstance(key, str) and key.startswith(FORGOTTEN_PREFIXES) and key not in _DRAWN]:
        del st.session_state[key]
    st.stop()


def _read(read, *args, otherwise: str | None = None):
    """A read of the review database: what it returned, or a message where the store refuses.

    The reads are never cached (a decision recorded a second ago has to show), so each one
    opens the file again, and a file that was fine when the page opened it can refuse the
    next read: locked past SQLite's wait, removed or replaced while a note was being
    requested, a disk or iCloud error. Uncaught, that was Streamlit's traceback. The run
    ends at the message, because what the page draws next is built on what could not be
    read (the legacy check that gates writes, the queue's decisions, the note a decision
    must point at), and no write may follow a read that failed. A read nothing else needs
    passes ``otherwise``, the sentence shown in its place: the page goes on and the read
    returns None. Only the store's refusals: a fault of our own stays a traceback.
    """
    try:
        return read(*args)
    except (sqlite3.Error, pd.errors.DatabaseError) as exc:
        refusal = _refusal(exc)
        if refusal is None:
            raise
        said = f"{type(refusal).__name__}: {refusal}"
        if otherwise is not None:
            st.error(md(f"{otherwise} ({said})."))
            return None
        _stop_for_the_store(
            f"The review database could not be read, so the page stops here ({said}).")


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
materiality_text = st.sidebar.text_input(
    "Materiality (optional)", key="materiality",
    help="Overall materiality for the engagement, in dollars; performance materiality is 75% "
         "of it and clearly trivial 5%. Each entry then shows the band its amount falls in, "
         "beside its scores and never in them.",
).strip()

if not Path(ledger_path).exists():
    st.warning("No ledger found. Run `ledgerlens generate` first.")
    st.stop()

try:
    df, flags, combined, scores, report, labels, ledger_id, labels_problem, controls = load(
        ledger_path, labels_path,
        (_stamp(ledger_path), _stamp(labels_path), _stamp(str(identity_path(ledger_path))),
         _stamp(str(controls_path(ledger_path)))))
except ControlsError as exc:  # before ValueError, which it is: the ledger itself is fine
    # Never the defaults in its place: another company's approver list would flag every
    # manual entry. As text: the reason can quote the file.
    st.error("The controls file beside this ledger cannot be used.")
    st.text(str(exc))
    st.stop()
except (OSError, ValueError) as exc:
    # A path that cannot be read (a folder exists too), a sidecar that names no ledger (never
    # guess which one it is), a file that is not a ledger, a cell no test can read, or a
    # population the model cannot be fitted on: a message, not an exception page.
    st.error("This ledger cannot be loaded.")
    # As text, never markdown: the reason quotes the cell it could not read, and an image
    # written into a ledger cell would be fetched the moment the page drew it.
    st.text(str(exc))
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
except RuntimeError as exc:
    # In the store's own words: a file from a newer version, one that is no review database,
    # one SQLite cannot read or prepare, a folder that cannot be made.
    _stop_for_the_store(str(exc))
st.sidebar.caption(f"Review rows keyed by `{ledger_id}`")
# What JET-12 and JET-06 were run against. Names from a file are drawn as code spans, as
# a flag's reason is, and the limit's "$" escaped so no pair of them starts maths.
st.sidebar.caption("Controls: approvers {} (JET-12) · approval limit {} (JET-06) · {}".format(
    ", ".join(code_span(name) for name in controls.manual_entry_approvers),
    md(format_limit(controls.approval_limit)),
    "from " + code_span(controls.source) if controls.source is not None
    else "the synthetic company's (no controls file beside the ledger)"))
others = _read(store.other_ledgers)
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
# again when the sidebar points at another database. A tick a stopped run set
# aside waits under that key, so it comes back to this file's box and no other,
# also after a visit to another file, as a draft does (without a stop such a
# visit asks again here too: Streamlit forgets a box it no longer draws).
writes_blocked = False
if (LEGACY_LEDGER_ID in set(others["ledger_id"])
        and ledger_id not in set(_read(store.ledgers)["ledger_id"])):
    st.sidebar.warning(
        "Rows from before review data was keyed by ledger sit under 'legacy', and this ledger "
        "has none yet. If they were written for it, adopt them first: `ledgerlens adopt-legacy "
        f"{ledger_path} --db {db_path}` (no API calls). A note or a decision recorded here first "
        "would make adopting them impossible, and narrating would buy every note again."
    )
    _give_back(f"start-fresh-{db_path}|{ledger_id}")
    writes_blocked = not st.sidebar.checkbox(
        "Start this ledger's review from scratch", key=f"start-fresh-{db_path}|{ledger_id}",
        help="The dashboard's --ignore-legacy: the legacy rows stay where they are, unadopted.",
    )
current = _read(store.current)

# ---- materiality: a band beside each entry's scores, never in them (rule 3) ----
# Nothing below is cached: the band is one pass over the loaded frame, and the figures are
# typed in, not read from a file.
materiality = materiality_year = None
if materiality_text:
    try:
        materiality = Materiality(materiality_text)
    except SamplingError as exc:
        # As text, never markdown: the reason quotes what was typed.
        st.sidebar.text(f"Materiality not used: {exc}")
    else:
        years = sorted({int(year) for year in combined["fiscal_year"]})
        if len(years) > 1:  # the figures are set for one year's statements, as the sample's are
            # the latest year first: the one usually under audit
            _DRAWN.add("materiality_year")
            materiality_year = st.sidebar.selectbox("Fiscal year for materiality", years,
                                                    index=len(years) - 1, key="materiality_year")
        else:
            materiality_year = years[0] if years else None
        combined = combined.assign(materiality_band=bands(combined, materiality, materiality_year))
        st.sidebar.caption(md(f"Materiality {materiality.describe()}; fiscal year {materiality_year}"))
        st.sidebar.caption(md(BAND_NOTE))
        st.sidebar.caption(md(FISCAL_YEAR_NOTE))

# ---- headline numbers ----
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Entries", "{:,}".format(df["entry_id"].nunique()))
c2.metric("Flagged", int((combined["risk_score"] > 0).sum()))
c3.metric("Flags raised", len(flags))
c4.metric("Flag rate", "{:.2%}".format((combined["risk_score"] > 0).mean()))
c5.metric("Decided", len(current))
progress = _read(store.summary)
if not progress.empty:
    st.caption("Review progress: " + ", ".join(
        f"{r.decision} {r.entries}" for r in progress.itertuples()
    ))

# Two of the reads that end the run sit inside the first tab (the history's is there too,
# and does not end it). Their refusal is said here, above the tab bar: said inside that
# tab, it left the other three blank with no word. A placeholder, not a container: left
# empty, a container gave Streamlit 1.50's test harness each element below it twice after
# a rerun.
_NOTICE.append(st.empty())
tab_queue, tab_tiers, tab_benford, tab_quality = st.tabs(
    ["Exception queue", "Tier comparison", "Benford", "Detection quality"]
)

# ---- queue ----
with tab_queue:
    left, right = st.columns([1, 3])
    with left:
        severities = sorted(flags["severity"].unique()) if not flags.empty else []
        chosen_sev = st.multiselect("Severity", severities, default=severities,
                                    key=_filter("severity", severities))
        test_ids = sorted(flags["test_id"].unique()) if not flags.empty else []
        chosen_tests = st.multiselect("Test", test_ids, default=test_ids,
                                      key=_filter("test", test_ids))
        agreements = sorted(combined["agreement"].unique())
        chosen_agree = st.multiselect("Tier agreement", agreements, default=agreements,
                                      key=_filter("agreement", agreements))
        min_amount = st.number_input("Minimum amount", value=0.0, step=1000.0,
                                     key=_filter("amount"))
        _give_back("outstanding_only")
        outstanding_only = st.checkbox("Outstanding only", value=False, key="outstanding_only",
                                       help="Hide entries that already have a decision.")
        chosen_bands = None
        if materiality is not None:
            # a filter the reviewer chooses, as the amount is, in the bands' own order. Keyed
            # by its options, like the other filters: a choice kept across a change of ledger
            # would empty the queue of a ledger that holds none of the band chosen.
            seen = set(combined["materiality_band"])
            present = [b for b in (*BANDS, OTHER_YEAR) if b in seen]
            chosen_bands = st.multiselect("Materiality band", present, default=present,
                                          key=_filter("band", present))

    matching = flags[
        flags["severity"].isin(chosen_sev) & flags["test_id"].isin(chosen_tests)
    ] if not flags.empty else flags
    ids = set(matching["entry_id"])

    queue = combined[
        combined["entry_id"].isin(ids)
        & combined["agreement"].isin(chosen_agree)
        & (combined["entry_amount"] >= min_amount)
    ]
    if chosen_bands is not None:
        queue = queue[queue["materiality_band"].isin(chosen_bands)]
    if outstanding_only:
        queue = queue[~queue["entry_id"].isin(_read(store.decided_ids))]
    queue = queue.merge(current[["entry_id", "decision", "reviewer"]], on="entry_id", how="left")

    with right:
        st.subheader(f"{len(queue):,} entries to review")
        st.dataframe(
            queue[["entry_id", "posting_date", "source", "created_by", "entry_amount",
                   *(["materiality_band"] if materiality is not None else []),
                   "risk_score", "model_score", "agreement", "tests_fired", "decision",
                   "reviewer"]],
            width="stretch", hide_index=True, height=340,
        )

    if not queue.empty:
        st.divider()
        _give_back("picked", allowed=queue["entry_id"].tolist())
        picked = st.selectbox("Inspect entry", queue["entry_id"].tolist(), key="picked")
        row = combined[combined["entry_id"] == picked].iloc[0]

        # Where "Recorded: ..." is said, once the note below has been read: a run that stops
        # at that read says it above its own error instead (see _stop_for_the_store).
        said = st.empty()

        a, b, c, *rest = st.columns(4 if materiality is not None else 3)
        a.metric("Rule score", "{:.1f}".format(row["risk_score"]))
        b.metric("Model score", "{:.3f}".format(row["model_score"]))
        c.metric("Tier agreement", row["agreement"])
        if rest:  # beside the two scores, never in them
            rest[0].metric("Materiality band", row["materiality_band"])

        st.markdown("**Why it was flagged**")
        for f in flags[flags["entry_id"] == picked].itertuples():
            # the reason quotes ledger cells: a code span, where a URL is not a link and
            # nothing is redrawn (md() leaves both to Streamlit, see its docstring)
            st.markdown(f"- `{f.test_id}` **{f.test_name}** ({f.severity}) - {code_span(f.reason)}")

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
        narrative = _read(store.get_narrative, picked)
        flash = st.session_state.pop("flash", None)
        if flash:
            said.success(flash)
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
            narrator = Narrator()
            entry, entry_flags, entry_lines = entry_context(combined, flags, df, picked)
            prompt = build_prompt(entry, entry_flags, entry_lines)
            written = None
            try:
                with st.spinner("Asking Claude..."):
                    written = narrator.write_note(prompt)
            except NarrativeError as exc:
                # st.error draws markdown, and the message can quote the response.
                st.error(md(str(exc)))
            except _sdk_errors() as exc:  # evaluated only once an exception reaches it
                # The SDK's own errors (a refused key, no connection, a rate limit, an
                # overloaded API) are not NarrativeErrors. Uncaught, one stops the script here
                # and the decision form below is not drawn. Only these: a fault in our own
                # reading of the reply is a traceback, not the request failing. A status
                # error quotes the server's body.
                st.error(md(f"The request to Claude failed, so no note was stored "
                            f"({type(exc).__name__}: {exc})"))
            if written is not None:
                fresh, model = written
                try:
                    note_id = store.save_narrative(picked, fresh, model=model)
                except CALLER_FAULTS:
                    raise
                except sqlite3.Error as exc:
                    # The review database refused the write: a read-only file, a lock held
                    # past SQLite's five-second wait (a database browser in a transaction;
                    # one insert by narrate or another tab is waited out), a file removed or
                    # replaced while the note was being requested, a disk or iCloud error.
                    # The note was requested and answered, so this says so with the usage,
                    # but it is not drawn: a decision must point at advice on record, and
                    # this note is not.
                    st.error(md(f"The note came back but could not be stored, so it is not "
                                f"shown and no decision can point to it "
                                f"({type(exc).__name__}: {exc}). This request still counts: "
                                f"{narrator.usage.describe()}."))
                else:
                    st.session_state["flash"] = (
                        f"Narrative written as note #{note_id} ({narrator.usage.describe()})."
                    )
                    st.rerun()
        if not has_key:
            st.caption("Set ANTHROPIC_API_KEY in .env to write narratives from here, or run "
                       "`ledgerlens narrate` from the command line.")

        # ---- decision: a named human, append-only ----
        st.markdown("**Record a decision**")
        choice_key, note_key = (f"{prefix}{entry_key}" for prefix in DRAFT_KEYS)
        # The form is not cleared on submit: a refused submit (the note changed
        # while the reviewer was reading it) must not discard what they typed.
        # The draft is cleared here instead, on the rerun after a decision was
        # recorded, and each entry keeps its own draft in the meantime.
        clearing = st.session_state.pop("clear-decision", None) == entry_key
        if clearing:
            # Assigned, never removed. A removed key resets the value for this script and
            # tells the browser nothing: it went on showing the decision just recorded, and
            # sent it back with the next submit. An assigned key reaches the browser as the
            # widget's new value. The choice is cleared to none: a form that came back on
            # "accept" recorded an accept the reviewer never chose at a second click.
            st.session_state[choice_key] = None
            st.session_state[note_key] = ""
        else:
            # What a reviewer was recording when a run stopped for the store (see
            # _stop_for_the_store), given back the same way, so the browser shows it again.
            # Only this entry's: another entry's draft waits for its own form.
            for key in (choice_key, note_key):
                _give_back(key)
        with st.form(key=f"decision-{entry_key}"):
            # No decision is chosen until the reviewer chooses one (rule 8).
            choice = st.radio("Decision", DECISIONS, index=None, horizontal=True,
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
            if not reviewer:
                # The button is drawn disabled without a name, but on Streamlit 1.50 a click
                # lands when the name is cleared and the button clicked before the rerun that
                # disables it, and record() refuses a decision with no reviewer (a traceback
                # that stopped the page).
                st.warning("Enter your name in the sidebar first: nothing was recorded, and "
                           "what you typed is kept.")
            elif clearing or st.session_state.get("last_decision") == signature:
                # A click that arrives on the run that clears the form carries the decision
                # just recorded: it is the second click, whatever the cleared form now holds.
                st.warning("That decision was just recorded.")
            elif choice is None:
                st.warning("Choose a decision first: nothing was recorded, and what you typed "
                           "is kept.")
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
                decision = Decision(
                    entry_id=picked, decision=choice, reviewer=reviewer, note=note.strip(),
                    risk_score=float(row["risk_score"]), model_score=float(row["model_score"]),
                    narrative_id=seen_id,
                )
                try:
                    store.record(decision)
                except CALLER_FAULTS:
                    raise
                except sqlite3.Error as exc:
                    # Nothing was written (an uncommitted insert is rolled back), so
                    # nothing that marks it done is set either: the draft stays, and the
                    # same submit is not refused as a duplicate when it is retried.
                    # record()'s own ValueErrors stay tracebacks: the decision is one of the
                    # radio's (none chosen is refused above), the queue gives an entry, and
                    # a missing name is refused above.
                    st.error(md(f"The decision was not recorded ({type(exc).__name__}: {exc}). "
                                "What you typed is kept: record it again."))
                else:
                    st.session_state["last_decision"] = signature
                    st.session_state["flash"] = f"Recorded: {choice} by {reviewer}."
                    st.session_state["clear-decision"] = entry_key
                    st.rerun()

        # The last thing the queue draws, and nothing else is built on it: where the store
        # refuses this read (a file replaced while a note was being requested fails the save
        # above and then this), the message stands in its place and the other tabs are drawn.
        history = _read(store.history, picked,
                        otherwise="The decision history could not be read, so it is not shown")
        if history is not None and not history.empty:
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
        st.markdown("**Each archetype against the model's budget**")
        st.dataframe(evaluate.budget_recall_by_archetype(combined, labels),
                     width="stretch", hide_index=True)
    else:
        st.dataframe(combined["agreement"].value_counts().rename("entries"))
    st.code(report.describe(), language="text")

# ---- benford ----
with tab_benford:
    result = benford_test(df["abs_amount"], "population")
    if not result["sufficient_sample"]:
        st.warning(SMALL_SAMPLE_NOTE)
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
        st.markdown("**Caught by the test written for each archetype, or by others**")
        st.dataframe(evaluate.attribution(flags, labels),
                     width="stretch", hide_index=True)
        st.markdown("**Precision by test**")
        st.dataframe(evaluate.precision_by_test(flags, labels),
                     width="stretch", hide_index=True)

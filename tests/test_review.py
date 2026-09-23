import pytest

from ledgerlens import jets
from ledgerlens.review import DECISIONS, Decision, ReviewStore


@pytest.fixture
def store(tmp_path):
    return ReviewStore(tmp_path / "review.sqlite")


def test_store_creates_its_schema_on_a_fresh_path(tmp_path):
    path = tmp_path / "nested" / "review.sqlite"
    ReviewStore(path)
    assert path.exists()


def test_history_is_append_only(store):
    first = store.record(Decision("JE-1", "dismiss", "ana", "routine"))
    second = store.record(Decision("JE-1", "escalate", "ana", "changed my mind"))

    assert second > first
    history = store.history("JE-1")
    assert history["decision"].tolist() == ["dismiss", "escalate"]
    assert history["note"].tolist() == ["routine", "changed my mind"]


def test_current_is_the_latest_decision_per_entry(store):
    store.record(Decision("JE-1", "dismiss", "ana"))
    store.record(Decision("JE-2", "accept", "ben"))
    store.record(Decision("JE-1", "escalate", "ana"))

    current = store.current().set_index("entry_id")["decision"]
    assert current.to_dict() == {"JE-1": "escalate", "JE-2": "accept"}


@pytest.mark.parametrize("decision", [
    Decision("JE-1", "approve", "ana"),   # not a documented decision
    Decision("JE-1", "accept", "   "),    # nobody signed it
    Decision("  ", "accept", "ana"),      # no entry
])
def test_record_rejects_bad_decisions(store, decision):
    with pytest.raises(ValueError):
        store.record(decision)


def test_summary_counts_current_decisions_only(store):
    assert store.summary().empty
    store.record(Decision("JE-1", "dismiss", "ana"))
    store.record(Decision("JE-2", "dismiss", "ana"))
    store.record(Decision("JE-3", "accept", "ana"))
    store.record(Decision("JE-3", "escalate", "ana"))  # supersedes the accept

    summary = store.summary().set_index("decision")["entries"]
    assert summary.to_dict() == {"dismiss": 2, "escalate": 1}


def test_outstanding_is_the_flagged_entries_without_a_decision(store, small_ledger):
    ledger, _ = small_ledger
    flags = jets.run_all(ledger)
    scored = jets.score_entries(ledger, flags)
    flagged = scored[scored["risk_score"] > 0]["entry_id"].tolist()
    assert len(flagged) > 1

    store.record(Decision(flagged[0], "accept", "ana"))

    outstanding = set(store.outstanding(scored)["entry_id"])
    assert outstanding == set(flagged[1:])
    assert store.decided_ids() == {flagged[0]}


def test_narrative_round_trip_keeps_the_evidence_list(store):
    narrative = {"summary": "s", "why_flagged": "w", "evidence_to_request": ["a", "b"],
                 "suggested_control": "c", "confidence": "low"}
    store.save_narrative("JE-1", narrative, model="claude-test")

    assert store.get_narrative("JE-9") is None
    back = store.get_narrative("JE-1")
    assert back["evidence_to_request"] == ["a", "b"]
    assert back["model"] == "claude-test"
    assert back["generated_at"]
    assert store.narrative_ids() == {"JE-1"}

    store.save_narrative("JE-1", dict(narrative, summary="updated"))
    assert store.get_narrative("JE-1")["summary"] == "updated"
    assert len(store.narratives_frame()) == 1


def test_decisions_survive_a_restart(tmp_path):
    path = tmp_path / "review.sqlite"
    ReviewStore(path).record(Decision("JE-1", "escalate", "ana", risk_score=5.0, model_score=0.9))

    reopened = ReviewStore(path)
    history = reopened.history("JE-1")
    assert len(history) == 1
    assert history.iloc[0]["risk_score"] == 5.0
    assert reopened.decided_ids() == {"JE-1"}


def test_the_only_decisions_are_the_documented_ones():
    assert DECISIONS == ("accept", "dismiss", "escalate")

"""The read-only query layer, on every supported Python (no MCP import)."""

import json
import sqlite3
from pathlib import Path

import pytest

from ledgerlens.ledger_context import ENV_LEDGER, ENV_REVIEW_DB, LedgerContext, records
from ledgerlens.review import Decision, ReviewStore


@pytest.fixture(scope="module")
def context(small_ledger):
    ledger, _ = small_ledger
    return LedgerContext(ledger).load()


def test_summary_is_json_safe_and_counts_the_population(context):
    summary = context.summary()
    json.dumps(summary)
    assert summary["entries"] == context.combined["entry_id"].nunique()
    assert summary["flagged"] > 0
    assert sum(summary["flags_by_test"].values()) == summary["flags_raised"]
    assert "Isolation Forest" in summary["model"]
    assert "a question, not a finding" in summary["caveat"]


def test_top_exceptions_are_ordered_and_limited(context):
    top = context.top_exceptions(limit=5)
    json.dumps(top)
    assert top["count"] == 5
    assert top["matching"] == int((context.combined["risk_score"] > 0).sum())
    scores = [e["risk_score"] for e in top["entries"]]
    assert scores == sorted(scores, reverse=True)
    assert all(e["reasons"] for e in top["entries"])


def test_top_exceptions_filters_by_year_period_and_agreement(context):
    c = context.combined
    year = int(c["fiscal_year"].iloc[0])
    top = context.top_exceptions(limit=50, fiscal_year=year, period_from=1, period_to=2)
    assert all(e["fiscal_year"] == year and 1 <= e["period"] <= 2 for e in top["entries"])
    expected = int(((c["risk_score"] > 0) & (c["fiscal_year"] == year)
                    & c["period"].between(1, 2)).sum())
    assert top["matching"] == expected

    rules_only = context.top_exceptions(limit=100, agreement="rules only")
    assert all(e["agreement"] == "rules only" for e in rules_only["entries"])


def test_explain_entry_has_lines_flags_scores_and_no_review_without_a_store(context):
    entry_id = context.top_exceptions(limit=1)["entries"][0]["entry_id"]
    detail = context.explain_entry(entry_id)
    json.dumps(detail)
    assert detail["entry"]["entry_id"] == entry_id
    assert len(detail["lines"]) >= 2
    assert detail["flags"] and all(f["reason"] for f in detail["flags"])
    assert detail["narrative"] is None
    assert detail["decisions"] == []


def test_explain_unknown_entry_raises_keyerror(context):
    with pytest.raises(KeyError, match="JE-0000-000000"):
        context.explain_entry("JE-0000-000000")


def test_review_data_is_read_but_a_database_is_never_created(small_ledger, tmp_path):
    ledger, _ = small_ledger
    db = tmp_path / "review.sqlite"
    context = LedgerContext(ledger, review_db=db).load()

    status = context.review_status()
    assert status["exists"] is False
    assert status["outstanding"] == status["flagged"]
    assert not db.exists()

    entry_id = context.top_exceptions(limit=1)["entries"][0]["entry_id"]
    store = ReviewStore(db)
    seen = store.save_narrative(entry_id, {"summary": "s", "why_flagged": "w",
                                           "evidence_to_request": ["e"],
                                           "suggested_control": "c", "confidence": "low"}, model="m")
    store.record(Decision(entry_id, "dismiss", "ana", "routine", narrative_id=seen))

    status = context.review_status()
    assert status["exists"] is True
    assert status["decided"] == 1
    assert status["by_decision"] == {"dismiss": 1}
    assert status["narratives"] == 1
    assert status["outstanding"] == status["flagged"] - 1

    detail = context.explain_entry(entry_id)
    assert detail["decisions"][0]["decision"] == "dismiss"
    assert detail["decisions"][0]["narrative_id"] == seen
    assert detail["narrative"]["summary"] == "s"
    assert detail["narrative"]["id"] == seen
    top = context.top_exceptions(limit=1)["entries"][0]
    assert top["decision"] == "dismiss"
    assert top["narrative_summary"] == "s"


def test_the_review_database_is_opened_read_only(small_ledger, tmp_path):
    ledger, _ = small_ledger
    db = tmp_path / "review.sqlite"
    ReviewStore(db).record(Decision("JE-2024-000001", "dismiss", "ana"))
    context = LedgerContext(ledger, review_db=db).load()

    store = context._store()
    assert store.is_read_only
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.record(Decision("JE-2024-000002", "accept", "ana"))
    assert ReviewStore(db).decided_ids() == {"JE-2024-000001"}

    # An empty file is reported, not given a schema.
    empty = tmp_path / "empty.sqlite"
    empty.touch()
    with pytest.raises(RuntimeError, match="ledgerlens narrate"):
        LedgerContext(ledger, review_db=empty).load().review_status()
    assert empty.stat().st_size == 0


def test_search_entries_filters(context):
    code = context.lines["account_code"].iloc[0]
    touched = set(context.lines.loc[context.lines["account_code"] == code, "entry_id"])
    result = context.search_entries(account_code=code, limit=5)
    assert result["matching"] == len(touched)
    assert all(e["entry_id"] in touched for e in result["entries"])

    user = context.combined["created_by"].iloc[0]
    result = context.search_entries(created_by=user, min_amount=1000, limit=3)
    assert all(e["created_by"] == user and e["entry_amount"] >= 1000 for e in result["entries"])
    assert result["count"] <= 3


def test_benford_for_the_population_and_per_segment(context):
    population = context.benford()
    json.dumps(population)
    assert population["scope"] == "population"
    assert {"mad", "conformity", "chi_square", "observed_prop"} <= set(population)

    segments = context.benford(by="account_code", min_n=50)
    json.dumps(segments)
    assert all(s["n"] >= 50 for s in segments["segments"])
    with pytest.raises(KeyError):
        context.benford(by="not_a_column")


def test_from_env_reads_the_paths(monkeypatch):
    monkeypatch.setenv(ENV_LEDGER, "x.csv")
    monkeypatch.setenv(ENV_REVIEW_DB, "y.sqlite")
    context = LedgerContext.from_env()
    assert context._ledger == "x.csv"
    assert context.review_db == Path("y.sqlite")
    assert context.loaded is False


def test_records_are_plain_json(context):
    rows = records(context.combined.head(2), ("entry_id", "posting_date", "entry_amount"))
    assert isinstance(rows[0]["posting_date"], str)
    assert isinstance(rows[0]["entry_amount"], float)

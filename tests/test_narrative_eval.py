"""The narrative eval: selection, grading, running and reporting - no API key needed."""

import json
import re
from pathlib import Path

import pytest

from ledgerlens import jets
from ledgerlens.model import combine, score_ledger
from ledgerlens.narrate import NarrativeError, Narrator, build_prompt, entry_context
from ledgerlens.narrative_eval import (
    METRICS,
    Case,
    aggregate,
    check_cases,
    grade,
    load_cases,
    numbers_in,
    render_markdown,
    run_eval,
    save_cases,
    select_cases,
)


@pytest.fixture(scope="module")
def scored(ledger):
    flags = jets.run_all(ledger)
    model_scores, _ = score_ledger(ledger)
    return combine(jets.score_entries(ledger, flags), model_scores), flags


@pytest.fixture(scope="module")
def sample(scored, ledger):
    """A case built from the riskiest entry, with expectations derived from its own data."""
    combined, flags = scored
    row = combined[combined["risk_score"] > 0].iloc[0]
    entry, entry_flags, lines = entry_context(combined, flags, ledger, row["entry_id"])
    amount_whole = f"{row['entry_amount']:,.2f}".split(".")[0]
    case = Case(
        entry_id=row["entry_id"], archetype="sample", tests_fired=row["tests_fired"],
        why_chosen="test", must_mention=[re.escape(amount_whole), *row["tests_fired"].split(", ")],
        expected_confidence=["high", "medium"],
    )
    return case, build_prompt(entry, entry_flags, lines), entry, lines


def oracle(case, entry, lines):
    amount = f"${entry['entry_amount']:,.2f}"
    account = lines.iloc[0]["account_name"]
    return {
        "summary": f"Entry {case.entry_id} for {amount} touching {account}.",
        "why_flagged": f"{case.tests_fired} fired. The entry is unusual for the reasons given, "
                       "and an innocent explanation is possible.",
        "evidence_to_request": [f"The invoice supporting the {amount} posting to {account}"],
        "suggested_control": "Manual journal approval",
        "confidence": "medium",
    }


# --- selection ----------------------------------------------------------------


def test_select_cases_is_deterministic_and_covers_every_caught_archetype(scored, labels):
    combined, flags = scored
    cases = select_cases(combined, flags, labels)

    assert [c.entry_id for c in cases] == [c.entry_id for c in select_cases(combined, flags, labels)]
    assert len({c.entry_id for c in cases}) == len(cases)
    caught = set(labels.loc[labels["entry_id"].isin(set(flags["entry_id"])) & labels["is_anomaly"],
                            "anomaly_type"])
    assert caught <= {c.archetype for c in cases}
    assert sum(c.archetype == "multi_flag" for c in cases) == 3
    benign = [c for c in cases if c.archetype == "benign"]
    assert len(benign) == 2
    assert all(c.tests_fired == "JET-12" and c.expected_confidence == ["low"] for c in benign)
    normal = set(labels.loc[~labels["is_anomaly"], "entry_id"])
    assert all(c.entry_id in normal for c in benign)


def test_check_cases_reports_stale_and_missing_entries(scored):
    combined, _ = scored
    first = combined.iloc[0]
    good = Case(first["entry_id"], "x", first["tests_fired"], "w")
    stale = Case(first["entry_id"], "x", "JET-99", "w")
    missing = Case("JE-0000-000000", "x", "", "w")
    assert check_cases([good], combined) == []
    assert len(check_cases([stale, missing], combined)) == 2


def test_case_files_round_trip_and_reject_bad_input(tmp_path):
    cases = [Case("JE-1", "a", "JET-01", "w", must_mention=[r"\$1,000"], expected_confidence=["high"])]
    path = save_cases(cases, tmp_path / "cases.json", generator={"seed": 1})
    assert load_cases(path) == cases

    payload = json.loads(path.read_text())
    payload["cases"][0]["must_mention"] = ["("]
    path.write_text(json.dumps(payload))
    with pytest.raises(re.error):
        load_cases(path)

    payload["cases"][0]["must_mention"] = []
    payload["cases"].append(dict(payload["cases"][0]))
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


# --- grading -----------------------------------------------------------------


def test_oracle_narrative_passes_every_check(sample):
    case, prompt, entry, lines = sample
    assert grade(oracle(case, entry, lines), case, prompt) == {**{m: True for m in METRICS}, "passed": True}


def test_no_narrative_fails_everything(sample):
    case, prompt, *_ = sample
    assert grade(None, case, prompt)["passed"] is False
    assert grade({}, case, prompt)["schema_valid"] is False
    assert grade({"summary": "x"}, case, prompt)["schema_valid"] is False


@pytest.mark.parametrize("damage, failing", [
    (lambda n: n.update(why_flagged=n["why_flagged"] + " The entry was deliberately concealed."),
     "no_assertions"),
    (lambda n: n.update(why_flagged=n["why_flagged"] + " The related invoice was for $123,456.78."),
     "no_invented_numbers"),
    (lambda n: n.update(evidence_to_request=["Obtain supporting documentation"]),
     "evidence_specific"),
    (lambda n: n.update(confidence="low"), "confidence_in_band"),
    (lambda n: n.update(summary="An entry.", why_flagged="Something looks off."),
     "mentions_required"),
])
def test_each_property_fails_on_its_own(sample, damage, failing):
    case, prompt, entry, lines = sample
    narrative = oracle(case, entry, lines)
    damage(narrative)
    metrics = grade(narrative, case, prompt)
    assert [m for m in METRICS if not metrics[m]] == [failing]
    assert metrics["passed"] is False


def test_numbers_normalise_formatting_and_ignore_small_tokens():
    assert numbers_in("$9,900.00 and 9,900 and 9900") == {"9900"}
    assert numbers_in("JET-05, line 2, posted 2025-09-30") == {"2025"}
    assert numbers_in("a difference of $143.57") == {"143.57"}


# --- running and reporting ---------------------------------------------------


def test_run_eval_records_resumes_and_keeps_plumbing_out_of_the_score(sample, scored, ledger, llm,
                                                                        tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    others = combined[(combined["risk_score"] > 0) & (combined["entry_id"] != case.entry_id)]
    second = Case(others.iloc[0]["entry_id"], "y", others.iloc[0]["tests_fired"], "w")
    third = Case(others.iloc[1]["entry_id"], "z", others.iloc[1]["tests_fired"], "w")

    client = llm.client([
        llm.response(oracle(case, entry, lines)),
        llm.response(stop_reason="max_tokens"),
        RuntimeError("socket closed"),
    ])
    narrator = Narrator(client=client)
    rows = run_eval([case, second, third], narrator, combined, flags, ledger, tmp_path)

    assert [r["status"] for r in rows] == ["ok", "invalid", "error"]
    assert rows[0]["metrics"]["passed"] is True
    assert rows[0]["usage"]["input_tokens"] == 300
    assert rows[0]["model"] == narrator.model
    assert rows[1]["metrics"]["schema_valid"] is False
    assert rows[2]["metrics"] is None
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2
    assert len((tmp_path / "errors.jsonl").read_text().splitlines()) == 1

    # A resumed run re-runs only the case that errored.
    again = llm.client()
    rerun = run_eval([case, second, third], Narrator(client=again), combined, flags, ledger, tmp_path)
    assert [r["status"] for r in rerun] == ["ok", "invalid", "ok"]
    assert len(again.calls) == 1

    summary = aggregate(rerun)
    assert (summary["graded"], summary["invalid"], summary["errors"]) == (3, 1, 0)
    assert summary["rates"]["schema_valid"] == round(2 / 3, 4)
    assert summary["usage"]["requests"] == 3

    report = render_markdown(rerun, summary)
    assert narrator.model in report
    assert all(r["entry_id"] in report for r in rerun)
    assert "Misses" in report
    assert "max_tokens" in report


def test_run_eval_fails_once_without_a_key(sample, scored, ledger, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    case, *_ = sample
    combined, flags = scored
    with pytest.raises(NarrativeError, match=r"\.env"):
        run_eval([case], Narrator(), combined, flags, ledger, tmp_path)
    assert not (tmp_path / "results.jsonl").exists()


# --- the committed case set -----------------------------------------------------

CASES_PATH = Path(__file__).resolve().parents[1] / "evals" / "narratives" / "cases.json"


def test_committed_case_set_matches_the_default_ledger(scored, labels):
    """The file is tied to the generator: a change there fails here, not silently later."""
    combined, flags = scored
    cases = load_cases(CASES_PATH)
    assert len(cases) >= 15
    assert check_cases(cases, combined) == []
    assert [c.entry_id for c in cases] == [c.entry_id for c in select_cases(combined, flags, labels)]
    for case in cases:
        assert case.must_mention, case.entry_id
        assert set(case.expected_confidence) <= {"high", "medium", "low"}
        for test_id in case.tests_fired.split(", "):
            assert test_id in case.must_mention, (case.entry_id, test_id)


def test_committed_expectations_are_satisfiable_from_the_prompt(scored, ledger):
    """Every required fact is in the entry's own prompt, so a faithful note can pass."""
    combined, flags = scored
    for case in load_cases(CASES_PATH):
        entry, entry_flags, lines = entry_context(combined, flags, ledger, case.entry_id)
        prompt = build_prompt(entry, entry_flags, lines)
        for pattern in case.must_mention:
            assert re.search(pattern, prompt, re.I), (case.entry_id, pattern)

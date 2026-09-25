"""The narrative eval: selection, grading, running and reporting - no API key needed."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from ledgerlens import jets, narrative_eval
from ledgerlens.model import combine, score_ledger
from ledgerlens.narrate import (
    SYSTEM_PROMPT,
    NarrativeError,
    Narrator,
    build_prompt,
    entry_context,
)
from ledgerlens.narrative_eval import (
    CITATIONS,
    GRADER_NOTES,
    GRADER_SHA256,
    METRICS,
    UNRECORDED,
    Case,
    CasesChangedError,
    RegradeError,
    aggregate,
    cases_digest,
    check_cases,
    confidence_baselines,
    default_runs_dir,
    grade,
    grader_digest,
    load_cases,
    numbers_in,
    render_markdown,
    run_eval,
    save_cases,
    select_cases,
    strip_citations,
)

#: Figures the system prompt quotes as examples of specific wording ("$9,912
#: payment", "Sales Revenue (4000)"). They are not citations and are not set
#: aside by the grader.
STYLE_EXAMPLE_NUMBERS = {"9912", "4000"}


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


def test_every_number_the_prompt_shows_is_a_style_example_or_a_stripped_citation():
    """The guard that keeps the citation list honest.

    If a future prompt cites another standard, its number appears here without
    being a style example or a citation the grader strips, and this fails:
    somebody then has to decide, in CI, rather than the check drifting.
    """
    cited = set()
    for pattern in CITATIONS:
        for match in pattern.finditer(SYSTEM_PROMPT):
            cited |= numbers_in(match.group(0))
    assert cited == {"240"}
    assert numbers_in(SYSTEM_PROMPT) - STYLE_EXAMPLE_NUMBERS == cited
    assert strip_citations("per AU-C 240 and au-c240, $240 remains") == "per   and  , $240 remains"


def test_the_citation_is_set_aside_but_its_bare_number_is_not(sample):
    """AU-C 240 is a citation; "$240" is a figure, and the style example is neither.

    The first real run cited AU-C 240 and was marked as inventing a number.
    The first fix admitted every number in the system prompt, which also let
    through the style example's $9,912 and account 4000. The second admitted
    a bare "240" wherever it appeared. Now the citation is removed from the
    note before its numbers are extracted, and nothing else is excused.
    """
    case, prompt, entry, lines = sample
    assert {"240", "9912"}.isdisjoint(numbers_in(prompt))  # the sample entry is not the example

    cited = oracle(case, entry, lines)
    cited["why_flagged"] += " This is the pattern AU-C 240 directs auditors to test."
    assert grade(cited, case, prompt)["no_invented_numbers"] is True

    bare = oracle(case, entry, lines)
    bare["why_flagged"] += " A $240 fee was charged on the same day."
    assert grade(bare, case, prompt)["no_invented_numbers"] is False

    copied = oracle(case, entry, lines)
    copied["evidence_to_request"].append("Obtain the signed approval for this $9,912 payment")
    assert grade(copied, case, prompt)["no_invented_numbers"] is False

    invented = oracle(case, entry, lines)
    invented["why_flagged"] += " The related invoice was for $123,456.78."
    assert grade(invented, case, prompt)["no_invented_numbers"] is False


def test_regrade_rescores_stored_rows_without_calling_the_api(sample, scored, ledger, llm,
                                                              tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    same = "a" * 64
    first = llm.client([llm.response(oracle(case, entry, lines))])
    run_eval([case], Narrator(client=first), combined, flags, ledger, tmp_path, cases_sha256=same)

    stricter = Case(**{**case.__dict__, "expected_confidence": ["high"]})  # oracle says medium
    second = llm.client()
    rows = run_eval([stricter], Narrator(client=second), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256=same)
    assert second.calls == []
    assert rows[0]["metrics"]["confidence_in_band"] is False
    stored = json.loads((tmp_path / "results.jsonl").read_text().splitlines()[0])
    assert stored["metrics"]["confidence_in_band"] is False


def test_a_regrade_can_never_reach_the_api(sample, scored, ledger, llm, tmp_path):
    """A mistyped directory, a new case or --no-resume must not turn into a paid run."""
    case, prompt, entry, lines = sample
    combined, flags = scored
    same = "a" * 64
    quiet = llm.client()
    results = tmp_path / "results.jsonl"

    with pytest.raises(FileNotFoundError, match="nothing to re-grade"):
        run_eval([case], Narrator(client=quiet), combined, flags, ledger, tmp_path / "typo",
                 regrade=True, cases_sha256=same)
    assert quiet.calls == []
    assert not (tmp_path / "typo").exists()

    run_eval([case], Narrator(client=llm.client([llm.response(oracle(case, entry, lines))])),
             combined, flags, ledger, tmp_path, cases_sha256=same)
    others = combined[(combined["risk_score"] > 0) & (combined["entry_id"] != case.entry_id)]
    added = Case(others.iloc[0]["entry_id"], "y", others.iloc[0]["tests_fired"], "w")
    rows = run_eval([case, added], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256=same)
    assert quiet.calls == []
    assert [r["status"] for r in rows] == ["ok", "missing"]
    assert len(results.read_text().splitlines()) == 1  # the placeholder is not stored
    summary = aggregate(rows)
    assert (summary["graded"], summary["missing"]) == (1, 1)
    assert summary["rates"]["passed"] == 1.0  # missing rows are not averaged in
    report = render_markdown(rows, summary, cases=[case, added])
    assert "missing (no stored row, not narrated): 1" in report
    assert f"| {added.entry_id} |" in report and "missing:" in report
    assert "1/1" in report  # the baseline counts graded cases only

    with pytest.raises(RegradeError, match="no-resume"):
        run_eval([case], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                 regrade=True, resume=False, cases_sha256=same)
    with pytest.raises(RegradeError, match="cases_sha256"):
        run_eval([case], Narrator(client=quiet), combined, flags, ledger, tmp_path, regrade=True)
    assert quiet.calls == []
    assert not list(tmp_path.glob(".results-*"))  # the atomic rewrite leaves nothing behind


def test_a_case_that_only_errored_is_reported_as_such_by_a_regrade(sample, scored, ledger, llm,
                                                                    tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    others = combined[(combined["risk_score"] > 0) & (combined["entry_id"] != case.entry_id)]
    broken = Case(others.iloc[0]["entry_id"], "y", others.iloc[0]["tests_fired"], "w")
    client = llm.client([llm.response(oracle(case, entry, lines)), RuntimeError("socket closed")])
    run_eval([case, broken], Narrator(client=client), combined, flags, ledger, tmp_path,
             cases_sha256="a" * 64)

    quiet = llm.client()
    rows = run_eval([case, broken], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256="a" * 64)
    assert quiet.calls == []
    assert rows[1]["status"] == "missing"
    assert "errors.jsonl" in rows[1]["error"]
    assert "errors.jsonl" in render_markdown(rows, aggregate(rows))


def test_a_results_file_with_two_rows_for_one_case_is_refused_not_merged(sample, scored, ledger,
                                                                        llm, tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    results = tmp_path / "results.jsonl"
    run_eval([case], Narrator(client=llm.client([llm.response(oracle(case, entry, lines))])),
             combined, flags, ledger, tmp_path, cases_sha256="a" * 64)
    damaged = results.read_text() * 2  # what a hand edit or a bad merge could leave
    results.write_text(damaged)

    quiet = llm.client()
    with pytest.raises(RegradeError, match="more than one row"):
        run_eval([case], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                 regrade=True, cases_sha256="a" * 64)
    with pytest.raises(RegradeError, match="more than one row"):
        run_eval([case], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                 cases_sha256="a" * 64)  # a resumed run trusts the same file
    assert quiet.calls == []
    assert results.read_text() == damaged  # refused, not repaired


def test_grader_digest_tracks_the_grading_code_and_its_word_lists(monkeypatch):
    assert re.fullmatch(r"[0-9a-f]{64}", GRADER_SHA256)
    assert grader_digest() == GRADER_SHA256
    monkeypatch.setattr(narrative_eval, "FORBIDDEN_ASSERTIONS", ())  # a loosened check
    assert grader_digest() != GRADER_SHA256


def test_cases_digest_is_the_sha256_of_the_file_bytes(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text('{"cases": []}')
    assert cases_digest(path) == hashlib.sha256(b'{"cases": []}').hexdigest()


def test_rows_carry_the_case_file_hash_and_a_regrade_will_not_cross_a_change_quietly(
        sample, scored, ledger, llm, tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    same, edited = "a" * 64, "b" * 64
    results = tmp_path / "results.jsonl"

    first = llm.client([llm.response(oracle(case, entry, lines))])
    rows = run_eval([case], Narrator(client=first), combined, flags, ledger, tmp_path,
                    cases_sha256=same)
    assert rows[0]["cases_sha256"] == same
    assert rows[0]["grader_sha256"] == GRADER_SHA256
    assert json.loads(results.read_text().splitlines()[0])["cases_sha256"] == same

    # A grader fix under the same file re-grades freely and leaves no case-file
    # mark, but the grades it replaced stay on the row with what produced them.
    quiet = llm.client()
    rows = run_eval([case], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256=same)
    assert quiet.calls == []
    assert rows[0]["regraded_at"]
    assert "previous_cases_sha256" not in rows[0]
    assert rows[0]["metrics_history"] == [{
        "metrics": rows[0]["metrics"], "grader_sha256": GRADER_SHA256,
        "cases_sha256": same, "graded_at": rows[0]["graded_at"],
    }]
    provenance = aggregate(rows)["provenance"]
    assert provenance["rows_regraded_across_cases"] == 0
    assert (provenance["rows_regraded"], provenance["rows_with_changed_result"]) == (1, 0)
    assert provenance["grader_sha256"] == [GRADER_SHA256]

    # Edited expectations: refused outright, nothing rewritten, still no API call.
    stricter = Case(**{**case.__dict__, "expected_confidence": ["high"]})  # oracle says medium
    before = results.read_text()
    with pytest.raises(CasesChangedError, match="allow-cases-change"):
        run_eval([stricter], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                 regrade=True, cases_sha256=edited)
    assert results.read_text() == before
    assert quiet.calls == []

    # Allowed explicitly: re-graded, and the row remembers where it came from.
    rows = run_eval([stricter], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256=edited, allow_cases_change=True)
    assert rows[0]["metrics"]["confidence_in_band"] is False
    assert rows[0]["cases_sha256"] == edited
    assert rows[0]["previous_cases_sha256"] == same
    assert [h["metrics"]["passed"] for h in rows[0]["metrics_history"]] == [True, True]
    summary = aggregate(rows)
    assert summary["provenance"]["cases_sha256"] == [edited]
    assert summary["provenance"]["rows_regraded_across_cases"] == 1
    assert summary["provenance"]["previous_cases_sha256"] == [same]
    assert summary["provenance"]["rows_with_changed_result"] == 1  # passed went True -> False
    report = render_markdown(rows, summary, runs_dir=tmp_path)
    assert edited in report and same in report and GRADER_SHA256 in report
    assert "Provenance note" in report and "results.jsonl" in report
    assert "1 row(s) changed their overall result" in report

    # A second crossing keeps the earliest origin rather than the last.
    rows = run_eval([stricter], Narrator(client=quiet), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256="c" * 64, allow_cases_change=True)
    assert rows[0]["previous_cases_sha256"] == same


def test_rows_graded_before_provenance_existed_count_as_unrecorded(sample, scored, ledger, llm,
                                                                    tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    results = tmp_path / "results.jsonl"
    run_eval([case], Narrator(client=llm.client([llm.response(oracle(case, entry, lines))])),
             combined, flags, ledger, tmp_path, cases_sha256="a" * 64)
    stored = json.loads(results.read_text())
    del stored["cases_sha256"]  # what a row from before this field looks like
    results.write_text(json.dumps(stored) + "\n")

    with pytest.raises(CasesChangedError, match=UNRECORDED):
        run_eval([case], Narrator(client=llm.client()), combined, flags, ledger, tmp_path,
                 regrade=True, cases_sha256="a" * 64)
    rows = run_eval([case], Narrator(client=llm.client()), combined, flags, ledger, tmp_path,
                    regrade=True, cases_sha256="a" * 64, allow_cases_change=True)
    assert rows[0]["previous_cases_sha256"] == UNRECORDED
    report = render_markdown(rows, aggregate(rows))
    assert "hash was not recorded" in report


def test_the_report_prints_the_constant_answer_baseline_and_its_own_history(
        sample, scored, ledger, llm, tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    others = combined[(combined["risk_score"] > 0) & (combined["entry_id"] != case.entry_id)]
    wide = Case(others.iloc[0]["entry_id"], "y", others.iloc[0]["tests_fired"], "w",
                expected_confidence=["medium", "low"])
    narrow = Case(others.iloc[1]["entry_id"], "z", others.iloc[1]["tests_fired"], "w",
                  expected_confidence=["high"])
    cases = [case, wide, narrow]  # sample accepts high and medium

    assert confidence_baselines(cases) == {"high": (2, 3), "medium": (2, 3), "low": (1, 3)}

    client = llm.client([llm.response(oracle(case, entry, lines))] * 3)
    rows = run_eval(cases, Narrator(client=client), combined, flags, ledger, tmp_path)
    report = render_markdown(rows, aggregate(rows), cases=cases)
    assert 'always "high" 2/3 (67%)' in report
    assert 'always "medium" 2/3 (67%)' in report
    assert 'always "low" 1/3 (33%)' in report
    assert "more than one level on 2 of 3 cases" in report
    # The oracle answers "medium" every time: right on the two wide bands, wrong
    # on the narrow one - exactly a constant answer's score.
    assert "The notes passed it on 2/3, so they match the best constant answer" in report

    # The baseline is over the cases graded, not the whole file.
    partial = render_markdown(rows[:1], aggregate(rows[:1]), cases=cases)
    assert 'always "high" 1/1 (100%)' in partial
    assert "Confidence baseline" not in render_markdown(rows, aggregate(rows))

    # The grader's history is in the report itself, not only in the commit log.
    assert "## Grader notes" in report
    assert len(GRADER_NOTES) >= 2
    for date, note in GRADER_NOTES:
        assert date in report and note in report
    assert "88%" in report and "9,912" in report


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
    assert "(or is AU-C 240, the one standard the prompt cites)" in report  # the label is exact


def test_a_run_that_does_not_resume_never_deletes_stored_rows(sample, scored, ledger, llm,
                                                             tmp_path):
    case, prompt, entry, lines = sample
    combined, flags = scored
    run_eval([case], Narrator(client=llm.client([llm.response(oracle(case, entry, lines))])),
             combined, flags, ledger, tmp_path, resume=False)
    before = (tmp_path / "results.jsonl").read_text()

    again = llm.client([llm.response(oracle(case, entry, lines))])
    with pytest.raises(FileExistsError, match="never deleted"):
        run_eval([case], Narrator(client=again), combined, flags, ledger, tmp_path, resume=False)
    assert again.calls == []
    assert (tmp_path / "results.jsonl").read_text() == before


def test_default_runs_dir_dates_new_runs_and_finds_the_newest_to_regrade(tmp_path):
    root = tmp_path / "runs"
    fresh = default_runs_dir("claude-x", root=root)
    assert fresh.parent == root
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-claude-x", fresh.name)

    with pytest.raises(FileNotFoundError, match="runs-dir"):
        default_runs_dir("claude-x", regrade=True, root=root)
    for name in ("2026-09-23-claude-x", "2026-09-24-claude-x", "2026-09-25-claude-y"):
        (root / name).mkdir(parents=True)
    assert default_runs_dir("claude-x", regrade=True, root=root) == root / "2026-09-24-claude-x"


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

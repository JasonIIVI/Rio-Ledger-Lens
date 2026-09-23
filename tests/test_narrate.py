"""The narrative layer, tested without a network or an API key.

The ``llm`` fixture (tests/conftest.py) stands in for anthropic.Anthropic: it
records every request and replays canned responses shaped like the real ones,
including the thinking block current models return before their text.
"""

import json
from types import SimpleNamespace

import pytest

from ledgerlens import jets, narrate
from ledgerlens.narrate import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    NARRATIVE_SCHEMA,
    REQUIRED_KEYS,
    SYSTEM_PROMPT,
    TEST_REFERENCE,
    NarrativeError,
    Narrator,
    Usage,
    build_prompt,
    build_system_prompt,
    entry_context,
    format_narrative,
    parse_response,
    validate,
)


@pytest.fixture(scope="module")
def scored(ledger):
    flags = jets.run_all(ledger)
    return jets.score_entries(ledger, flags), flags


# --- prompts -----------------------------------------------------------------


def test_prompt_carries_the_entry_and_nothing_from_the_labels(ledger, scored):
    entries, flags = scored
    entry_id = entries.iloc[0]["entry_id"]
    row, entry_flags, lines = entry_context(entries, flags, ledger, entry_id)

    prompt = build_prompt(row, entry_flags, lines)

    assert entry_id in prompt
    assert "{:,.2f}".format(row["entry_amount"]) in prompt
    for test_id in entry_flags["test_id"]:
        assert test_id in prompt
    for line in lines.itertuples():
        assert line.account_name in prompt
    assert "is_anomaly" not in prompt
    assert "anomaly_type" not in prompt


def test_system_prompt_is_long_enough_to_cache_and_byte_stable():
    # Below ~1,024 tokens the API silently declines to cache. 5,000 characters
    # is a comfortable floor for English prose.
    assert len(SYSTEM_PROMPT) > 5000
    assert build_system_prompt() == SYSTEM_PROMPT
    assert "a question, not a finding" in SYSTEM_PROMPT
    for test_id in jets.REGISTRY:
        assert test_id in SYSTEM_PROMPT


def test_test_reference_matches_the_registry(ledger):
    assert [t[0] for t in TEST_REFERENCE] == list(jets.REGISTRY)
    assert [t[1] for t in TEST_REFERENCE] == [jets.REGISTRY[t[0]][0] for t in TEST_REFERENCE]
    fired = jets.run_all(ledger).drop_duplicates("test_id").set_index("test_id")["severity"]
    for test_id, _, severity, _ in TEST_REFERENCE:
        if test_id in fired.index:
            assert fired[test_id] == severity, test_id


def test_schema_is_the_contract():
    assert NARRATIVE_SCHEMA["required"] == list(REQUIRED_KEYS)
    assert NARRATIVE_SCHEMA["additionalProperties"] is False
    assert NARRATIVE_SCHEMA["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


# --- the request -------------------------------------------------------------


def test_request_uses_structured_output_and_caches_the_system_prompt(llm):
    client = llm.client()
    Narrator(client=client).narrate_one("prompt")

    request = client.calls[0]
    assert request["model"] == DEFAULT_MODEL
    assert request["max_tokens"] == DEFAULT_MAX_TOKENS
    assert request["system"][0]["text"] == SYSTEM_PROMPT
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": NARRATIVE_SCHEMA}
    assert request["output_config"]["effort"] == "medium"
    assert request["messages"] == [{"role": "user", "content": "prompt"}]


def test_text_block_is_found_after_a_thinking_block(llm):
    narrative = Narrator(client=llm.client([llm.response(thinking=True)])).narrate_one("p")
    assert narrative["summary"] == llm.good["summary"]
    assert narrative["confidence"] == "high"  # normalised


def test_truncated_response_is_an_error(llm):
    client = llm.client([llm.response(stop_reason="max_tokens")])
    with pytest.raises(NarrativeError, match="max_tokens"):
        Narrator(client=client).narrate_one("p")


def test_refusal_is_an_error(llm):
    client = llm.client([llm.response(stop_reason="refusal")])
    with pytest.raises(NarrativeError, match="declined"):
        Narrator(client=client).narrate_one("p")


def test_response_without_text_is_an_error(llm):
    client = llm.client([llm.response(text_block=False)])
    with pytest.raises(NarrativeError, match="no text block"):
        Narrator(client=client).narrate_one("p")


def test_usage_is_accumulated_from_the_api_not_estimated(llm):
    cold = llm.usage(cache_read_input_tokens=0, cache_creation_input_tokens=1500)
    client = llm.client([llm.response(usage=cold), llm.response(), llm.response()])
    narrator = Narrator(client=client)
    for _ in range(3):
        narrator.narrate_one("p")

    assert narrator.usage.requests == 3
    assert narrator.usage.input_tokens == 900
    assert narrator.usage.cache_read_input_tokens == 3000
    assert narrator.usage.cache_creation_input_tokens == 1500
    assert 0 < narrator.usage.cache_read_share < 1
    assert "read from cache" in narrator.usage.describe()


def test_usage_tolerates_missing_counters():
    usage = Usage()
    usage.add(SimpleNamespace(input_tokens=10, output_tokens=None))
    assert usage.input_tokens == 10
    assert usage.output_tokens == 0
    assert usage.cache_read_input_tokens == 0


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(NarrativeError, match=r"\.env"):
        _ = Narrator().client


def test_real_client_is_constructed_from_the_environment(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    client = Narrator().client
    assert isinstance(client, anthropic.Anthropic)  # no request is made
    assert "anthropic-workspace-id" not in client.default_headers


def test_workspace_id_becomes_a_header_when_set(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    assert Narrator().client.default_headers["anthropic-workspace-id"] == "wrkspc_test"


# --- parsing and validation ---------------------------------------------------


def test_parse_tolerates_a_markdown_fence(llm):
    fenced = "```json\n" + json.dumps(llm.good) + "\n```"
    assert parse_response(fenced)["confidence"] == "high"


def test_parse_rejects_non_json():
    with pytest.raises(NarrativeError, match="valid JSON"):
        parse_response("Here is the note you asked for.")


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.pop("summary"), "missing key"),
    (lambda d: d.update(evidence_to_request="a string"), "must be a list"),
    (lambda d: d.update(evidence_to_request=["", "  "]), "must not be empty"),
    (lambda d: d.update(confidence="certain"), "confidence"),
    (lambda d: d.update(why_flagged="   "), "why_flagged"),
])
def test_validate_rejects_broken_contracts(mutate, message, llm):
    payload = dict(llm.good)
    mutate(payload)
    with pytest.raises(NarrativeError, match=message):
        validate(payload)


def test_validate_rejects_a_non_object():
    with pytest.raises(NarrativeError, match="JSON object"):
        validate(["not", "an", "object"])


# --- batches ----------------------------------------------------------------


def test_narrate_takes_the_highest_risk_first_and_honours_top_n_and_skip(ledger, scored, llm):
    entries, flags = scored
    shuffled = entries.sample(frac=1, random_state=1).reset_index(drop=True)
    expected = entries[entries["risk_score"] > 0].sort_values(
        ["risk_score", "entry_amount", "entry_id"], ascending=[False, False, True]
    )["entry_id"].tolist()
    skip = {expected[0]}

    result = Narrator(client=llm.client()).narrate(shuffled, flags, ledger, top_n=3, skip=skip)

    assert list(result.narratives) == expected[1:4]
    assert result.entries_requested == 3
    assert result.failures == {}
    assert result.success_rate == 1.0


def test_narrate_records_a_failure_without_stopping_the_batch(ledger, scored, llm):
    entries, flags = scored
    client = llm.client([llm.response(), llm.response(stop_reason="max_tokens"), llm.response()])

    result = Narrator(client=client).narrate(entries, flags, ledger, top_n=3)

    assert len(result.narratives) == 2
    assert len(result.failures) == 1
    assert "max_tokens" in next(iter(result.failures.values()))
    assert result.usage.requests == 3  # the truncated call still cost tokens
    assert "2/3" in result.describe()


def test_auth_failure_aborts_the_batch_instead_of_failing_every_entry(ledger, scored, llm):
    entries, flags = scored

    class AuthenticationError(Exception):
        pass

    client = llm.client([AuthenticationError("401 invalid x-api-key")])
    with pytest.raises(NarrativeError, match="authentication"):
        Narrator(client=client).narrate(entries, flags, ledger, top_n=5)
    assert len(client.calls) == 1


def test_missing_key_fails_the_batch_once_not_per_entry(ledger, scored, monkeypatch):
    entries, flags = scored
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(NarrativeError, match=r"\.env"):
        Narrator().narrate(entries, flags, ledger, top_n=5)


def test_entry_context_unknown_entry_raises(ledger, scored):
    entries, flags = scored
    with pytest.raises(KeyError):
        entry_context(entries, flags, ledger, "JE-0000-000000")


def test_format_narrative_renders_evidence_as_bullets(llm):
    text = format_narrative(validate(llm.good))
    assert text.startswith(llm.good["summary"])
    assert "  - The signed sales contract" in text
    assert text.endswith("Confidence: high")


def test_default_model_is_the_documented_one():
    assert narrate.DEFAULT_MODEL == "claude-opus-5"

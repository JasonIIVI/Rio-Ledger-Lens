"""Shared fixtures.

The full two-year ledger is generated once per session because it is the
expensive part; individual tests slice it rather than regenerating.
"""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from ledgerlens.generate import generate_ledger
from ledgerlens.ingest import prepare


@pytest.fixture(scope="session")
def raw_ledger():
    return generate_ledger(start=date(2024, 1, 1), end=date(2025, 12, 31), entries_per_day=10)


@pytest.fixture(scope="session")
def ledger(raw_ledger):
    lines, _ = raw_ledger
    return prepare(lines)


@pytest.fixture(scope="session")
def labels(raw_ledger):
    _, lbl = raw_ledger
    return lbl


@pytest.fixture(scope="session")
def small_ledger():
    lines, lbl = generate_ledger(
        start=date(2024, 1, 1), end=date(2024, 3, 31), entries_per_day=6
    )
    return prepare(lines), lbl


# --- a stand-in for the Claude API --------------------------------------------
# Shaped like the real thing, including the thinking block current models put
# before their text, so the narrative code is exercised on the response shape
# it will actually see.

GOOD_NARRATIVE = {
    "summary": "Manual credit of $75,000.00 to Sales Revenue two days before period close.",
    "why_flagged": "JET-05 fired because the credit lands within three days of period end. "
                   "JET-01 adds that the amount is an exact round thousand.",
    "evidence_to_request": [
        "The signed sales contract supporting the $75,000.00 credit",
        "Shipping documents to test cut-off against the posting date",
    ],
    "suggested_control": "Period-end manual journal approval",
    "confidence": "High",
}


def llm_usage(**overrides):
    base = {"input_tokens": 300, "output_tokens": 120,
            "cache_read_input_tokens": 1500, "cache_creation_input_tokens": 0}
    base.update(overrides)
    return SimpleNamespace(**base)


def llm_response(payload=GOOD_NARRATIVE, stop_reason="end_turn", thinking=True,
                 usage=None, text_block=True, model="claude-opus-5"):
    content = [SimpleNamespace(type="thinking", thinking="")] if thinking else []
    if text_block:
        text = json.dumps(payload) if isinstance(payload, dict) else payload
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None,
                           usage=usage or llm_usage(), model=model)


class FakeClient:
    """Records every request; replays responses in order, raising any exception given."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._responses:
            item = self._responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return llm_response()


@pytest.fixture
def llm():
    """Factory bundle: ``llm.client([...])``, ``llm.response(...)``, ``llm.usage(...)``."""
    return SimpleNamespace(client=FakeClient, response=llm_response, usage=llm_usage,
                           good=dict(GOOD_NARRATIVE))

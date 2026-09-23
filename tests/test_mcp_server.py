"""The MCP registration layer, exercised through the SDK's in-memory client.

Skipped where the SDK cannot be installed (Python 3.9); the analytics behind
the tools are covered on every Python by tests/test_ledger_context.py.
"""

import pytest

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from ledgerlens import mcp_server  # noqa: E402
from ledgerlens.ledger_context import LedgerContext  # noqa: E402

TOOLS = {
    "ledgerlens_summary", "ledgerlens_top_exceptions", "ledgerlens_explain_entry",
    "ledgerlens_search_entries", "ledgerlens_benford", "ledgerlens_review_status",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(small_ledger):
    ledger, _ = small_ledger
    mcp_server.use_context(LedgerContext(ledger))
    async with Client(mcp_server.mcp, raise_exceptions=True) as c:
        yield c


@pytest.mark.anyio
async def test_every_tool_is_registered_and_read_only(client):
    listed = await client.list_tools()
    tools = {t.name: t for t in listed.tools}
    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description
    assert "limit" in tools["ledgerlens_top_exceptions"].input_schema["properties"]


@pytest.mark.anyio
async def test_top_exceptions_round_trip(client):
    result = await client.call_tool("ledgerlens_top_exceptions", {"limit": 3})
    assert not result.is_error
    assert result.structured_content["count"] == 3
    assert result.structured_content["entries"][0]["reasons"]


@pytest.mark.anyio
async def test_unknown_entry_is_a_tool_error_the_model_can_read(client):
    result = await client.call_tool("ledgerlens_explain_entry", {"entry_id": "JE-0000-000000"})
    assert result.is_error
    assert "JE-0000-000000" in result.content[0].text


@pytest.mark.anyio
async def test_arguments_are_validated_before_the_tool_runs(client):
    result = await client.call_tool("ledgerlens_top_exceptions", {"limit": 999})
    assert result.is_error


@pytest.mark.anyio
async def test_review_status_without_a_database_is_zeros_not_an_error(client):
    result = await client.call_tool("ledgerlens_review_status", {})
    assert not result.is_error
    assert result.structured_content["exists"] is False
    assert result.structured_content["decided"] == 0

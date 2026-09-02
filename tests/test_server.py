from typing import Any
from collections import deque

import pytest
from mcp import Client

from mcp_logseq_db.server import create_server


class FakeClient:
    observed_methods: frozenset[str] = frozenset()
    experimental_writes_enabled = True

    async def call(self, method: str, args: list[Any]) -> Any:
        return []


class RecordingClient(FakeClient):
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)

    async def call(self, method: str, args: list[Any]) -> Any:
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_server_exposes_only_verified_read_tools() -> None:
    server = create_server(FakeClient())  # type: ignore[arg-type]

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "capabilities",
        "check_current_is_db_graph",
        "get_app_info",
        "get_current_graph",
        "list_pages",
        "get_page_data",
        "search",
        "list_properties",
        "list_tags",
        "upsert_nodes",
        "get_block",
        "get_block_tree",
        "create_page",
        "create_top_level_block",
        "insert_block",
        "delete_block",
        "move_block",
        "upsert_block",
        "rename_page",
        "delete_page",
        "recycle_page",
        "datascript_query",
        "get_all_properties",
        "get_property",
        "get_all_tags",
        "get_tag",
        "get_tags_by_name",
        "get_tag_objects",
        "upsert_property",
        "remove_property",
        "create_tag",
        "rename_tag",
        "delete_tag",
        "add_tag_property",
        "remove_tag_property",
        "set_tag_parent",
        "remove_tag_extends",
        "upsert_block_property",
        "remove_block_property",
        "add_block_tag",
        "remove_block_tag",
        "add_page_tag",
        "remove_page_tag",
        "set_block_icon",
        "remove_block_icon",
    }


@pytest.mark.asyncio
async def test_server_exposes_no_experimental_tools() -> None:
    client = FakeClient()
    client.experimental_writes_enabled = False

    tools = await create_server(client).list_tools()  # type: ignore[arg-type]

    assert not any(tool.name.endswith("_experimental") for tool in tools)


@pytest.mark.asyncio
async def test_direct_read_failure_is_visible_to_mcp_client() -> None:
    class ErrorClient(FakeClient):
        async def call(self, method: str, args: list[Any]) -> Any:
            raise RuntimeError("Logseq DB worker may be wedged")

    async with Client(create_server(ErrorClient())) as client:  # type: ignore[arg-type]
        result = await client.call_tool("search", {"query": "test"})

    assert result.is_error is True
    assert "DB worker may be wedged" in result.content[0].text
    assert '"failure_stage": "readback_mismatch"' in result.content[0].text


@pytest.mark.asyncio
async def test_validation_failure_returns_diagnostic_envelope() -> None:
    async with Client(create_server(FakeClient())) as client:  # type: ignore[arg-type]
        result = await client.call_tool("get_block", {"block_uuid": "not-a-uuid"})

    assert result.is_error is True
    assert '"verified": false' in result.content[0].text
    assert '"failure_stage": "validation"' in result.content[0].text
    assert "Expected an exact UUID" in result.content[0].text
    assert '"suggestion": "Use block_uuid as the exact UUID of a block."' in result.content[0].text


@pytest.mark.asyncio
async def test_move_validation_failure_suggests_public_input_format() -> None:
    async with Client(create_server(FakeClient())) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "move_block",
            {"block_uuid": "bad", "target_uuid": "also-bad", "placement": "child"},
        )

    assert result.is_error is True
    assert "Use distinct exact block and target UUIDs" in result.content[0].text
    assert "positional" not in result.content[0].text


@pytest.mark.asyncio
async def test_mutation_verification_failure_returns_observed_state() -> None:
    block_uuid = "87654321-4321-8765-4321-876543218765"
    ident = ":user.property/status"
    previous = {"id": 11, "uuid": block_uuid, ident: "before"}
    observed = {"id": 11, "uuid": block_uuid, ident: "unexpected"}
    client_impl = RecordingClient([
        {"ident": ident, "id": 42, "type": "default"},
        previous,
        {"ok": True},
        observed,
        ["unexpected"],
    ])

    async with Client(create_server(client_impl)) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "upsert_block_property",
            {"block_uuid": block_uuid, "property_ident": ident, "value": "expected"},
        )

    assert result.is_error is True
    assert '"error_type": "MutationVerificationError"' in result.content[0].text
    assert '"previous_state": {"id": 11' in result.content[0].text
    assert '"observed_state": {"id": 11' in result.content[0].text
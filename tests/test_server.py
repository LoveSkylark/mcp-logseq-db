from typing import Any

import pytest
from mcp import Client

from mcp_logseq_db.server import create_server


class FakeClient:
    observed_methods: frozenset[str] = frozenset()
    experimental_writes_enabled = True

    async def call(self, method: str, args: list[Any]) -> Any:
        return []


@pytest.mark.asyncio
async def test_server_exposes_only_verified_read_tools() -> None:
    server = create_server(FakeClient())  # type: ignore[arg-type]

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "db_capabilities",
        "db_check_current_is_db_graph",
        "db_get_app_info",
        "db_get_current_graph",
        "db_list_pages",
        "db_get_page_data",
        "db_search",
        "db_list_properties",
        "db_list_tags",
        "db_upsert_nodes",
        "db_get_block",
        "db_create_page",
        "db_create_top_level_block",
        "db_insert_block_experimental",
        "db_move_block_experimental",
        "db_delete_block_experimental",
        "db_upsert_block",
        "db_rename_page",
        "db_delete_page",
        "db_recycle_page",
        "db_q",
        "db_custom_query",
        "db_datascript_query",
        "db_get_all_properties",
        "db_get_property",
        "db_get_all_tags",
        "db_get_tag",
        "db_get_tags_by_name",
        "db_get_tag_objects",
        "db_upsert_property",
        "db_remove_property",
        "db_create_tag",
        "db_rename_tag",
        "db_delete_tag",
        "db_add_tag_property",
        "db_remove_tag_property",
        "db_add_tag_extends",
        "db_remove_tag_extends",
        "db_upsert_block_property",
        "db_remove_block_property",
        "db_add_block_tag",
        "db_remove_block_tag",
        "db_set_block_icon",
        "db_remove_block_icon",
    }


@pytest.mark.asyncio
async def test_server_hides_experimental_tools_by_default() -> None:
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
        result = await client.call_tool("db_search", {"query": "test"})

    assert result.is_error is True
    assert "DB worker may be wedged" in result.content[0].text
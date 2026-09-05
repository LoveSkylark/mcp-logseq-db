"""
The MCP surface.

Two things matter here and nothing else does: which tools exist, and what a
caller sees when one fails. The behaviour behind each tool is covered by
test_content and test_mutations against fakes that model the graph; repeating
it through the server layer would only test the wiring twice.
"""

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_logseq_db.capabilities import TOOL_ROUTES
from mcp_logseq_db.server import create_server

# The contract. A tool added or renamed without updating this set is a change
# to the public surface, so it should require a deliberate edit here.
EXPECTED_TOOLS = {
    "capabilities",
    # Pages
    "getPageUUID", "getPage",
    # Blocks
    "getBlockUUID", "getBlock", "getBlockTree", "createBlock",
    "createManyBlocks", "createPageofBlocks", "updateBlock", "removeBlock",
    # Tags
    "getTagUUID", "getTag", "getTagUsers", "creatTag", "deleteTag",
    "addTag", "removeTag",
    # Properties
    "getPropertyIndent", "getProperyUsers", "createProperty",
    "deleteProperty", "addProperty", "removeProperty",
    # Lists
    "listPages", "listJournals", "listTags", "listProperties",
    "listClosedValues", "listOrphanTags", "listOrphanProperties",
    "listAssets", "listStatus", "listRecycled",
}

# Removed in the rewrite. Each is listed with why, so a future reader does not
# restore one by assuming it was an oversight.
REMOVED_TOOLS = {
    "insert_block":            "no verified route; nesting is createBlock",
    "move_block":              "no verified route at all",
    "create_top_level_block":  "createBlock covers page and block parents",
    "add_page_tag":            "a page is a block; addTag takes either",
    "remove_page_tag":         "a page is a block; removeTag takes either",
    "add_block_tag":           "renamed to addTag",
    "remove_block_tag":        "renamed to removeTag",
    "upsert_page_property":    "a page is a block; addProperty takes either",
    "upsert_block_property":   "renamed to addProperty",
    "set_block_icon":          "no tool needs it",
    "remove_block_icon":       "no tool needs it",
    "rename_tag":              "routed through renamePage; untested",
    "add_tag_property":        "no tool needs it",
    "set_tag_parent":          "no tool needs it",
    "delete_page":             "documented as an alias of recycle_page",
    "recycle_page":            "reversibility unresolved; neither exposed",
    "search":                  "not in the tool surface",
    "get_page_data":           "replaced by getPage with a detail selector",
    "datascript_query":        "no raw query escape hatch is exposed",
}


class FakeClient:
    write_policy = None
    writable_property_prefix = "plugin.property._test_plugin/"

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, list[Any]]] = []

    async def call(self, method: str, args: list[Any]) -> Any:
        self.calls.append((method, args))
        outcome = self.responses.get(method, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def tool_names() -> set[str]:
    server = create_server(FakeClient())  # type: ignore[arg-type]
    return {tool.name for tool in await server.list_tools()}


# ----------------------------------------------------------- the surface

async def test_server_exposes_exactly_the_expected_tools() -> None:
    assert await tool_names() == EXPECTED_TOOLS


@pytest.mark.parametrize(
    ("name", "reason"), sorted(REMOVED_TOOLS.items()))
async def test_removed_tools_stay_removed(name: str, reason: str) -> None:
    assert name not in await tool_names(), f"{name} was removed: {reason}"


async def test_every_exposed_tool_has_a_route_or_is_meta() -> None:
    """A tool with no entry in TOOL_ROUTES cannot be reported by capabilities,
    so it would be invisible to a caller checking availability."""
    names = await tool_names()
    unrouted = names - set(TOOL_ROUTES) - {"capabilities", "getBlockTree"}
    assert not unrouted, f"tools with no declared route: {sorted(unrouted)}"


async def test_no_tool_name_leaks_the_api_method_it_uses() -> None:
    assert not any(name.startswith("logseq") for name in await tool_names())


async def test_every_tool_has_a_description() -> None:
    """The description carries the constraints -- which identifier a tool
    takes, whether a route is unverified. A tool without one is a trap."""
    server = create_server(FakeClient())  # type: ignore[arg-type]
    missing = [t.name for t in await server.list_tools() if not t.description]
    assert not missing


# ------------------------------------------------------- error envelope

async def test_failures_are_returned_as_a_structured_envelope() -> None:
    """A caller needs to distinguish a bad argument from a Logseq error from
    a write that silently did nothing, so the stage is machine-readable."""
    server = create_server(FakeClient())  # type: ignore[arg-type]

    with pytest.raises(ToolError) as caught:
        await server.call_tool("getTag", {"tag_uuid": "TAG-TEST"})

    payload = json.loads(str(caught.value))
    assert payload["verified"] is False
    assert payload["failure_stage"] == "validation"
    assert "title or name" in payload["diagnostic"]
    assert payload["suggestion"]


async def test_a_wrong_identifier_type_is_refused_before_the_api() -> None:
    client = FakeClient()
    server = create_server(client)  # type: ignore[arg-type]

    with pytest.raises(ToolError):
        await server.call_tool(
            "addTag",
            {"target_uuid": ":user.class/xzy", "tag_uuid": "TAG-TEST"})

    assert client.calls == []


async def test_the_offending_argument_is_named() -> None:
    """addTag takes two UUIDs; a message that does not say which one is wrong
    leaves the caller guessing."""
    server = create_server(FakeClient())  # type: ignore[arg-type]
    good = "6a9a1a1c-cede-430f-8768-7a3609d4039b"

    with pytest.raises(ToolError) as caught:
        await server.call_tool(
            "addTag", {"target_uuid": good, "tag_uuid": "not-a-uuid"})

    assert "tag_uuid" in json.loads(str(caught.value))["diagnostic"]


# --------------------------------------------------------- probe_writes

async def test_probe_writes_setting_reaches_capability_discovery() -> None:
    """Off, capabilities makes ~11 fewer calls and marks writes unknown."""
    client = FakeClient({
        "logseq.DB.getAppInfo": {"version": "2.0.1", "supportDb": True},
        "logseq.DB.checkCurrentIsDbGraph": True,
    })
    server = create_server(client, probe_writes=False)  # type: ignore[arg-type]

    await server.call_tool("capabilities", {})

    assert not any(method == "logseq.DB.removeBlock"
                   for method, _ in client.calls)
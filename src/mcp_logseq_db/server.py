"""Runnable MCP server exposing only live-verified Logseq DB reads."""

import asyncio
from functools import wraps
import json
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .access import WriteAccessPolicy
from .capabilities import CapabilityDiscovery
from .client import (
    LogseqAPIError,
    LogseqDBClient,
    LogseqProtocolError,
    WriteCircuitOpenError,
)
from .content import VerifiedContent
from .mutations import MutationVerificationError, VerifiedMutations
from .settings import Settings


def create_server(client: LogseqDBClient) -> MCPServer:
    server = MCPServer(
        "mcp-logseq-db",
        description="Narrow DB-native MCP server for Logseq 2.x",
        instructions=(
            "Use exact DB identifiers. Prefer promoted DB reads and writes. "
            "Treat verified_state as evidence, never a raw null response. "
            "Structural writes may time out and are successful only when "
            "their result reports verified=true."
        ),
    )

    register_tool = server.tool

    def safe_tool(*args, **kwargs):
        register = register_tool(*args, **kwargs)

        def decorator(method):
            @wraps(method)
            async def wrapped(*method_args, **method_kwargs):
                try:
                    return await method(*method_args, **method_kwargs)
                except Exception as error:
                    payload: dict[str, Any] = {
                        "verified": False,
                        "failure_stage": _failure_stage(error),
                        "diagnostic": str(error),
                        "suggestion": _failure_suggestion(method.__name__, error),
                        "error_type": type(error).__name__,
                        "response": None,
                        "verified_state": None,
                        "verified_entities": [],
                        "observed_entities": [],
                        "previous_state": None,
                        "previous_entities": [],
                    }
                    if isinstance(error, MutationVerificationError):
                        payload.update(error.result.to_dict())
                    raise ToolError(json.dumps(payload)) from error

            return register(wrapped)

        return decorator

    server.tool = safe_tool  # type: ignore[method-assign]

    @server.tool(name="capabilities", structured_output=True)
    async def capabilities() -> dict[str, Any]:
        """Probe and report DB methods supported by the connected instance."""
        capabilities = await CapabilityDiscovery(client).discover()
        return capabilities.to_dict()

    @server.tool(name="check_current_is_db_graph")
    async def check_current_is_db_graph() -> bool:
        """Return whether the current Logseq graph is a DB graph."""
        try:
            async with asyncio.timeout(20):
                return bool(
                    await client.call("logseq.DB.checkCurrentIsDbGraph", [])
                )
        except TimeoutError as error:
            raise RuntimeError(
                "Logseq DB health check exceeded 20 seconds; restart Logseq "
                "itself if its DB worker is wedged"
            ) from error

    @server.tool(name="get_app_info")
    async def get_app_info() -> Any:
        """Return connected Logseq version and DB support metadata."""
        return await client.call("logseq.DB.getAppInfo", [])

    @server.tool(name="get_current_graph")
    async def get_current_graph() -> Any:
        """Return the current graph identity and path metadata."""
        return await client.call("logseq.DB.getCurrentGraph", [])

    @server.tool(name="list_pages")
    async def list_pages(expand: bool = False) -> Any:
        """List DB pages with UUIDs and optional expanded metadata."""
        return await client.call("logseq.DB.listPages", [{"expand": expand}])

    @server.tool(name="get_page_data")
    async def get_page_data(page_name_or_uuid: str) -> Any:
        """Read a page and only blocks directly parented by it. Nested descendants require get_block_tree or a Datascript parent/page query."""
        return await client.call("logseq.DB.getPageData", [page_name_or_uuid])

    @server.tool(name="search")
    async def search(query: str) -> Any:
        """Search the DB graph through the verified DB namespace alias."""
        return await client.call("logseq.DB.search", [query])

    @server.tool(name="list_properties")
    async def list_properties(expand: bool = False) -> Any:
        """List DB properties with optional expanded metadata."""
        return await client.call("logseq.DB.listProperties", [{"expand": expand}])

    @server.tool(name="list_tags")
    async def list_tags(expand: bool = False) -> Any:
        """List DB tags with optional expanded metadata."""
        return await client.call("logseq.DB.listTags", [{"expand": expand}])

    @server.tool(name="upsert_nodes", structured_output=True)
    async def upsert_nodes(
        operations: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        """Create pages/top-level blocks or edit block titles. Always validates first; nested block targets are rejected because they corrupt ownership."""
        return (
            await VerifiedContent(client).upsert_nodes(operations, dry_run=dry_run)
        ).to_dict()

    @server.tool(name="get_block", structured_output=True)
    async def get_block(block_uuid: str) -> dict[str, Any]:
        """Read one exact block UUID through Datascript. Missing UUIDs return found=false rather than a tool error."""
        return await VerifiedContent(client).find_block(block_uuid)

    @server.tool(name="get_block_tree", structured_output=True)
    async def get_block_tree(
        block_uuid: str,
        max_depth: int = 20,
        max_nodes: int = 1000,
    ) -> dict[str, Any]:
        """Read a recursive block subtree through two bounded Datascript calls. Returns truncated=true when a limit stops traversal."""
        return await VerifiedContent(client).find_block_tree(
            block_uuid,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

    @server.tool(name="create_page", structured_output=True)
    async def create_page(title: str, dry_run: bool = False) -> dict[str, Any]:
        """Create one page through DB.upsertNodes with validation and read-back."""
        return (
            await VerifiedContent(client).create_page(title, dry_run=dry_run)
        ).to_dict()

    @server.tool(name="create_top_level_block", structured_output=True)
    async def create_top_level_block(
        page_uuid: str,
        title: str,
        tag_uuids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create one top-level block on an exact page UUID. This cannot create a nested child or control sibling position."""
        return (
            await VerifiedContent(client).create_top_level_block(
                page_uuid,
                title,
                tag_uuids=tag_uuids,
                dry_run=dry_run,
            )
        ).to_dict()

    @server.tool(name="delete_block", structured_output=True)
    async def delete_block(block_uuid: str) -> dict[str, Any]:
        """Delete the subtree rooted at an exact block UUID through Logseq's supported DB worker path and verify every UUID is absent."""
        return (await VerifiedContent(client).delete_block(block_uuid)).to_dict()

    @server.tool(name="insert_block", structured_output=True)
    async def insert_block(
        target_uuid: str,
        title: str,
        placement: Literal["child", "after"] = "child",
    ) -> dict[str, Any]:
        """Insert a titled block as the last child of or sibling after an exact block UUID, then verify its structure."""
        return (
            await VerifiedContent(client).insert_block(
                target_uuid, title, placement=placement
            )
        ).to_dict()

    @server.tool(name="move_block", structured_output=True)
    async def move_block(
        block_uuid: str,
        target_uuid: str,
        placement: Literal["child", "after"] = "child",
    ) -> dict[str, Any]:
        """Move an exact block subtree as the last child of or sibling after an exact target UUID, then verify its structure."""
        return (
            await VerifiedContent(client).move_block(
                block_uuid, target_uuid, placement=placement
            )
        ).to_dict()

    @server.tool(name="upsert_block", structured_output=True)
    async def upsert_block(
        block_uuid: str, title: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Edit one existing block title through DB.upsertNodes. This does not create, move, nest, or delete blocks."""
        return (
            await VerifiedContent(client).upsert_block(
                block_uuid, title, dry_run=dry_run
            )
        ).to_dict()

    @server.tool(name="rename_page", structured_output=True)
    async def rename_page(page_uuid: str, new_title: str) -> dict[str, Any]:
        """Rename an exact page UUID and verify its new title."""
        return (await VerifiedContent(client).rename_page(page_uuid, new_title)).to_dict()

    @server.tool(name="delete_page", structured_output=True)
    async def delete_page(
        page_uuid: str, acknowledge_reference_rewrite: bool = False
    ) -> dict[str, Any]:
        """Compatibility alias for recycle_page."""
        return (
            await VerifiedContent(client).delete_page(
                page_uuid,
                acknowledge_reference_rewrite=acknowledge_reference_rewrite,
            )
        ).to_dict()

    @server.tool(name="recycle_page", structured_output=True)
    async def recycle_page(
        page_uuid: str, acknowledge_reference_rewrite: bool = False
    ) -> dict[str, Any]:
        """Recycle a page after checking for reference-rewrite side effects."""
        return (
            await VerifiedContent(client).delete_page(
                page_uuid,
                acknowledge_reference_rewrite=acknowledge_reference_rewrite,
            )
        ).to_dict()

    @server.tool(name="datascript_query")
    async def datascript_query(query: str) -> Any:
        """Run a read-only Datascript query through logseq.DB.datascriptQuery."""
        return await client.call("logseq.DB.datascriptQuery", [query])

    @server.tool(name="get_all_properties")
    async def get_all_properties() -> Any:
        """Return all DB property definitions."""
        return await client.call("logseq.DB.getAllProperties", [])

    @server.tool(name="get_property")
    async def get_property(property_ident: str) -> Any:
        """Get a property by its exact namespaced ident."""
        if not property_ident.startswith(":") or "/" not in property_ident:
            raise ValueError("property_ident must be an exact namespaced ident")
        return await client.call("logseq.DB.getProperty", [property_ident])

    @server.tool(name="get_all_tags")
    async def get_all_tags() -> Any:
        """Return all DB tags/classes."""
        return await client.call("logseq.DB.getAllTags", [])

    @server.tool(name="get_tag")
    async def get_tag(identifier: str) -> Any:
        """Get a tag by exact ident, UUID, or title."""
        return await client.call("logseq.DB.getTag", [identifier])

    @server.tool(name="get_tags_by_name")
    async def get_tags_by_name(title: str) -> Any:
        """Get tags matching an exact title."""
        return await client.call("logseq.DB.getTagsByName", [title])

    @server.tool(name="get_tag_objects")
    async def get_tag_objects(identifier: str) -> Any:
        """Return a mixed list of pages and blocks associated with a tag ident, UUID, or title."""
        return await client.call("logseq.DB.getTagObjects", [identifier])

    @server.tool(name="upsert_property", structured_output=True)
    async def upsert_property(
        title: str,
        schema: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a property and verify it by its returned exact ident."""
        result = await VerifiedMutations(client).upsert_property(
            title, schema, options
        )
        return result.to_dict()

    @server.tool(name="remove_property", structured_output=True)
    async def remove_property(property_ident: str) -> dict[str, Any]:
        """Remove an exact property ident and verify that it is absent."""
        result = await VerifiedMutations(client).remove_property(property_ident)
        return result.to_dict()

    @server.tool(name="create_tag", structured_output=True)
    async def create_tag(
        title: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a tag and verify it through its returned exact identity."""
        return (await VerifiedMutations(client).create_tag(title, options)).to_dict()

    @server.tool(name="rename_tag", structured_output=True)
    async def rename_tag(tag_uuid: str, new_title: str) -> dict[str, Any]:
        """Rename one exact tag UUID and verify its new title."""
        return (await VerifiedMutations(client).rename_tag(tag_uuid, new_title)).to_dict()

    @server.tool(name="delete_tag", structured_output=True)
    async def delete_tag(
        tag_uuid: str, acknowledge_child_reparent: bool = False
    ) -> dict[str, Any]:
        """Permanently delete one tag, requiring acknowledgement before child tags are reparented."""
        return (
            await VerifiedMutations(client).delete_tag(
                tag_uuid, acknowledge_child_reparent=acknowledge_child_reparent
            )
        ).to_dict()

    @server.tool(name="add_tag_property", structured_output=True)
    async def add_tag_property(tag_uuid: str, property_ident: str) -> dict[str, Any]:
        """Add an exact property to an exact tag UUID and verify the relation."""
        return (
            await VerifiedMutations(client).add_tag_property(tag_uuid, property_ident)
        ).to_dict()

    @server.tool(name="remove_tag_property", structured_output=True)
    async def remove_tag_property(tag_uuid: str, property_ident: str) -> dict[str, Any]:
        """Remove an exact property from an exact tag UUID and verify removal."""
        return (
            await VerifiedMutations(client).remove_tag_property(tag_uuid, property_ident)
        ).to_dict()

    @server.tool(name="set_tag_parent", structured_output=True)
    async def set_tag_parent(
        tag_uuid: str,
        parent_tag_uuid: str,
        acknowledge_replacement: bool = False,
    ) -> dict[str, Any]:
        """Set one tag parent, requiring acknowledgement before replacement."""
        return (
            await VerifiedMutations(client).set_tag_parent(
                tag_uuid,
                parent_tag_uuid,
                acknowledge_replacement=acknowledge_replacement,
            )
        ).to_dict()

    @server.tool(name="remove_tag_extends", structured_output=True)
    async def remove_tag_extends(tag_uuid: str, parent_tag_uuid: str) -> dict[str, Any]:
        """Remove and verify inheritance between two exact tag UUIDs."""
        return (
            await VerifiedMutations(client).remove_tag_extends(tag_uuid, parent_tag_uuid)
        ).to_dict()

    @server.tool(name="upsert_block_property", structured_output=True)
    async def upsert_block_property(
        block_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set an exact property on an exact block UUID and verify its value."""
        return (
            await VerifiedMutations(client).upsert_block_property(
                block_uuid, property_ident, value, options
            )
        ).to_dict()

    @server.tool(name="remove_block_property", structured_output=True)
    async def remove_block_property(
        block_uuid: str, property_ident: str
    ) -> dict[str, Any]:
        """Remove an exact property from a block UUID and verify its absence."""
        return (
            await VerifiedMutations(client).remove_block_property(
                block_uuid, property_ident
            )
        ).to_dict()

    @server.tool(name="upsert_page_property", structured_output=True)
    async def upsert_page_property(
        page_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set an exact property on an exact page UUID and verify its value."""
        return (
            await VerifiedMutations(client).upsert_page_property(
                page_uuid, property_ident, value, options
            )
        ).to_dict()

    @server.tool(name="remove_page_property", structured_output=True)
    async def remove_page_property(
        page_uuid: str, property_ident: str
    ) -> dict[str, Any]:
        """Remove an exact property from a page UUID and verify its absence."""
        return (
            await VerifiedMutations(client).remove_page_property(
                page_uuid, property_ident
            )
        ).to_dict()

    @server.tool(name="add_block_tag", structured_output=True)
    async def add_block_tag(block_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Add an exact tag UUID to an exact block UUID and verify it."""
        return (
            await VerifiedMutations(client).add_block_tag(block_uuid, tag_uuid)
        ).to_dict()

    @server.tool(name="remove_block_tag", structured_output=True)
    async def remove_block_tag(block_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Remove an exact tag UUID from an exact block UUID and verify absence."""
        return (
            await VerifiedMutations(client).remove_block_tag(block_uuid, tag_uuid)
        ).to_dict()

    @server.tool(name="add_page_tag", structured_output=True)
    async def add_page_tag(page_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Add an exact tag UUID to an exact page UUID through the native DB API."""
        return (
            await VerifiedMutations(client).add_page_tag(page_uuid, tag_uuid)
        ).to_dict()

    @server.tool(name="remove_page_tag", structured_output=True)
    async def remove_page_tag(page_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Remove an exact tag UUID from an exact page UUID through the native DB API."""
        return (
            await VerifiedMutations(client).remove_page_tag(page_uuid, tag_uuid)
        ).to_dict()

    @server.tool(name="set_block_icon", structured_output=True)
    async def set_block_icon(
        block_uuid: str, icon_type: str, icon_name: str
    ) -> dict[str, Any]:
        """Set and verify a Tabler or emoji icon on an exact block UUID."""
        return (
            await VerifiedMutations(client).set_block_icon(
                block_uuid, icon_type, icon_name
            )
        ).to_dict()

    @server.tool(name="remove_block_icon", structured_output=True)
    async def remove_block_icon(block_uuid: str) -> dict[str, Any]:
        """Remove an icon from an exact block UUID and verify its absence."""
        return (await VerifiedMutations(client).remove_block_icon(block_uuid)).to_dict()

    return server


def _failure_stage(error: Exception) -> str:
    if isinstance(error, (ValueError, TypeError, LookupError)):
        return "validation"
    if isinstance(error, LogseqAPIError):
        return "logseq_error"
    if isinstance(
        error, (httpx.TransportError, LogseqProtocolError, WriteCircuitOpenError)
    ):
        return "transport"
    return "readback_mismatch"


def _failure_suggestion(tool_name: str, error: Exception) -> str:
    contracts = {
        "insert_block": (
            "Use an exact target block UUID, a non-empty title, and placement "
            "child or after."
        ),
        "move_block": (
            "Use distinct exact block and target UUIDs, with placement child or after."
        ),
        "delete_block": (
            "Use block_uuid as the exact UUID of a block, not a page UUID."
        ),
        "get_block": "Use block_uuid as the exact UUID of a block.",
        "get_block_tree": (
            "Use block_uuid as an exact block UUID, max_depth from 0 to 100, "
            "and max_nodes from 1 to 1000."
        ),
        "upsert_block_property": (
            "Use an exact block UUID, a full namespaced property ident, "
            "the schema-compatible value, and an optional options object."
        ),
        "upsert_page_property": (
            "Use an exact page UUID, a full namespaced property ident, "
            "the schema-compatible value, and an optional options object."
        ),
        "add_block_tag": "Use exact block and tag UUIDs.",
        "remove_block_tag": "Use exact block and tag UUIDs.",
        "add_page_tag": "Use exact page and tag UUIDs.",
        "remove_page_tag": "Use exact page and tag UUIDs.",
        "set_tag_parent": (
            "Use exact child and parent tag UUIDs and acknowledge replacement "
            "when the child already has a different parent."
        ),
        "remove_tag_extends": "Use exact child-tag and parent-tag UUIDs.",
        "set_block_icon": (
            "Use an exact block UUID, icon_type tabler-icon or emoji, and a "
            "valid icon name."
        ),
    }
    if tool_name in contracts:
        return contracts[tool_name]
    if isinstance(error, WriteCircuitOpenError):
        return "Read the target state, restart Logseq, and reconnect the MCP."
    return "Correct the named MCP arguments according to this tool's input schema."


def main() -> None:
    settings = Settings.from_env()
    client = LogseqDBClient(
        settings.api_url,
        settings.api_token,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        verify_ssl=settings.verify_ssl,
        readback_attempts=settings.readback_attempts,
        readback_delay=settings.readback_delay,
        read_attempts=settings.read_attempts,
        write_policy=WriteAccessPolicy(
            title_prefixes=settings.write_title_prefixes,
            property_prefixes=settings.write_property_prefixes,
            entity_uuids=settings.write_entity_uuids,
        ),
        max_response_bytes=settings.max_response_bytes,
    )
    create_server(client).run(transport="stdio")


if __name__ == "__main__":
    main()
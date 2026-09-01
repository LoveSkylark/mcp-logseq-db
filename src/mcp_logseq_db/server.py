"""Runnable MCP server exposing only live-verified Logseq DB reads."""

import asyncio
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .access import WriteAccessPolicy
from .capabilities import CapabilityDiscovery
from .client import LogseqDBClient
from .content import VerifiedContent
from .mutations import VerifiedMutations
from .settings import Settings


def create_server(client: LogseqDBClient) -> MCPServer:
    server = MCPServer(
        "mcp-logseq-db",
        description="Narrow DB-native MCP server for Logseq 2.x",
        instructions=(
            "Use exact DB identifiers. Prefer promoted DB reads and writes. "
            "Treat verified_state as evidence, never a raw null response. "
            "Experimental tools may time out and are successful only when "
            "their result reports verified=true."
        ),
    )

    async def visible(awaitable):
        try:
            return await awaitable
        except Exception as error:
            raise ToolError(str(error)) from error

    @server.tool(name="db_capabilities", structured_output=True)
    async def db_capabilities() -> dict[str, Any]:
        """Probe and report DB methods supported by the connected instance."""
        capabilities = await visible(CapabilityDiscovery(client).discover())
        return capabilities.to_dict()

    @server.tool(name="db_check_current_is_db_graph")
    async def db_check_current_is_db_graph() -> bool:
        """Return whether the current Logseq graph is a DB graph."""
        try:
            async with asyncio.timeout(20):
                return bool(
                    await visible(client.call("logseq.DB.checkCurrentIsDbGraph", []))
                )
        except TimeoutError as error:
            raise RuntimeError(
                "Logseq DB health check exceeded 20 seconds; restart Logseq "
                "itself if its DB worker is wedged"
            ) from error

    @server.tool(name="db_get_app_info")
    async def db_get_app_info() -> Any:
        """Return connected Logseq version and DB support metadata."""
        return await visible(client.call("logseq.DB.getAppInfo", []))

    @server.tool(name="db_get_current_graph")
    async def db_get_current_graph() -> Any:
        """Return the current graph identity and path metadata."""
        return await visible(client.call("logseq.DB.getCurrentGraph", []))

    @server.tool(name="db_list_pages")
    async def db_list_pages(expand: bool = False) -> Any:
        """List DB pages with UUIDs and optional expanded metadata."""
        return await visible(client.call("logseq.DB.listPages", [{"expand": expand}]))

    @server.tool(name="db_get_page_data")
    async def db_get_page_data(page_name_or_uuid: str) -> Any:
        """Read a page and only blocks directly parented by it. Nested descendants require db_get_block or a Datascript parent/page query."""
        return await visible(client.call("logseq.DB.getPageData", [page_name_or_uuid]))

    @server.tool(name="db_search")
    async def db_search(query: str) -> Any:
        """Search the DB graph through the verified DB namespace alias."""
        return await visible(client.call("logseq.DB.search", [query]))

    @server.tool(name="db_list_properties")
    async def db_list_properties(expand: bool = False) -> Any:
        """List DB properties with optional expanded metadata."""
        return await visible(client.call("logseq.DB.listProperties", [{"expand": expand}]))

    @server.tool(name="db_list_tags")
    async def db_list_tags(expand: bool = False) -> Any:
        """List DB tags with optional expanded metadata."""
        return await visible(client.call("logseq.DB.listTags", [{"expand": expand}]))

    @server.tool(name="db_upsert_nodes", structured_output=True)
    async def db_upsert_nodes(
        operations: list[dict[str, Any]], dry_run: bool = False
    ) -> dict[str, Any]:
        """Create pages/top-level blocks or edit block titles. Always validates first; nested block targets are rejected because they corrupt ownership."""
        return (
            await VerifiedContent(client).upsert_nodes(operations, dry_run=dry_run)
        ).to_dict()

    @server.tool(name="db_get_block", structured_output=True)
    async def db_get_block(block_uuid: str) -> dict[str, Any]:
        """Read one exact block UUID through Datascript. Missing UUIDs return found=false rather than a tool error."""
        return await VerifiedContent(client).find_block(block_uuid)

    @server.tool(name="db_create_page", structured_output=True)
    async def db_create_page(title: str, dry_run: bool = False) -> dict[str, Any]:
        """Create one page through DB.upsertNodes with validation and read-back."""
        return (
            await VerifiedContent(client).create_page(title, dry_run=dry_run)
        ).to_dict()

    @server.tool(name="db_create_top_level_block", structured_output=True)
    async def db_create_top_level_block(
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

    if getattr(client, "experimental_writes_enabled", False):
        @server.tool(name="db_insert_block_experimental", structured_output=True)
        async def db_insert_block_experimental(
            target_uuid: str,
            title: str,
            placement: Literal["child", "before", "after"] = "child",
        ) -> dict[str, Any]:
            """EXPERIMENTAL: insert a block as child, sibling before, or sibling after. The alias may time out; success is determined only by exact UUID read-back."""
            return (
                await VerifiedContent(client).insert_block(
                    target_uuid, title, placement=placement
                )
            ).to_dict()

        @server.tool(name="db_move_block_experimental", structured_output=True)
        async def db_move_block_experimental(
            block_uuid: str,
            target_uuid: str,
            placement: Literal["child", "before"] = "child",
        ) -> dict[str, Any]:
            """EXPERIMENTAL: move a block as a child or before a target. The alias may time out; parent, page, and order are read back."""
            return (
                await VerifiedContent(client).move_block(
                    block_uuid, target_uuid, placement=placement
                )
            ).to_dict()

        @server.tool(name="db_delete_block_experimental", structured_output=True)
        async def db_delete_block_experimental(block_uuid: str) -> dict[str, Any]:
            """EXPERIMENTAL AND DESTRUCTIVE: delete one exact block subtree. On verified absence, verified_entities is empty and previous_entities holds every pre-delete subtree entity."""
            return (await VerifiedContent(client).delete_block(block_uuid)).to_dict()

    @server.tool(name="db_upsert_block", structured_output=True)
    async def db_upsert_block(
        block_uuid: str, title: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Edit one existing block title through DB.upsertNodes. This does not create, move, nest, or delete blocks."""
        return (
            await VerifiedContent(client).upsert_block(
                block_uuid, title, dry_run=dry_run
            )
        ).to_dict()

    @server.tool(name="db_rename_page", structured_output=True)
    async def db_rename_page(page_uuid: str, new_title: str) -> dict[str, Any]:
        """Rename an exact page UUID and verify its new title."""
        return (await VerifiedContent(client).rename_page(page_uuid, new_title)).to_dict()

    @server.tool(name="db_delete_page", structured_output=True)
    async def db_delete_page(page_uuid: str) -> dict[str, Any]:
        """Compatibility alias for db_recycle_page."""
        return (await VerifiedContent(client).delete_page(page_uuid)).to_dict()

    @server.tool(name="db_recycle_page", structured_output=True)
    async def db_recycle_page(page_uuid: str) -> dict[str, Any]:
        """Recycle an exact page UUID and verify its deleted-at marker."""
        return (await VerifiedContent(client).delete_page(page_uuid)).to_dict()

    @server.tool(name="db_q")
    async def db_q(query: str) -> Any:
        """Run a query through logseq.DB.q."""
        return await visible(client.call("logseq.DB.q", [query]))

    @server.tool(name="db_custom_query")
    async def db_custom_query(query: str) -> Any:
        """Run a custom query through logseq.DB.customQuery."""
        return await visible(client.call("logseq.DB.customQuery", [query]))

    @server.tool(name="db_datascript_query")
    async def db_datascript_query(query: str) -> Any:
        """Run a read-only Datascript query through logseq.DB.datascriptQuery."""
        return await visible(client.call("logseq.DB.datascriptQuery", [query]))

    @server.tool(name="db_get_all_properties")
    async def db_get_all_properties() -> Any:
        """Return all DB property definitions."""
        return await visible(client.call("logseq.DB.getAllProperties", []))

    @server.tool(name="db_get_property")
    async def db_get_property(property_ident: str) -> Any:
        """Get a property by its exact namespaced ident."""
        if not property_ident.startswith(":") or "/" not in property_ident:
            raise ValueError("property_ident must be an exact namespaced ident")
        return await visible(client.call("logseq.DB.getProperty", [property_ident]))

    @server.tool(name="db_get_all_tags")
    async def db_get_all_tags() -> Any:
        """Return all DB tags/classes."""
        return await visible(client.call("logseq.DB.getAllTags", []))

    @server.tool(name="db_get_tag")
    async def db_get_tag(identifier: str) -> Any:
        """Get a tag by exact ident, UUID, or title."""
        return await visible(client.call("logseq.DB.getTag", [identifier]))

    @server.tool(name="db_get_tags_by_name")
    async def db_get_tags_by_name(title: str) -> Any:
        """Get tags matching an exact title."""
        return await visible(client.call("logseq.DB.getTagsByName", [title]))

    @server.tool(name="db_get_tag_objects")
    async def db_get_tag_objects(identifier: str) -> Any:
        """Return a mixed list of pages and blocks associated with a tag ident, UUID, or title."""
        return await visible(client.call("logseq.DB.getTagObjects", [identifier]))

    @server.tool(name="db_upsert_property", structured_output=True)
    async def db_upsert_property(
        title: str,
        schema: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a property and verify it by its returned exact ident."""
        result = await VerifiedMutations(client).upsert_property(
            title, schema, options
        )
        return result.to_dict()

    @server.tool(name="db_remove_property", structured_output=True)
    async def db_remove_property(property_ident: str) -> dict[str, Any]:
        """Remove an exact property ident and verify that it is absent."""
        result = await VerifiedMutations(client).remove_property(property_ident)
        return result.to_dict()

    @server.tool(name="db_create_tag", structured_output=True)
    async def db_create_tag(
        title: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a tag and verify it through its returned exact identity."""
        return (await VerifiedMutations(client).create_tag(title, options)).to_dict()

    @server.tool(name="db_rename_tag", structured_output=True)
    async def db_rename_tag(tag_uuid: str, new_title: str) -> dict[str, Any]:
        """Rename one exact tag UUID and verify its new title."""
        return (await VerifiedMutations(client).rename_tag(tag_uuid, new_title)).to_dict()

    @server.tool(name="db_delete_tag", structured_output=True)
    async def db_delete_tag(tag_uuid: str) -> dict[str, Any]:
        """Permanently delete one exact tag UUID. verified_state is null after exact absence; previous_state contains the deleted tag snapshot."""
        return (await VerifiedMutations(client).delete_tag(tag_uuid)).to_dict()

    @server.tool(name="db_add_tag_property", structured_output=True)
    async def db_add_tag_property(tag_uuid: str, property_ident: str) -> dict[str, Any]:
        """Add an exact property to an exact tag UUID and verify the relation."""
        return (
            await VerifiedMutations(client).add_tag_property(tag_uuid, property_ident)
        ).to_dict()

    @server.tool(name="db_remove_tag_property", structured_output=True)
    async def db_remove_tag_property(tag_uuid: str, property_ident: str) -> dict[str, Any]:
        """Remove an exact property from an exact tag UUID and verify removal."""
        return (
            await VerifiedMutations(client).remove_tag_property(tag_uuid, property_ident)
        ).to_dict()

    @server.tool(name="db_add_tag_extends", structured_output=True)
    async def db_add_tag_extends(tag_uuid: str, parent_tag_uuid: str) -> dict[str, Any]:
        """Add and verify inheritance between two exact tag UUIDs."""
        return (
            await VerifiedMutations(client).add_tag_extends(tag_uuid, parent_tag_uuid)
        ).to_dict()

    @server.tool(name="db_remove_tag_extends", structured_output=True)
    async def db_remove_tag_extends(tag_uuid: str, parent_tag_uuid: str) -> dict[str, Any]:
        """Remove and verify inheritance between two exact tag UUIDs."""
        return (
            await VerifiedMutations(client).remove_tag_extends(tag_uuid, parent_tag_uuid)
        ).to_dict()

    @server.tool(name="db_upsert_block_property", structured_output=True)
    async def db_upsert_block_property(
        block_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set an exact property on an exact block UUID and verify its presence."""
        return (
            await VerifiedMutations(client).upsert_block_property(
                block_uuid, property_ident, value, options
            )
        ).to_dict()

    @server.tool(name="db_remove_block_property", structured_output=True)
    async def db_remove_block_property(
        block_uuid: str, property_ident: str
    ) -> dict[str, Any]:
        """Remove an exact property from a block UUID and verify its absence."""
        return (
            await VerifiedMutations(client).remove_block_property(
                block_uuid, property_ident
            )
        ).to_dict()

    @server.tool(name="db_add_block_tag", structured_output=True)
    async def db_add_block_tag(block_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Add an exact tag UUID to an exact page or block UUID and verify the relation."""
        return (
            await VerifiedMutations(client).add_block_tag(block_uuid, tag_uuid)
        ).to_dict()

    @server.tool(name="db_remove_block_tag", structured_output=True)
    async def db_remove_block_tag(block_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Remove an exact tag UUID from a page or block UUID and verify its absence."""
        return (
            await VerifiedMutations(client).remove_block_tag(block_uuid, tag_uuid)
        ).to_dict()

    @server.tool(name="db_set_block_icon", structured_output=True)
    async def db_set_block_icon(
        block_uuid: str, icon_type: str, icon_name: str
    ) -> dict[str, Any]:
        """Set and verify an icon. For emoji, use its case-sensitive emoji-mart display name, such as 'Test Tube' or 'Books', not a glyph or ID."""
        return (
            await VerifiedMutations(client).set_block_icon(
                block_uuid, icon_type, icon_name
            )
        ).to_dict()

    @server.tool(name="db_remove_block_icon", structured_output=True)
    async def db_remove_block_icon(block_uuid: str) -> dict[str, Any]:
        """Remove an icon from an exact block UUID and verify its absence."""
        return (await VerifiedMutations(client).remove_block_icon(block_uuid)).to_dict()

    return server


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
        experimental_writes_enabled=settings.experimental_writes_enabled,
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
"""Runnable MCP server exposing the verified Logseq DB tool surface.

WHAT CHANGED, AND WHY
---------------------
Page/block tool pairs are collapsed. `add_page_tag`/`add_block_tag` and
`upsert_page_property`/`upsert_block_property` did the same thing through the
same API method -- a page IS a block in the DB -- so exposing both asked a
caller to choose between identical operations. There is one `addTag` and one
`addProperty`, each taking a target that may be either.

Removed, having no verified route: `insert_block` and `move_block` (both
routed through methods reached only by a graph-worker CLI fallback that
existed because a hardcoded capability list was wrong), `rename_tag`,
`add_tag_property`, `remove_tag_property`, `set_tag_parent`,
`remove_tag_extends`, `set_block_icon`, `remove_block_icon`.

`delete_page`/`recycle_page` are gone too: one was documented as an alias of
the other, which invites a caller to reason about a distinction that may not
exist. Whether recycling is reversible is an open question; until it is
settled, exposing one honest tool beats two ambiguous ones.

Nesting no longer needs its own tool. `createBlock` takes a parent that may be
a page or a block, because the API's `page-id` field is a parent pointer.
"""

from __future__ import annotations

import asyncio
import json
from functools import wraps
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
from .identifiers import require_uuid
from .mutations import MutationVerificationError, VerifiedMutations
from .settings import Settings

# Class idents, resolved to :db/id at call time. Integer ids are not stable
# across a rebuilt graph, so nothing here hardcodes 2, 3 or 4.
PAGE_CLASS = ":logseq.class/Page"
TAG_CLASS = ":logseq.class/Tag"
PROPERTY_CLASS = ":logseq.class/Property"


def create_server(
    client: LogseqDBClient, *, probe_writes: bool = True
) -> MCPServer:
    server = MCPServer(
        "mcp-logseq-db",
        description="DB-native MCP server for Logseq 2.x",
        instructions=(
            "Use exact UUIDs for pages and blocks, and exact :db/idents for "
            "properties. This API returns success for calls that do nothing, "
            "so treat verified_state as the only evidence a write happened -- "
            "never the response. A result with verified=false means the write "
            "did not take effect, even though no error was raised."
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
                        "suggestion": _failure_suggestion(
                            method.__name__, error),
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

    content = lambda: VerifiedContent(client)          # noqa: E731
    mutations = lambda: VerifiedMutations(client)      # noqa: E731

    async def query(q: str, *params: Any) -> Any:
        return await client.call("logseq.DB.datascriptQuery", [q, *params])

    async def class_id(ident: str) -> int:
        value = await query(
            f"[:find ?class . :where [?class :db/ident {ident}]]")
        if not isinstance(value, int):
            raise RuntimeError(f"Could not resolve the class {ident}")
        return value

    # ------------------------------------------------------------ meta

    @server.tool(name="capabilities", structured_output=True)
    async def capabilities(include_diagnostics: bool = False) -> dict[str, Any]:
        """Report which tools are available on the connected graph, with the constraints that apply to each. Set include_diagnostics for the underlying probe results."""
        result = await CapabilityDiscovery(client).discover(
            probe_writes=probe_writes)
        return result.to_dict(include_diagnostics=include_diagnostics)

    # ------------------------------------------------------------ pages

    @server.tool(name="getPageUUID", structured_output=True)
    async def get_page_uuid(title: str) -> dict[str, Any]:
        """Resolve a page title to exactly one UUID. Returns found=false with candidates when the title is ambiguous, rather than guessing a write target."""
        return await content().get_page_uuid(title)

    @server.tool(name="getPage", structured_output=True)
    async def get_page(
        page_uuid: str,
        detail: Literal[
            "page", "blocks", "tags", "properties", "declared", "all"
        ] = "page",
    ) -> dict[str, Any]:
        """Read one page. detail=page is the page entity alone; blocks lists every block at any depth; tags covers the page and its blocks; properties returns values that are set; declared returns property slots inherited from the page's classes that have no value yet; all combines them."""
        return await content().get_page(page_uuid, detail)

    @server.tool(name="createPage", structured_output=True)
    async def create_page(title: str, dry_run: bool = False) -> dict[str, Any]:
        """Create one page. A title that already exists is rejected rather than duplicated, because the read-back could not then tell the new page from the old one."""
        return (await content().create_page(title, dry_run=dry_run)).to_dict()

    @server.tool(name="renamePage", structured_output=True)
    async def rename_page(page_uuid: str, new_title: str) -> dict[str, Any]:
        """Rename a page and verify by UUID. Reading back by title would not distinguish a rename from Logseq creating a second page and leaving the original alone."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return (await content().rename_page(page_uuid, new_title)).to_dict()

    @server.tool(name="deletePage", structured_output=True)
    async def delete_page(
        page_uuid: str, acknowledge_reference_rewrite: bool = False
    ) -> dict[str, Any]:
        """Delete a page. On this build it recycles rather than destroys: the page keeps its UUID, tags and blocks and stops appearing in listPages. Inbound references are NOT rewritten, so acknowledge_reference_rewrite is required when any entity links to it."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return (await content().delete_page(
            page_uuid,
            acknowledge_reference_rewrite=acknowledge_reference_rewrite
        )).to_dict()

    @server.tool(name="clearPage", structured_output=True)
    async def clear_page(page_uuid: str) -> dict[str, Any]:
        """Delete every block on a page, keeping the page itself along with its tags and property values. One call per top-level block, since the API has no batch delete."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return (await content().clear_page(page_uuid)).to_dict()

    # ------------------------------------------------------------ blocks

    @server.tool(name="getBlockUUID", structured_output=True)
    async def get_block_uuid(page_uuid: str) -> list[dict[str, Any]]:
        """List every block on a page, at any depth, ordered by position. Returns a list, not a single UUID."""
        return await content().get_block_uuid(page_uuid)

    @server.tool(name="getBlock", structured_output=True)
    async def get_block(block_uuid: str) -> dict[str, Any]:
        """Read one exact block. A missing UUID returns found=false rather than raising."""
        return await content().find_block(block_uuid)

    @server.tool(name="findOrphans", structured_output=True)
    async def find_orphans(page_uuid: str) -> dict[str, Any]:
        """List blocks whose :block/parent and :block/page disagree. Such blocks are real children but are invisible to every page-scoped query, which is the signature of a failed nested write. Run this to audit a page after a nested create fails."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return await content().find_orphans(page_uuid)

    @server.tool(name="getBlockTree", structured_output=True)
    async def get_block_tree(
        block_uuid: str, max_depth: int = 20, max_nodes: int = 1000
    ) -> dict[str, Any]:
        """Read a block and its descendants as a nested tree. Returns truncated=true when a limit stops traversal."""
        return await content().find_block_tree(
            block_uuid, max_depth=max_depth, max_nodes=max_nodes)

    @server.tool(name="createBlock", structured_output=True)
    async def create_block(
        parent_uuid: str, title: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Create one block. parent_uuid may be a page UUID (top-level block) or a block UUID (nested child). Only the title can be set at creation; tags and position are follow-up calls, and the new UUID is assigned by Logseq rather than returned."""
        return (await content().create_block(
            parent_uuid, title, dry_run=dry_run)).to_dict()

    @server.tool(name="createManyBlocks", structured_output=True)
    async def create_many_blocks(
        blocks: list[dict[str, str]], dry_run: bool = False
    ) -> dict[str, Any]:
        """Create several blocks in one batched call. Each item takes parent_uuid and title. Titles must be unique within the batch, since verification identifies new blocks by title."""
        return (await content().create_many_blocks(
            blocks, dry_run=dry_run)).to_dict()

    @server.tool(name="createPageofBlocks", structured_output=True)
    async def create_page_of_blocks(
        page_uuid: str, outline: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Build an indented outline on a page. Each level is created, read back to learn the UUIDs Logseq assigned, then used as the parent for the next -- creation does not return UUIDs and parents cannot be named by title."""
        return await content().create_page_of_blocks(
            page_uuid, outline, dry_run=dry_run)

    @server.tool(name="updateBlock", structured_output=True)
    async def update_block(
        block_uuid: str, title: str, dry_run: bool = False
    ) -> dict[str, Any]:
        """Edit one block's title. Does not create, move, nest, or delete."""
        return (await content().update_block(
            block_uuid, title, dry_run=dry_run)).to_dict()

    @server.tool(name="moveBlock", structured_output=True)
    async def move_block(
        block_uuid: str,
        target_uuid: str,
        placement: Literal["child", "before", "after"] = "child",
    ) -> dict[str, Any]:
        """Move a block and its subtree relative to a target. placement=child puts it under the target (a page target moves it to the page's top level); before and after place it as a sibling. The API returns nothing on a move, so the result is verified by reading the block back and checking its parent, its owning page, and that descendants followed."""
        block_uuid = require_uuid(
            block_uuid, role="block_uuid", hint="getBlockUUID")
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getBlockUUID or getPageUUID")
        return (await content().move_block(
            block_uuid, target_uuid, placement=placement)).to_dict()

    @server.tool(name="removeBlock", structured_output=True)
    async def remove_block(block_uuid: str) -> dict[str, Any]:
        """Delete a block and its entire subtree, then verify every UUID in that subtree is absent."""
        return (await content().remove_block(block_uuid)).to_dict()

    # ------------------------------------------------------------- tags

    @server.tool(name="getTagUUID", structured_output=True)
    async def get_tag_uuid(title: str) -> dict[str, Any]:
        """Resolve a tag title to exactly one UUID. Returns found=false with candidates when several tags share the title."""
        return await mutations().get_tag_uuid(title)

    @server.tool(name="getTag", structured_output=True)
    async def get_tag(tag_uuid: str) -> dict[str, Any]:
        """Read one exact tag entity. Takes a UUID, not a title or ident -- use getTagUUID to resolve a title."""
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return await mutations().get_tag(tag_uuid)

    @server.tool(name="getTagUsers", structured_output=True)
    async def get_tag_users(tag_uuid: str) -> list[dict[str, Any]]:
        """List every page and block carrying the tag. Holders with block/name are pages. This is the work list for removing a tag everywhere, and the check to run before deleting it."""
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return await mutations().get_tag_users(tag_uuid)

    @server.tool(name="creatTag", structured_output=True)
    async def creat_tag(
        title: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Create a tag. Its ident carries a random suffix assigned by Logseq, so it cannot be predicted from the title and is read back."""
        return (await mutations().create_tag(title, options)).to_dict()

    @server.tool(name="deleteTag", structured_output=True)
    async def delete_tag(
        tag_uuid: str,
        acknowledge_child_reparent: bool = False,
        acknowledge_detach: bool = False,
    ) -> dict[str, Any]:
        """Delete one tag entity. Everything carrying the tag loses it, so acknowledge_detach is required when any page or block holds it, and acknowledge_child_reparent when child tags would be reparented. Run getTagUsers first to see what is affected."""
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return (await mutations().delete_tag(
            tag_uuid,
            acknowledge_child_reparent=acknowledge_child_reparent,
            acknowledge_detach=acknowledge_detach)).to_dict()

    @server.tool(name="addTag", structured_output=True)
    async def add_tag(target_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Attach an existing tag to a page or a block. target_uuid may be either. Both arguments are UUIDs; the target comes first."""
        # Validate both before either lookup, so a call with the arguments
        # swapped or a title in one slot names the offending argument rather
        # than failing halfway through.
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getPageUUID or getBlockUUID")
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return (await mutations().add_tag(target_uuid, tag_uuid)).to_dict()

    @server.tool(name="removeTag", structured_output=True)
    async def remove_tag(target_uuid: str, tag_uuid: str) -> dict[str, Any]:
        """Detach one tag from a page or a block. Other tags on the target are untouched and the tag entity survives. Both arguments are UUIDs; the target comes first."""
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getPageUUID or getBlockUUID")
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return (await mutations().remove_tag(target_uuid, tag_uuid)).to_dict()

    # -------------------------------------------------------- properties

    @server.tool(name="getPropertyIndent", structured_output=True)
    async def get_property_indent(title: str) -> dict[str, Any]:
        """Resolve a property title to exactly one :db/ident. Returns the ident, which is what every other property tool takes -- a UUID will not work."""
        return await mutations().get_property_ident(title)

    @server.tool(name="getProperyUsers", structured_output=True)
    async def get_propery_users(property_ident: str) -> list[dict[str, Any]]:
        """List every page and block holding a value for this property, with the value in both raw and resolved form. Run this before deleteProperty."""
        return await mutations().get_property_users(property_ident)

    @server.tool(name="createProperty", structured_output=True)
    async def create_property(
        title: str,
        schema: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a property definition. Pass a plain title, never a namespaced ident. schema takes a type: default (text), number, string, datetime, checkbox, url, node, page, class, property, or map. The namespace is assigned from caller identity and cannot be chosen."""
        return (await mutations().create_property(
            title, schema, options)).to_dict()

    @server.tool(name="deleteProperty", structured_output=True)
    async def delete_property(
        property_ident: str, acknowledge_value_loss: bool = False
    ) -> dict[str, Any]:
        """Delete a property definition graph-wide, taking every value with it. Not reversible -- recreating mints a new entity and the old values do not return. acknowledge_value_loss is required when anything holds a value; run getProperyUsers first to see what is affected."""
        return (await mutations().delete_property(
            property_ident,
            acknowledge_value_loss=acknowledge_value_loss)).to_dict()

    @server.tool(name="addProperty", structured_output=True)
    async def add_property(
        target_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set a property value on a page or a block. target_uuid may be either. Reference-typed properties take an entity id, not a literal; closed enums such as Status take one of the entities from listClosedValues. Only properties in this plugin's own namespace can be written."""
        return (await mutations().set_property(
            target_uuid, property_ident, value, options)).to_dict()

    @server.tool(name="removeProperty", structured_output=True)
    async def remove_property(
        target_uuid: str, property_ident: str
    ) -> dict[str, Any]:
        """Clear a property value from a page or a block. The property definition survives and other targets keep their values -- use deleteProperty to remove the definition itself."""
        return (await mutations().clear_property(
            target_uuid, property_ident)).to_dict()

    # ------------------------------------------------------------ lists
    # Each takes no arguments and returns the whole of one kind.

    @server.tool(name="listPages")
    async def list_pages() -> Any:
        """List all live pages. Recycled pages are excluded -- they keep the Page class and would otherwise appear live."""
        return await query(
            "[:find [(pull ?page [:db/id :block/uuid :block/name "
            ":block/title]) ...] :in $ ?class :where "
            "[?page :block/name] [?page :block/tags ?class] "
            "[(missing? $ ?page :logseq.property/deleted-at)]]",
            await class_id(PAGE_CLASS))

    @server.tool(name="listJournals")
    async def list_journals() -> Any:
        """List all journal pages, with their journal day as an integer date."""
        return await query(
            "[:find [(pull ?page [:db/id :block/uuid :block/name "
            ":block/title :block/journal-day]) ...] "
            ":where [?page :block/journal-day _]]")

    @server.tool(name="listTags")
    async def list_tags() -> Any:
        """List all tags and classes, including Logseq built-ins."""
        return await client.call("logseq.DB.getAllTags", [])

    @server.tool(name="listProperties")
    async def list_properties() -> Any:
        """List all property definitions with their idents and types, including built-ins."""
        return await client.call("logseq.DB.getAllProperties", [])

    @server.tool(name="listClosedValues")
    async def list_closed_values() -> Any:
        """List every enum property with its permitted values. Required before setting Status, Priority, or any closed property -- the value must be one of these entities."""
        # The relationship is stored on the VALUE, pointing back at its
        # property, under a bare unnamespaced ident. Querying the property
        # side for a `closed-values` attribute finds nothing, because no such
        # attribute exists on this build.
        return await query(
            "[:find (pull ?prop [:db/id :db/ident :block/title]) "
            "(pull ?value [:db/id :db/ident :block/title "
            ":logseq.property/value :block/order]) "
            ":where [?value :closed-value-property ?prop]]")

    @server.tool(name="listOrphanTags")
    async def list_orphan_tags() -> Any:
        """List tags that nothing carries. Run before deleting tags in bulk."""
        return await query(
            "[:find [(pull ?tag [:db/id :db/ident :block/uuid "
            ":block/title]) ...] :in $ ?class :where "
            "[?tag :block/tags ?class] "
            "[(missing? $ ?tag :block/_tags)]]",
            await class_id(TAG_CLASS))

    @server.tool(name="listOrphanProperties")
    async def list_orphan_properties() -> Any:
        """List properties with no values anywhere. Each property is its own DB attribute, so this checks them one at a time and is slower than the other lists."""
        properties = await client.call("logseq.DB.getAllProperties", [])
        orphans = []
        for entry in properties or []:
            ident = entry.get("ident") if isinstance(entry, dict) else None
            if not isinstance(ident, str) or not ident.startswith(":"):
                continue
            holders = await query(
                f"[:find [?holder ...] :where [?holder {ident} _]]")
            if not holders:
                orphans.append({
                    "ident": ident,
                    "title": entry.get("title"),
                    "type": entry.get(":logseq.property/type"),
                })
        return orphans

    @server.tool(name="listAssets")
    async def list_assets() -> Any:
        """List asset-related attributes in use. UNVERIFIED: asset modelling was never established, so this is a discovery probe rather than a reliable list."""
        return await query(
            '[:find [?attr ...] :where [_ ?attr _] [(str ?attr) ?s] '
            '[(clojure.string/includes? ?s "asset")]]')

    @server.tool(name="listStatus")
    async def list_status() -> Any:
        """List everything with a Status value, paired with the status it holds."""
        return await query(
            "[:find (pull ?entity [:db/id :block/uuid :block/title "
            ":block/name {:block/page [:block/title]}]) "
            "(pull ?value [:db/ident :block/title]) "
            ":where [?entity :logseq.property/status ?value]]")

    @server.tool(name="listRecycled")
    async def list_recycled() -> Any:
        """List recycled pages. These keep their UUID, tags and references; inbound links to them are not rewritten."""
        return await query(
            "[:find [(pull ?page [:db/id :block/uuid :block/name "
            ":block/title :logseq.property/deleted-at]) ...] "
            ":where [?page :logseq.property/deleted-at _]]")

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
        "get_page_uuid": (
            "Pass the page's display title. If several pages share it, use "
            "getPage with a UUID instead."
        ),
        "get_page": (
            "Pass an exact page UUID and one of: page, blocks, tags, "
            "properties, declared, all."
        ),
        "create_page": (
            "Use a title no existing page, tag or block holds -- they share "
            "one title space. If the failure is an EDN validation error from "
            "upsertNodes instead, the title is not the problem: the graph is "
            "refusing the transaction, and no retry or rename will help."
        ),
        "rename_page": (
            "Pass an exact page UUID and a title nothing else already uses."
        ),
        "delete_page": (
            "Pass an exact page UUID. If entities reference the page, pass "
            "acknowledge_reference_rewrite=true -- those references are not "
            "rewritten."
        ),
        "clear_page": "Pass an exact page UUID, not a block UUID.",
        "get_block_uuid": "Pass an exact page UUID, not a block UUID.",
        "get_block": "Pass an exact block UUID.",
        "get_block_tree": (
            "Pass an exact block UUID, max_depth 0-100, max_nodes 1-1000."
        ),
        "create_block": (
            "Pass an exact parent UUID -- a page UUID for a top-level block or "
            "a block UUID to nest -- and a non-empty title. A page title will "
            "not resolve."
        ),
        "create_many_blocks": (
            "Pass a list of objects with parent_uuid and title. Titles must be "
            "unique within the batch."
        ),
        "create_page_of_blocks": (
            "Pass an exact page UUID and an outline whose indentation is "
            "consistent and never jumps more than one level."
        ),
        "update_block": "Pass an exact block UUID and a non-empty title.",
        "move_block": (
            "Pass the block to move, then the target, then placement. The "
            "target may be a block, or a page when placement is child. A "
            "block cannot be moved inside its own subtree."
        ),
        "remove_block": "Pass an exact block UUID, not a page UUID.",
        "get_tag_uuid": "Pass the tag's display title.",
        "get_tag": "Pass an exact tag UUID.",
        "get_tag_users": "Pass an exact tag UUID.",
        "creat_tag": "Pass a non-empty tag title.",
        "delete_tag": (
            "Pass an exact tag UUID. This route is unverified; if the result "
            "reports verified=false the tag was not deleted."
        ),
        "add_tag": (
            "Pass the target UUID first and the tag UUID second. The target "
            "may be a page or a block."
        ),
        "remove_tag": (
            "Pass the target UUID first and the tag UUID second."
        ),
        "get_property_indent": "Pass the property's display title.",
        "get_propery_users": (
            "Pass a full namespaced ident such as "
            ":plugin.property.my_plugin/Effort, not a title or UUID."
        ),
        "create_property": (
            "Pass a plain title with no '/', and a schema with a valid type."
        ),
        "delete_property": (
            "Pass a full namespaced ident. A UUID returns success and does "
            "nothing. Only this plugin's own properties can be deleted."
        ),
        "add_property": (
            "Pass the target UUID, a full namespaced ident, and a value "
            "matching the property's type. Reference types take an entity id."
        ),
        "remove_property": (
            "Pass the target UUID and a full namespaced ident."
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
            property_prefixes=settings.property_prefixes,
            entity_uuids=settings.write_entity_uuids,
        ),
        writable_property_prefix=settings.writable_property_prefix,
        max_response_bytes=settings.max_response_bytes,
    )
    create_server(client, probe_writes=settings.probe_writes).run(
        transport="stdio")


if __name__ == "__main__":
    main()

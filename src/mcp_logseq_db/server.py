"""Runnable MCP server exposing the verified Logseq DB tool surface.

WHAT CHANGED, AND WHY
---------------------
Page/block tool pairs are collapsed. `add_page_tag`/`add_block_tag` and
`upsert_page_property`/`upsert_block_property` did the same thing through the
same API method -- a page IS a block in the DB -- so exposing both asked a
caller to choose between identical operations. There is one `addTag` and one
`addProperty`, each taking a target that may be either.

Removed, having no verified route: `insert_block` (routed through a method
reached only by a graph-worker CLI fallback that existed because a hardcoded
capability list was wrong), `rename_tag`, `add_tag_property`,
`remove_tag_property`, `set_tag_parent`, `remove_tag_extends`,
`set_block_icon`, `remove_block_icon`.

`move_block` was on that list and is now a tool: `logseq.DB.moveBlock` is
verified on every placement, including across pages.

`delete_page`/`recycle_page` are gone too: one was documented as an alias of
the other, which invites a caller to reason about a distinction that may not
exist. Whether recycling is reversible is an open question; until it is
settled, exposing one honest tool beats two ambiguous ones.

Nesting no longer needs its own tool. `createBlock` takes a parent that may be
a page or a block, because `insertBlock`'s target argument accepts either --
and unlike `upsertNodes`, it sets `:block/parent` and `:block/page`
independently rather than writing one value into both.
"""

from __future__ import annotations

import asyncio
import json
from functools import wraps
from typing import Any, Literal

import httpx
# The full module path rather than the `mcp.server` re-export: the SDK's v2
# migration guide names `mcp.server.mcpserver`, and a convenience re-export is
# a weaker guarantee than the module the guide documents. FastMCP was renamed
# MCPServer and moved here in mcp 2.0.0, which is why pyproject pins
# `mcp>=2,<3` -- an unbounded pin resolves through that rename and fails at
# import.
from mcp.server.mcpserver import MCPServer
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
from .identifiers import IdentifierError, require_ident, require_uuid
from .importer import VerifiedImport
from .mutations import MutationVerificationError, VerifiedMutations
from .settings import Settings

# Class ident, resolved to a :db/id at call time. Integer ids are not stable
# across a rebuilt graph, so nothing here hardcodes 2, 3 or 4. Only the Tag
# class is needed at this layer now -- the page and property listings moved
# into content.py and mutations.py, which resolve their own.
TAG_CLASS = ":logseq.class/Tag"


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
    importer = lambda: VerifiedImport(client)          # noqa: E731

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
        """Resolve a page title to exactly one UUID. Accepts the display title or the lowercased name. RECYCLED PAGES DO NOT RESOLVE, deliberately -- a link must never point at a page the user deleted -- so a title this reports as not found may still be TAKEN for writing. Use isTitleAvailable before a rename or a creation. A title held only by a tag resolves to nothing rather than to the tag. Returns found=false with candidates when two live pages share a title, rather than guessing a write target."""
        return await content().get_page_uuid(title)

    @server.tool(name="isTitleAvailable", structured_output=True)
    async def is_title_available(title: str) -> dict[str, Any]:
        """Can this title be written? Uses the exact check createPage and renamePage make, so its answer cannot disagree with theirs. Returns available, and when taken, held_by with each holder's uuid, kind (page, tag, block or property -- all four share one title space) and whether it is RECYCLED. Recycling does not release a title: the entity survives, so the writers still refuse it while getPageUUID reports the same title as not found. That is deliberate on both sides -- a recycled page must not resolve or a link would point at a deleted page -- and this tool is how you see it. Call it before any rename or page creation, and when a duplicate-title repair needs to know what actually holds a name."""
        return await content().is_title_available(title)

    @server.tool(name="findDuplicateTitles", structured_output=True)
    async def find_duplicate_titles(
        normalize: Literal["exact", "loose", "fuzzy"] = "loose",
        include_recycled: bool = True,
    ) -> dict[str, Any]:
        """Group pages and tags whose titles may name the same thing, with the evidence to classify each group: both content counts, both reference counts, and ALIAS status. The front end to duplicate triage -- it replaces reading every title by eye, and costs six queries whatever the graph size. IT REPORTS AND RANKS; IT NEVER ACTS, and no classification is an instruction. Groups come back as dead_stub (safest), split_identity, near_title, genuine_split, or alias. An alias group is NOT a duplicate: an alias is empty, lightly referenced and titled one character off its neighbour, so it is indistinguishable from an abandoned stub by counts alone -- and deleting either side cannot be repaired, since alias is outside the writable namespace. Those groups are ranked last and excluded from anything actionable. normalize=exact groups identical titles only; loose folds case, whitespace, punctuation and simple plurals; fuzzy adds edit-distance matching for typo pairs and is opt-in because it also matches legitimately distinct short titles like Thread and Threads. Recycled pages are included by default, because a recycled page still holds its title. Confirm a symptom in the Logseq UI before any write."""
        return await content().find_duplicate_titles(
            normalize=normalize, include_recycled=include_recycled)

    @server.tool(name="inspectPage", structured_output=True)
    async def inspect_page(
        page_uuid: str,
        detail: Literal[
            "page", "blocks", "tags", "properties", "declared", "all"
        ] = "page",
    ) -> dict[str, Any]:
        """Read one page at a chosen level of detail. detail=page is the page entity alone; blocks lists every block at any depth; tags covers the page and its blocks; properties returns values that are set; declared returns property slots inherited from the page's classes that have no value yet; all combines them. Named inspectPage rather than getPage because it returns far more than a page entity, and because logseq.DB.getPage is a different and much narrower thing."""
        return await content().get_page(page_uuid, detail)

    @server.tool(name="createPage", structured_output=True)
    async def create_page(
        title: str, dry_run: bool = False, verbose: bool = True
    ) -> dict[str, Any]:
        """Create one page. Routed through logseq.DB.createPage, which is idempotent on title -- unlike upsertNodes, which fails outright on synced graphs. A title already held by any page, tag or block is rejected, since the three share one title space. verbose=false returns identity and position only."""
        return (await content().create_page(
            title, dry_run=dry_run)).to_dict(verbose)

    @server.tool(name="renamePage", structured_output=True)
    async def rename_page(
        page_uuid: str, new_title: str, verbose: bool = True
    ) -> dict[str, Any]:
        """Rename a page and verify by UUID. Reading back by title would not distinguish a rename from Logseq creating a second page and leaving the original alone. verbose=false returns identity and position only."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return (await content().rename_page(
            page_uuid, new_title)).to_dict(verbose)

    @server.tool(name="retitleOverDuplicate", structured_output=True)
    async def retitle_over_duplicate(
        from_uuid: str,
        to_title: str,
        park_suffix: str = "(parked)",
    ) -> dict[str, Any]:
        """Give a page a title that an EMPTY duplicate is holding, by renaming the holder out of the way and then renaming from_uuid onto it. Two renames, no block edits: references are by UUID, so every inbound link, tag and property value on both pages survives. This also works when the holder is a recycled page -- renaming it is what releases the title. YOU choose the direction: from_uuid keeps its identity and gains the title, and both pages' inbound reference counts are reported so you can check you picked the side the graph is actually wired into. Refuses, with the counts, when the holder has content blocks (merging is your decision), when the holder is in an alias relation (an alias is live wiring, not an abandoned typo), or when the title is held by more than one entity or by a block, tag or property. Not atomic -- if the second rename fails, the parked page's UUID and original title come back with instructions to undo."""
        from_uuid = require_uuid(
            from_uuid, role="from_uuid", hint="getPageUUID")
        return await content().retitle_over_duplicate(
            from_uuid, to_title, park_suffix=park_suffix)

    @server.tool(name="deletePage", structured_output=True)
    async def delete_page(
        page_uuid: str,
        acknowledge_reference_rewrite: bool = False,
        acknowledge_alias_loss: bool = False,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Delete a page. On this build it recycles rather than destroys: the page keeps its UUID, tags and blocks and stops appearing in listPages. Inbound references are NOT rewritten, so acknowledge_reference_rewrite is required when any entity links to it. acknowledge_alias_loss is required when the page is in an ALIAS relation in either direction -- that one is UNREPAIRABLE, because alias is a built-in property outside this server's writable namespace, and no block or reference count reveals the relation, so an empty page that is a working alias looks exactly like a dead stub. Check pageStats for is_alias_of and aliases first. Keep verbose=true here -- the envelope lists the referring and alias-related entities, which is the record of what now points at a page nobody can find."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return (await content().delete_page(
            page_uuid,
            acknowledge_reference_rewrite=acknowledge_reference_rewrite,
            acknowledge_alias_loss=acknowledge_alias_loss,
        )).to_dict(verbose)

    @server.tool(name="clearPage", structured_output=True)
    async def clear_page(
        page_uuid: str, verbose: bool = True
    ) -> dict[str, Any]:
        """Delete every block on a page, keeping the page itself along with its tags and property values. One call per top-level block, since the API has no batch delete. LEAVE verbose=true unless you have the content elsewhere: previous_entities is the only remaining record of what was destroyed, and it is what lets you diff an import against the originals. verbose=false reduces it to a count."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return (await content().clear_page(page_uuid)).to_dict(verbose)

    @server.tool(name="importPage", structured_output=True)
    async def import_page(
        target: str,
        markdown: str | list[Any],
        replace: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Build a whole page in one call. target is either a page UUID (write into it) or a title (create it). markdown takes either of two forms. As a STRING it is Logseq markdown: indentation gives structure, every block line must start with '- ', and a line without a bullet continues the block above it. Text before the first bullet that is not a 'key:: value' page property is REFUSED rather than dropped. As a LIST it is one block per element with depth stated explicitly -- either a plain string (depth 0) or {"text": "...", "depth": 1} -- and the text is used VERBATIM, which is the only way to import a block that contains newlines, blank lines, leading whitespace, a markdown table or a fenced code block. Use the list form for real manuscript content: in the string form a line beginning with '- ' inside a multi-line block becomes a child of it. Depth is required rather than inferred there, because indentation stops telling structure from content once values span lines. A JSON-encoded array is accepted as a list, since some clients cannot send a real one. Markdown headings convert to native Logseq headings in both forms. [[links]] and #tags are ESCAPED to {{link:X}} and {{tag:X}} rather than written live, because Logseq mints a page or tag for any reference it parses -- run repairLinks afterwards to convert them once their targets exist. NEW CONTENT LANDS ABOVE EXISTING CONTENT: insertBatchBlock prepends, confirmed 2026-09-20, so importing a document chapter by chapter into one page yields the chapters in REVERSE order. Order within a single call is correct. Import a page in one call where you can, and check :block/order before assuming otherwise. replace=true clears the page first, destroying its block UUIDs. Page properties are only parsed in the string form."""
        return (await importer().import_page(
            target, markdown, replace=replace, dry_run=dry_run)).to_dict()

    @server.tool(name="repairLinks", structured_output=True)
    async def repair_links(
        page_uuid: str | None = None,
        create_missing: bool = False,
        acknowledge_page_creation: bool = False,
        acknowledge_tag_creation: bool = False,
        max_pages_to_create: int = 5,
        max_tags_to_create: int = 5,
        include_tags: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Convert {{link:X}} and {{tag:X}} placeholders left by importPage back to live references. Omit page_uuid to scan every page, which is the usual case since links resolve only once their targets have been imported. ONLY THOSE TWO PREFIXED FORMS ARE RECOGNISED: a bare {{X}} was never written by an import and is left alone, which matters because Logseq reads it as an unknown macro call rather than a link -- run searchBlocks for '{{' to see what a page actually carries. NOTHING IS CREATED WITHOUT EXPLICIT APPROVAL: a name matching no page or tag is skipped, its placeholder left in place, and reported — links under missing, tags under tags_missing. Names matching several candidates are skipped rather than guessed. Creating missing pages needs BOTH create_missing and acknowledge_page_creation; creating missing TAGS needs create_missing and acknowledge_tag_creation, which is a separate flag because tags are a separate entity kind. Both are capped. Run with dry_run first to see exactly which pages and tags would be created. Tags are opt-in via include_tags. Safe to re-run."""
        if page_uuid is not None:
            page_uuid = require_uuid(
                page_uuid, role="page_uuid", hint="getPageUUID")
        return await importer().repair_links(
            page_uuid,
            create_missing=create_missing,
            acknowledge_page_creation=acknowledge_page_creation,
            acknowledge_tag_creation=acknowledge_tag_creation,
            max_pages_to_create=max_pages_to_create,
            max_tags_to_create=max_tags_to_create,
            include_tags=include_tags,
            dry_run=dry_run)

    # ------------------------------------------------------------ blocks

    @server.tool(name="getBlockUUID", structured_output=True)
    async def get_block_uuid(page_uuid: str) -> list[dict[str, Any]]:
        """List every block on a page, at any depth, sorted by :block/order across ALL depths -- which is not document order: a nested child can be returned before its own parent. Use getBlockTree when structure or reading order matters. Returns a list, not a single UUID."""
        return await content().get_block_uuid(page_uuid)

    @server.tool(name="getBlock", structured_output=True)
    async def get_block(block_uuid: str) -> dict[str, Any]:
        """Read one exact block. A missing UUID returns found=false rather than raising."""
        return await content().find_block(block_uuid)

    @server.tool(name="pageStats", structured_output=True)
    async def page_stats(page_uuid: str) -> dict[str, Any]:
        """Counts for one page: own blocks, subtree blocks, nested pages, true orphans, and inbound refs, tag holders and property values. Returns integers only, so a full-graph audit costs a fixed payload per page rather than one proportional to page size. Also reports ALIAS relations -- is_alias_of names the page that declares this one as an alias, and aliases lists the ones it declares itself. Check them before deleting: no count here would otherwise reveal the relation, so an empty page that is a working alias reads as a dead stub, and the relation cannot be rebuilt through this API afterwards."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return await content().page_stats(page_uuid)

    @server.tool(name="findBacklinks", structured_output=True)
    async def find_backlinks(target_uuid: str) -> dict[str, Any]:
        """Everything referring to a page, block or tag. Reports three mechanisms separately: refs (what Logseq's backlink panel counts), tags, and property values pointing at the target. Property values are references in the DB but do not appear in the UI panel. Nothing rewrites these on delete, so run this before removing anything."""
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getPageUUID, getBlockUUID or getTagUUID")
        return await content().find_backlinks(target_uuid)

    @server.tool(name="findOrphans", structured_output=True)
    async def find_orphans(page_uuid: str) -> dict[str, Any]:
        """Report blocks whose :block/page differs from their nearest ancestor page. THIS IS NOT DAMAGE. Logseq renders the outline from :block/parent, so such blocks display normally and are reachable in the UI; only queries written against :block/page miss them. Use this to understand a surprising query result, not as a repair signal -- there is nothing to repair, and moving these blocks changes their order for no benefit. Nested pages are reported separately as ordinary structure."""
        page_uuid = require_uuid(
            page_uuid, role="page_uuid", hint="getPageUUID")
        return await content().find_orphans(page_uuid)

    @server.tool(name="searchBlocks", structured_output=True)
    async def search_blocks(
        text: str,
        page_uuid: str | None = None,
        regex: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find every entity whose title contains a string -- the way to locate a typo, a phrase, or duplicated content without re-reading whole pages. Returns terse rows: uuid, kind, title, order, and the page each sits on, which is what updateBlock needs. MATCHING IS CASE-SENSITIVE and substring-only; regex refines those rows afterwards rather than widening the search. matches is counted separately from the rows returned, so zero means genuinely nothing found rather than a result set too large to send -- and when there are more matches than can safely be fetched, it reports the count and fetches nothing rather than returning a silently truncated list. Scope with page_uuid where you can: this runs a predicate inside Logseq's DB worker, which is the one query shape known to be able to wedge it, so it is single-attempt and never retried. Pages, tags and property definitions carry titles too and will match; each row says which kind it is."""
        if page_uuid is not None:
            page_uuid = require_uuid(
                page_uuid, role="page_uuid", hint="getPageUUID")
        return await content().search_blocks(
            text, page_uuid=page_uuid, regex=regex, limit=limit)

    @server.tool(name="getBlockTree", structured_output=True)
    async def get_block_tree(
        block_uuid: str, max_depth: int = 20, max_nodes: int = 1000
    ) -> dict[str, Any]:
        """Read a block and its descendants as a nested tree. Returns truncated=true when a limit stops traversal."""
        return await content().find_block_tree(
            block_uuid, max_depth=max_depth, max_nodes=max_nodes)

    @server.tool(name="createBlock", structured_output=True)
    async def create_block(
        parent_uuid: str,
        title: str,
        dry_run: bool = False,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Create one block. parent_uuid may be a page UUID (top-level block) or a block UUID (nested child). Only the title can be set at creation; tags and position are follow-up calls. The block is appended after the parent's existing children. verbose=false returns identity and position only, which is all a creation tells you that you did not already know."""
        return (await content().create_block(
            parent_uuid, title, dry_run=dry_run)).to_dict(verbose)

    @server.tool(name="createPageofBlocks", structured_output=True)
    async def create_page_of_blocks(
        page_uuid: str,
        outline: str,
        dry_run: bool = False,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Build an indented outline on a page. Structure comes from INDENTATION ONLY -- a leading markdown bullet is stripped, anything else becomes part of the title. Costs one call per parent that has children. The whole outline is validated before the first write, so a malformed one commits nothing. verbose=false returns a UUID per created block instead of the whole block, which matters here because the titles echoed back are the outline you just sent."""
        return await content().create_page_of_blocks(
            page_uuid, outline, dry_run=dry_run, verbose=verbose)

    @server.tool(name="updateBlock", structured_output=True)
    async def update_block(
        block_uuid: str,
        title: str,
        dry_run: bool = False,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Edit one block's title. Does not create, move, nest, or delete. verbose=true returns the block before and after, which is worth having when Logseq rewrote what you sent -- it parses content on write, so [[X]] comes back as [[uuid]]. verbose=false when you only need to know it landed."""
        return (await content().update_block(
            block_uuid, title, dry_run=dry_run)).to_dict(verbose)

    @server.tool(name="splitBlock", structured_output=True)
    async def split_block(
        block_uuid: str,
        offset: int | None = None,
        delimiter: str | None = None,
    ) -> dict[str, Any]:
        """Split one block into several siblings, in document order, losing no text -- the inverse of a merge, which updateBlock cannot express since it only sets a title. Use it to free a heading that is fused onto the tail of a pasted paragraph. Pass exactly one of offset (a character index, one split) or delimiter (splits on EVERY occurrence and consumes it, so one call frees several trapped headings; a blank line is '\n\n'). NOT ATOMIC, and the order is the safety property: the new parts are CREATED FIRST and the original truncated LAST, so a failure mid-way leaves the text duplicated rather than truncated -- visible and repairable instead of lost prose. Any failure reports the created UUIDs so it can be undone. Refused before any write: a split producing an empty part, a delimiter that does not occur, and any part containing a line starting with '- ', which Logseq would truncate."""
        block_uuid = require_uuid(
            block_uuid, role="block_uuid", hint="getBlockUUID")
        return await content().split_block(
            block_uuid, offset=offset, delimiter=delimiter)

    @server.tool(name="moveBlock", structured_output=True)
    async def move_block(
        block_uuid: str,
        target_uuid: str,
        placement: Literal[
            "child", "last-child", "before", "after"
        ] = "child",
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Move a block and its subtree relative to a target. placement=child PREPENDS under the target, so moving several blocks with it reverses their order -- use last-child to APPEND, which is what relocating a sequence needs. Both accept a page target, moving the block to the page's top level; before and after place it as a sibling and need a block target. The API returns nothing on a move, so the result is verified by reading the block back and checking its parent, its owning page, and that descendants followed; last-child additionally confirms the block ended up last. PASS verbose=false when chaining moves: a move changes position, not content, so the full envelope costs the block's own text twice to tell you a parent id."""
        block_uuid = require_uuid(
            block_uuid, role="block_uuid", hint="getBlockUUID")
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getBlockUUID or getPageUUID")
        return (await content().move_block(
            block_uuid, target_uuid, placement=placement)).to_dict(verbose)

    @server.tool(name="moveBlocks", structured_output=True)
    async def move_blocks(
        block_uuids: list[str],
        target_uuid: str,
        placement: Literal[
            "child", "last-child", "before", "after"
        ] = "last-child",
        all_or_nothing: bool = False,
    ) -> dict[str, Any]:
        """Relocate a LIST of blocks, in the order given, in one call -- the tool for moving a flat run of siblings from a journal to a page. The first block is placed by placement (last-child appends the run after whatever the target already held; child puts it at the top), and each later block is placed after the one before it, which is what preserves the order. Costs two calls per block rather than the eight a single moveBlock needs, because the target is read once and the guards run once over the whole set. Returns a verdict per block plus a summary, with no entity payloads. NOT ATOMIC: it stops at the first block that does not verify, and moved says exactly which landed -- the rest of the list would otherwise be positioned relative to a block that did not move. Order is verified by reading the destination once, since a correct parent with the wrong order is the failure this exists to prevent. all_or_nothing moves the landed blocks back under their original parents, but CANNOT restore their position there -- :block/order is unwritable -- so read its note before relying on it. Capped at 50 per call; the remainder come back in not_attempted, in order, to pass in a further call."""
        block_uuids = [
            require_uuid(u, role=f"block_uuids[{i}]", hint="getBlockUUID")
            for i, u in enumerate(block_uuids or [])
        ]
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getBlockUUID or getPageUUID")
        return await content().move_blocks(
            block_uuids, target_uuid, placement=placement,
            all_or_nothing=all_or_nothing)

    @server.tool(name="removeBlock", structured_output=True)
    async def remove_block(
        block_uuid: str, verbose: bool = True
    ) -> dict[str, Any]:
        """Delete a block and its entire subtree, then verify every UUID in that subtree is absent. verbose=true returns the subtree as it was, which is the only record of what was deleted; verbose=false reduces it to a count."""
        return (await content().remove_block(block_uuid)).to_dict(verbose)

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
        title: str,
        options: dict[str, Any] | None = None,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Create a tag. The ident is assigned by Logseq, not derived from the title, and is returned in verified_state -- read it rather than constructing it. Tags and pages share one title space, so a title an existing page holds is refused. verbose=false still returns the ident, since that is the point of the call."""
        return (await mutations().create_tag(title, options)).to_dict(verbose)

    @server.tool(name="deleteTag", structured_output=True)
    async def delete_tag(
        tag_uuid: str,
        acknowledge_child_reparent: bool = False,
        acknowledge_detach: bool = False,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Delete one tag entity. Everything carrying the tag loses it, so acknowledge_detach is required when any page or block holds it, and acknowledge_child_reparent when child tags would be reparented. Run getTagUsers first to see what is affected."""
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return (await mutations().delete_tag(
            tag_uuid,
            acknowledge_child_reparent=acknowledge_child_reparent,
            acknowledge_detach=acknowledge_detach)).to_dict(verbose)

    @server.tool(name="addTag", structured_output=True)
    async def add_tag(
        target_uuid: str, tag_uuid: str, verbose: bool = True
    ) -> dict[str, Any]:
        """Attach an existing tag to a page or a block. target_uuid may be either. Both arguments are UUIDs; the target comes first. verbose=false is usually right: the envelope otherwise returns the whole target entity, whose content is unrelated to the tag being added."""
        # Validate both before either lookup, so a call with the arguments
        # swapped or a title in one slot names the offending argument rather
        # than failing halfway through.
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getPageUUID or getBlockUUID")
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return (await mutations().add_tag(
            target_uuid, tag_uuid)).to_dict(verbose)

    @server.tool(name="removeTag", structured_output=True)
    async def remove_tag(
        target_uuid: str, tag_uuid: str, verbose: bool = True
    ) -> dict[str, Any]:
        """Detach one tag from a page or a block. Other tags on the target are untouched and the tag entity survives. Both arguments are UUIDs; the target comes first."""
        target_uuid = require_uuid(
            target_uuid, role="target_uuid",
            hint="getPageUUID or getBlockUUID")
        tag_uuid = require_uuid(tag_uuid, role="tag_uuid", hint="getTagUUID")
        return (await mutations().remove_tag(
            target_uuid, tag_uuid)).to_dict(verbose)

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
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Create a property definition. Pass a plain title, never a namespaced ident -- spaces are stripped from it, so "MCPT Check" becomes MCPTCheck. schema takes a type: default (text), number, string, datetime, checkbox, url, node, page, class, property, or map. The namespace is assigned from caller identity and cannot be chosen. The stored type is verified against the requested one. verbose=false still returns the assigned ident, which every other property tool needs."""
        return (await mutations().create_property(
            title, schema, options)).to_dict(verbose)

    @server.tool(name="deleteProperty", structured_output=True)
    async def delete_property(
        property_ident: str,
        acknowledge_value_loss: bool = False,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Delete a property definition graph-wide, taking every value with it. Not reversible -- recreating mints a new entity and the old values do not return. acknowledge_value_loss is required when anything holds a value; run getProperyUsers first to see what is affected."""
        return (await mutations().delete_property(
            property_ident,
            acknowledge_value_loss=acknowledge_value_loss)).to_dict(verbose)

    @server.tool(name="addProperty", structured_output=True)
    async def add_property(
        target_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
        verbose: bool = True,
    ) -> dict[str, Any]:
        """Set a property value on a page or a block. target_uuid may be either. Reference-typed properties take an entity id, not a literal; closed enums such as Status take one of the entities from listClosedValues. Only properties in this plugin's own namespace can be written. verbose=true returns the target entity, which is how you see the stored value; verbose=false when you only need to know it landed."""
        return (await mutations().set_property(
            target_uuid, property_ident, value, options)).to_dict(verbose)

    @server.tool(name="removeProperty", structured_output=True)
    async def remove_property(
        target_uuid: str, property_ident: str, verbose: bool = True
    ) -> dict[str, Any]:
        """Clear a property value from a page or a block. The property definition survives and other targets keep their values -- use deleteProperty to remove the definition itself."""
        return (await mutations().clear_property(
            target_uuid, property_ident)).to_dict(verbose)

    # ------------------------------------------------------------ lists
    # Each returns the whole of one kind. Most take no arguments; the two page
    # listings take with_counts, which is opt-in so the cheap listing stays
    # cheap.

    @server.tool(name="listPages")
    async def list_pages(
        with_counts: bool = False, limit: int | None = None
    ) -> Any:
        """List all live pages. Recycled pages are excluded -- they keep the Page class and would otherwise appear live. with_counts adds own_blocks, content_blocks and refs to each entry, which costs four queries in total rather than one pageStats per page; it returns an envelope with total/counted/truncated instead of a bare list, and is capped at 500 pages per call because four numbers per page adds up on a large graph. Pair content_blocks with refs to judge whether a page carries anything."""
        return await content().list_pages(
            with_counts=with_counts, limit=limit)

    @server.tool(name="listJournals")
    async def list_journals(
        with_counts: bool = False, limit: int | None = None
    ) -> Any:
        """List all journal pages, newest first, with their journal day as an integer date. with_counts adds own_blocks, content_blocks and refs to each entry -- use it to decide which journals hold anything worth reading, which otherwise costs one pageStats per journal. It stays four queries however many journals there are, and returns an envelope with total/counted/truncated instead of a bare list. own_blocks counts seeded and trailing empty blocks, so content_blocks is the figure to pair with refs."""
        return await content().list_journals(
            with_counts=with_counts, limit=limit)

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
        """List every enum property with its permitted values. Required before setting a closed property -- the value must be one of these entities."""
        # `:block/closed-value-property` lives on the VALUE, pointing back at
        # its property. Two wrong guesses preceded this one:
        #
        #   :property/closed-values   -- what getAllProperties REPORTS on the
        #                                property, but there is no such datom;
        #                                it is synthesised from this reverse
        #                                relationship
        #   :closed-value-property    -- the name a probe found, but the API
        #                                strips the :block/ and :db/ prefixes
        #                                when serialising attribute names
        #
        # So a response is not a guide to queryable attribute names. `tags`,
        # `ident`, `uuid` and `title` all come back bare and are really
        # :block/tags, :db/ident, :block/uuid and :block/title.
        return await query(
            "[:find (pull ?prop [:db/id :db/ident :block/title]) "
            "(pull ?value [:db/id :db/ident :block/title "
            ":logseq.property/value :block/order]) "
            ":where [?value :block/closed-value-property ?prop]]")

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
            # The ident goes into query TEXT -- an attribute position takes no
            # `:in` binding -- so it is shape-checked even though it came from
            # Logseq rather than from the caller. A built-in with a bare ident
            # and no namespace is skipped rather than queried.
            try:
                ident = require_ident(ident)
            except IdentifierError:
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
            "inspectPage with a UUID instead. A title reported as not found "
            "may still be taken for writing -- recycled pages do not resolve "
            "here; check isTitleAvailable."
        ),
        "find_duplicate_titles": (
            "Takes no identifiers. normalize is exact, loose or fuzzy; "
            "fuzzy is capped at 2000 titles because it compares every pair."
        ),
        "is_title_available": (
            "Pass a title, not a UUID or an ident."
        ),
        "delete_page": (
            "Pass an exact page UUID. If entities reference the page, pass "
            "acknowledge_reference_rewrite=true -- those references are not "
            "rewritten. If it is in an alias relation, pass "
            "acknowledge_alias_loss=true -- that one cannot be repaired "
            "afterwards, and pageStats reports both."
        ),
        "retitle_over_duplicate": (
            "Pass the UUID of the page that should END UP with the title, "
            "then the title. The holder is found by title, not passed in."
        ),
        "inspect_page": (
            "Pass an exact page UUID and one of: page, blocks, tags, "
            "properties, declared, all."
        ),
        "create_page": (
            "Use a title no existing page, tag or block holds -- they share "
            "one title space. If the failure is an EDN validation error "
            "instead, the title is not the problem: the graph is refusing the "
            "transaction, and no retry or rename will help."
        ),
        "rename_page": (
            "Pass an exact page UUID and a title nothing else already uses."
        ),
        "clear_page": "Pass an exact page UUID, not a block UUID.",
        "import_page": (
            "Pass a page UUID or a page title, then the content. As a string, "
            "every block line must begin with '- ' and indentation alone "
            "creates nothing. As a list, one element per block with explicit "
            "depth -- use that form for blocks containing newlines."
        ),
        "repair_links": (
            "Omit page_uuid to scan the whole graph. Unresolvable names are "
            "skipped and reported, never created. To create missing pages "
            "you must pass create_missing AND acknowledge_page_creation; to "
            "create missing tags, create_missing AND "
            "acknowledge_tag_creation. Use dry_run to preview both."
        ),
        "find_orphans": (
            "Pass an exact page UUID. The result is informational -- the "
            "condition it reports is cosmetic, not damage."
        ),
        "page_stats": "Pass an exact page UUID, not a block UUID.",
        "find_backlinks": (
            "Pass the UUID of the entity being referred to -- a page, block "
            "or tag."
        ),
        "get_block_uuid": "Pass an exact page UUID, not a block UUID.",
        "get_block": "Pass an exact block UUID.",
        "search_blocks": (
            "Pass at least two characters. Scope with page_uuid when you "
            "can, and use a longer string rather than a bigger limit when "
            "there are too many matches."
        ),
        "get_block_tree": (
            "Pass an exact block UUID, max_depth 0-100, max_nodes 1-1000."
        ),
        "create_block": (
            "Pass an exact parent UUID -- a page UUID for a top-level block or "
            "a block UUID to nest -- and a non-empty title. A page title will "
            "not resolve."
        ),
        "create_page_of_blocks": (
            "Pass an exact page UUID and an outline whose indentation is "
            "consistent and never jumps more than one level."
        ),
        "update_block": "Pass an exact block UUID and a non-empty title.",
        "split_block": (
            "Pass an exact block UUID and exactly one of offset or "
            "delimiter. Read the block first -- the delimiter must occur in "
            "the text as Logseq stored it, which may not be as you sent it."
        ),
        "move_blocks": (
            "Pass the blocks in the order they should end up, then the "
            "target. They must be a flat set -- one cannot be a descendant "
            "of another, since a move carries the whole subtree. Use "
            "last-child to append the run."
        ),
        "move_block": (
            "Pass the block to move, then the target, then placement. The "
            "target may be a block, or a page when placement is child or "
            "last-child. Use last-child to append; child prepends. A block "
            "cannot be moved inside its own subtree."
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

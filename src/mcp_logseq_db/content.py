"""Verified page and block operations.

WHAT CHANGED, AND WHY
---------------------
`insert_block` and `move_block` are gone. Both routed through
`logseq.DB.insertBlock` / `logseq.DB.moveBlock` with graph-worker CLI
fallbacks, and both existed because a hardcoded capability list reported the
HTTP block methods as rejected. `removeBlock` works over HTTP, and nested
creation works through `upsertNodes` -- so the CLI path was routing around
methods that were never broken. Block movement has no established route and no
tool; it stays out until one is found by testing.

`data["page-id"]` is a PARENT pointer, not a page pointer. Passing a page UUID
creates a top-level block; passing a block UUID nests. The previous
implementation rejected block parents outright, which is what forced nesting
onto the CLI in the first place.

`data` is a closed allowlist -- only `title` and `page-id`. `tags` at creation
is rejected by the API as a disallowed key, so tagging is a follow-up call.

Page edits through `upsertNodes` are gone: `edit` + `page` returns "Editing a
page, tag or property isn't supported yet" from Logseq itself.

VERIFICATION
------------
This API returns success for calls that do nothing. Every write here is
followed by a read-back, and an unverified write is an error rather than a
quiet success. `{:block N}` responses are recorded but never treated as
evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from ._shared import VerifiedWriteHelpers
from .client import LogseqDBClient, poll_readback, serialized_write

MAX_BATCH_OPERATIONS = 100
MAX_SUBTREE_NODES = 1000

# Resolved to a :db/id at call time. Integer ids are renumbered when a graph is
# rebuilt, so nothing here hardcodes them.
PROPERTY_CLASS = ":logseq.class/Property"
PAGE_CLASS = ":logseq.class/Page"

UUID_PATTERN = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# getPage detail selectors. Each answers a different question; they are not
# interchangeable. A page's own tags and its blocks' tags live in different
# places, and properties that are declared but unset appear in neither.
PAGE_DETAILS = ("page", "blocks", "tags", "properties", "declared", "all")


@dataclass(frozen=True, kw_only=True)
class ContentResult:
    validation: Any
    response: Any
    verified_entities: tuple[dict[str, Any], ...]
    recovered_after_timeout: bool = False
    verified: bool = True
    diagnostic: str | None = None
    previous_entities: tuple[dict[str, Any], ...] = ()
    observed_entities: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerifiedContent(VerifiedWriteHelpers):
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client

    # ------------------------------------------------------------ page reads

    async def get_page_uuid(self, title: str) -> dict[str, Any]:
        """
        Resolve a page title to exactly one UUID.

        Refuses an ambiguous match rather than returning the first hit. Page
        titles are not unique, and selecting a write target from a fuzzy match
        is how the wrong entity gets modified.
        """
        self._validate_title(title)
        page_class = await self._class_id(PAGE_CLASS)

        # Exact display title first.
        query = (
            "[:find [(pull ?page [:db/id :block/uuid :block/name :block/title "
            ":logseq.property/deleted-at]) ...] :in $ ?class :where "
            "[?page :block/tags ?class] "
            f"[?page :block/title {json.dumps(title)}]]"
        )
        pages = await self._query_list(
            query, "Page title lookup", page_class)

        if not pages:
            # Fall back to the normalized name, which is lowercased. Without
            # this, the exact string Logseq stores in :block/name fails to
            # resolve the page it names.
            query = (
                "[:find [(pull ?page [:db/id :block/uuid :block/name "
                ":block/title :logseq.property/deleted-at]) ...] "
                ":in $ ?class :where [?page :block/tags ?class] "
                f"[?page :block/name {json.dumps(title.lower())}]]"
            )
            pages = await self._query_list(
                query, "Page name lookup", page_class)

        live = [p for p in pages
                if p.get(":logseq.property/deleted-at") is None]
        if not live:
            return {"found": False, "title": title, "page_uuid": None}
        if len(live) > 1:
            return {
                "found": False,
                "title": title,
                "page_uuid": None,
                "reason": f"{len(live)} pages share this title; use a UUID",
                "candidates": [p.get("uuid") for p in live],
            }
        return {"found": True, "title": title, "page_uuid": live[0].get("uuid")}

    async def get_page(
        self, page_uuid: str, detail: str = "page"
    ) -> dict[str, Any]:
        """Read one page at the requested level of detail."""
        page_uuid = self._validated_uuid(page_uuid)
        if detail not in PAGE_DETAILS:
            raise ValueError(
                "detail must be one of: " + ", ".join(PAGE_DETAILS))

        page = await self._optional_entity_by_uuid(page_uuid)
        if page is None:
            return {"found": False, "page_uuid": page_uuid, "page": None}
        if not page.get("name"):
            return {
                "found": False,
                "page_uuid": page_uuid,
                "page": None,
                "reason": "target is a block, not a page",
            }

        result: dict[str, Any] = {
            "found": True, "page_uuid": page_uuid, "page": page}
        if detail == "page":
            return result
        if detail in ("blocks", "all"):
            result["blocks"] = await self.get_block_uuid(page_uuid)
        if detail in ("tags", "all"):
            result["tags"] = await self._page_scope_tags(page["id"])
        if detail in ("properties", "all"):
            result["properties"] = await self._page_scope_properties(page["id"])
        if detail in ("declared", "all"):
            result["declared_properties"] = await self._declared_properties(
                page["id"])
        return result

    async def _page_scope_tags(self, page_id: int) -> list[dict[str, Any]]:
        """
        Tags on the page itself and on every block it owns.

        `:block/page` reaches any depth, so nesting is covered; the page entity
        has no `:block/page` of its own and is unioned in separately.
        """
        query = (
            "[:find [(pull ?holder [:db/id :block/uuid :block/title "
            ":block/name {:block/tags [:db/id :db/ident :block/title]}]) ...] "
            ":in $ ?page :where "
            "(or-join [?page ?holder] "
            "[(identity ?page) ?holder] [?holder :block/page ?page]) "
            "[?holder :block/tags _]]"
        )
        return await self._query_list(query, "Page tag lookup", page_id)

    async def _page_scope_properties(self, page_id: int) -> list[dict[str, Any]]:
        """
        Property values on the page and its blocks.

        Only properties that have a value appear; an unset property has no
        datom at all. Use `declared` for the slots a page could fill.
        """
        query = (
            "[:find (pull ?prop [:db/id :db/ident :block/title]) "
            "(pull ?holder [:db/id :block/uuid :block/title :block/name]) "
            "?value "
            ":in $ ?page ?class :where "
            "(or-join [?page ?holder] "
            "[(identity ?page) ?holder] [?holder :block/page ?page]) "
            "[?prop :block/tags ?class] [?prop :db/ident ?attr] "
            "[?holder ?attr ?value]]"
        )
        rows = await self._query_list(
            query, "Page property lookup", page_id,
            await self._class_id(PROPERTY_CLASS))

        parsed = [row for row in rows if isinstance(row, list) and len(row) == 3]
        # Reference-typed values arrive as integer entity ids. Resolve them in
        # one extra query rather than per row; scalars are left as they are.
        resolved = await self._resolve_entities(
            {row[2] for row in parsed if isinstance(row[2], int)
             and not isinstance(row[2], bool)})

        return [
            {
                "property": prop,
                "holder": holder,
                "value": value,
                "value_entity": resolved.get(value)
                if isinstance(value, int) and not isinstance(value, bool)
                else None,
            }
            for prop, holder, value in parsed
        ]

    async def _resolve_entities(
        self, entity_ids: set[int]
    ) -> dict[int, dict[str, Any]]:
        """
        Resolve entity ids to readable entities in one call.

        `[?e ?a _]` matches any entity carrying any attribute, which is the
        broadest safe pattern -- value entities need not have a :block/uuid, so
        matching on that would silently drop them.
        """
        if not entity_ids:
            return {}
        query = (
            "[:find [(pull ?e [:db/id :db/ident :block/title "
            ":logseq.property/value]) ...] "
            ":in $ [?e ...] :where [?e ?a _]]"
        )
        found = await self._query_list(
            query, "Value entity lookup", sorted(entity_ids))
        return {e["id"]: e for e in found
                if isinstance(e, dict) and isinstance(e.get("id"), int)}

    async def _declared_properties(self, page_id: int) -> list[dict[str, Any]]:
        """
        Property slots the page inherits from its classes.

        These have no datoms on the page. They are the source of the properties
        the UI shows as empty, and no query over the page will surface them.
        """
        query = (
            "[:find (pull ?class [:db/ident :block/title]) "
            "(pull ?prop [:db/id :db/ident :block/uuid :block/title "
            ":logseq.property/type]) :in $ ?page :where "
            "[?page :block/tags ?class] "
            "[?class :logseq.property.class/properties ?prop]]"
        )
        rows = await self._query_list(query, "Declared property lookup", page_id)
        return [{"class": r[0], "property": r[1]}
                for r in rows if isinstance(r, list) and len(r) == 2]

    # ----------------------------------------------------------- block reads

    async def get_block(self, block_uuid: str) -> dict[str, Any]:
        """Read one exact non-page block."""
        block = await self._entity_by_uuid(block_uuid)
        if block.get("name"):
            raise ValueError("UUID identifies a page, not a block")
        return block

    async def find_block(self, block_uuid: str) -> dict[str, Any]:
        """Return an explicit found/block envelope for one exact block UUID."""
        block_uuid = self._validated_uuid(block_uuid)
        block = await self._optional_entity_by_uuid(block_uuid)
        if block is None:
            return {"found": False, "block_uuid": block_uuid, "block": None}
        if block.get("name"):
            return {
                "found": False,
                "block_uuid": block_uuid,
                "block": None,
                "reason": "target is a page, not a block",
            }
        return {"found": True, "block_uuid": block_uuid, "block": block}

    async def get_block_uuid(self, page_uuid: str) -> list[dict[str, Any]]:
        """
        Every block on a page, at any depth.

        Named for the tool it backs. `:block/page` rather than `:block/parent`
        is deliberate: parent reaches one level, page reaches all of them.
        """
        page_uuid = self._validated_uuid(page_uuid)
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")
        query = (
            "[:find [(pull ?block [:db/id :block/uuid :block/title "
            ":block/order {:block/parent [:db/id :block/uuid]}]) ...] "
            ":in $ ?page :where [?block :block/page ?page]]"
        )
        blocks = await self._query_list(query, "Page block lookup", page["id"])
        # Fractional-index strings sort lexicographically into document order.
        blocks.sort(key=lambda b: str(b.get("order", "")))
        return blocks

    async def find_block_tree(
        self,
        block_uuid: str,
        *,
        max_depth: int = 20,
        max_nodes: int = MAX_SUBTREE_NODES,
    ) -> dict[str, Any]:
        """
        Read one block subtree with a single page-scoped query.

        `max_depth` counts generations BELOW the root, so 0 is the root alone
        and 1 is the root plus its children. Use `max_nodes` to cap the total
        instead; `truncated` reports whichever bound stopped traversal.
        """
        block_uuid = self._validated_uuid(block_uuid)
        if not isinstance(max_depth, int) or not 0 <= max_depth <= 100:
            raise ValueError("max_depth must be an integer between 0 and 100")
        if not isinstance(max_nodes, int) or not 1 <= max_nodes <= MAX_SUBTREE_NODES:
            raise ValueError(
                f"max_nodes must be between 1 and {MAX_SUBTREE_NODES}")

        root = await self._optional_entity_by_uuid(block_uuid)
        if root is None or root.get("name"):
            return {
                "found": False,
                "block_uuid": block_uuid,
                "block": None,
                "node_count": 0,
                "truncated": False,
                **({"reason": "target is a page, not a block"} if root else {}),
            }
        page_id = self._reference_id(root.get("page"))
        if page_id is None:
            raise RuntimeError("Block has no owning page reference")

        query = (
            "[:find [(pull ?block [*]) ...] :in $ ?page :where "
            "[?block :block/page ?page]]"
        )
        page_blocks = await self._query_list(query, "Page block lookup", page_id)

        by_parent: dict[int, list[dict[str, Any]]] = {}
        for entity in page_blocks:
            if not isinstance(entity.get("id"), int):
                continue
            parent_id = self._reference_id(entity.get("parent"))
            if parent_id is not None:
                by_parent.setdefault(parent_id, []).append(entity)
        for children in by_parent.values():
            children.sort(key=lambda e: str(e.get("order", "")))

        node_count = 0
        truncated = False
        visited: set[int] = set()

        def build(entity: dict[str, Any], depth: int) -> dict[str, Any]:
            nonlocal node_count, truncated
            entity_id = entity["id"]
            if entity_id in visited:
                raise RuntimeError("Block hierarchy contains a cycle")
            visited.add(entity_id)
            node_count += 1
            result = dict(entity)
            descendants = by_parent.get(entity_id, [])
            if descendants and (depth >= max_depth or node_count >= max_nodes):
                truncated = True
                result["children"] = []
                return result
            children = []
            for child in descendants:
                if node_count >= max_nodes:
                    truncated = True
                    break
                children.append(build(child, depth + 1))
            result["children"] = children
            return result

        tree = build(root, 0)
        return {
            "found": True,
            "block_uuid": block_uuid,
            "block": tree,
            "node_count": node_count,
            "truncated": truncated,
        }

    # ----------------------------------------------------------- page writes

    async def create_page(
        self, title: str, *, dry_run: bool = False
    ) -> ContentResult:
        """
        Create one page.

        Goes through the same batch path as block creation, so it inherits the
        duplicate-title snapshot and the read-back. A page whose title already
        exists is rejected rather than created, because verification could not
        then tell the new page from the old one.
        """
        return await self.upsert_nodes(
            [{"operation": "add", "entityType": "page",
              "data": {"title": title}}],
            dry_run=dry_run)

    @serialized_write
    async def rename_page(self, page_uuid: str, new_title: str) -> ContentResult:
        """
        Rename a page, verifying by UUID rather than by the new title.

        Reading back by title would not distinguish a rename from Logseq having
        created a second page and left the original alone. Reading the original
        UUID and checking its title catches that, and also confirms the entity
        is still a page.
        """
        page_uuid = self._require_entity(self._validated_uuid(page_uuid))
        self._require_title(new_title)
        page = await self._page_by_uuid(page_uuid)

        clashes = [e for e in await self._entities_by_title(new_title)
                   if e["id"] != page["id"]]
        if clashes:
            raise ValueError(
                f"An entity titled {new_title!r} already exists; renaming onto "
                "it would make the two indistinguishable")

        response: Any = None
        timed_out = False
        try:
            response = await self._client.call(
                "logseq.DB.renamePage", [page_uuid, new_title])
        except httpx.TimeoutException:
            timed_out = True

        current = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(page_uuid),
            lambda value: value is not None and value.get("title") == new_title,
        )
        if current is None or current.get("title") != new_title:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    "Rename was not observed. This route may take a page name "
                    "rather than a UUID."),
                previous_entities=(page,),
                observed_entities=(current,) if current else ())
        if not current.get("name"):
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic="The entity lost its page identity during the rename",
                previous_entities=(page,), observed_entities=(current,))
        return ContentResult(
            validation=None, response=response, verified_entities=(current,),
            recovered_after_timeout=timed_out, previous_entities=(page,))

    @serialized_write
    async def delete_page(
        self, page_uuid: str, *, acknowledge_reference_rewrite: bool = False
    ) -> ContentResult:
        """
        Delete a page, which on this build recycles rather than destroys it.

        A recycled page keeps its UUID, tags, refs and blocks, gaining
        :logseq.property/deleted-at. Inbound references are NOT rewritten, so
        anything linking to it keeps pointing at a page that no longer appears
        in listings -- which is why references are surfaced and require
        acknowledgement.

        The identifier this route wants is unconfirmed, so the UUID is tried
        first and the page name second. Whichever worked is reported.
        """
        page_uuid = self._require_entity(self._validated_uuid(page_uuid))
        page = await self._page_by_uuid(page_uuid)
        if page.get(":logseq.property/deleted-at") is not None:
            raise ValueError("Page is already recycled")

        blocks = await self.get_block_uuid(page_uuid)
        inbound = await self._inbound_references(page["id"])
        previous = (page, *blocks)

        if inbound and not acknowledge_reference_rewrite:
            return ContentResult(
                validation=None, response=None, verified_entities=(),
                verified=False,
                diagnostic=(
                    f"{len(inbound)} entities reference this page and those "
                    "references are not rewritten on delete; set "
                    "acknowledge_reference_rewrite=true to proceed"),
                previous_entities=previous,
                observed_entities=tuple(inbound))

        def deleted(value: dict[str, Any] | None) -> bool:
            # Either outcome counts: the entity may vanish, or survive carrying
            # a deletion timestamp. Both mean the page is gone from listings.
            return (value is None
                    or value.get(":logseq.property/deleted-at") is not None)

        response: Any = None
        timed_out = False
        used = "uuid"
        try:
            response = await self._client.call(
                "logseq.DB.deletePage", [page_uuid])
        except httpx.TimeoutException:
            timed_out = True

        current = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(page_uuid),
            deleted)

        if not deleted(current) and not timed_out:
            # The UUID form did nothing -- silently, as this API does. Fall
            # back to the name before reporting failure.
            used = "name"
            try:
                response = await self._client.call(
                    "logseq.DB.deletePage", [page["name"]])
            except httpx.TimeoutException:
                timed_out = True
            current = await poll_readback(
                self._client,
                lambda: self._optional_entity_by_uuid(page_uuid),
                deleted)

        if not deleted(current):
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    "Deletion was not observed with either a UUID or a page "
                    "name; the page is still present"),
                previous_entities=previous,
                observed_entities=(current,) if current else ())

        return ContentResult(
            validation=None, response=response,
            verified_entities=(current,) if current else (),
            recovered_after_timeout=timed_out,
            diagnostic=(
                f"Page recycled via its {used}. It keeps its UUID and tags, "
                f"and {len(inbound)} inbound reference(s) were not rewritten."
                if current else f"Page removed via its {used}."),
            previous_entities=previous,
            observed_entities=tuple(inbound))

    @serialized_write
    async def clear_page(self, page_uuid: str) -> ContentResult:
        """
        Delete every block on a page, keeping the page itself.

        There is no batch delete, so this is one call per top-level block --
        each taking its subtree with it. The page entity, its tags and its
        property values are untouched.
        """
        page_uuid = self._require_entity(self._validated_uuid(page_uuid))
        page = await self._page_by_uuid(page_uuid)
        before = await self.get_block_uuid(page_uuid)
        top_level = await self._children_of(page_uuid)

        if not top_level:
            return ContentResult(
                validation=None, response=None, verified_entities=(),
                diagnostic="The page already has no blocks",
                previous_entities=(page,))

        response: Any = None
        timed_out = False
        for block in top_level:
            try:
                # A subtree deleted earlier in the loop may already have taken
                # this block, so a miss here is expected rather than an error.
                response = await self._client.call(
                    "logseq.DB.removeBlock", [block["uuid"]])
            except httpx.TimeoutException:
                timed_out = True

        remaining = await poll_readback(
            self._client,
            lambda: self.get_block_uuid(page_uuid),
            lambda blocks: not blocks)

        if remaining:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=f"{len(remaining)} block(s) remain on the page",
                previous_entities=tuple(before),
                observed_entities=tuple(remaining))

        return ContentResult(
            validation=None, response=response, verified_entities=(page,),
            recovered_after_timeout=timed_out,
            diagnostic=f"Removed {len(top_level)} top-level block(s)",
            previous_entities=tuple(before))

    async def _page_by_uuid(self, page_uuid: str) -> dict[str, Any]:
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")
        return page

    async def _inbound_references(self, page_id: int) -> list[dict[str, Any]]:
        query = (
            "[:find [(pull ?entity [:db/id :block/uuid :block/title "
            ":block/name {:block/page [:db/id :block/title]}]) ...] "
            ":in $ ?page :where [?entity :block/refs ?page]]"
        )
        return await self._query_list(query, "Inbound reference lookup", page_id)

    # ---------------------------------------------------------- block writes

    async def create_block(
        self,
        parent_uuid: str,
        title: str,
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """
        Create one block under a page or another block.

        `parent_uuid` may be either. Passing a page UUID produces a top-level
        block; passing a block UUID nests. The API field is called `page-id`
        but behaves as a parent pointer.
        """
        return await self.create_many_blocks(
            [{"parent_uuid": parent_uuid, "title": title}], dry_run=dry_run)

    async def create_many_blocks(
        self,
        blocks: list[dict[str, str]],
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """
        Create several blocks in one batched call.

        Whether a batch applies atomically is untested, so verification checks
        each block individually rather than assuming all-or-nothing.
        """
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BATCH_OPERATIONS:
            raise ValueError(
                f"blocks must contain between 1 and {MAX_BATCH_OPERATIONS} items")
        operations = []
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise ValueError(f"block {index} must be an object")
            parent = self._require_entity(
                self._validated_uuid(block.get("parent_uuid")))
            title = block.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"block {index} requires a non-empty title")
            # Block content, not a named entity -- no title scope. See
            # VerifiedWriteHelpers._validate_title.
            self._validate_title(title)
            operations.append({
                "operation": "add",
                "entityType": "block",
                # Only these two keys are permitted; `data` is a closed
                # allowlist and rejects anything else outright.
                "data": {"page-id": parent, "title": title},
            })
        return await self.upsert_nodes(operations, dry_run=dry_run)

    async def update_block(
        self,
        block_uuid: str,
        title: str,
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """Edit one existing block title."""
        return await self.upsert_nodes(
            [{
                "operation": "edit",
                "entityType": "block",
                "id": self._require_entity(self._validated_uuid(block_uuid)),
                "data": {"title": title},
            }],
            dry_run=dry_run,
        )

    @serialized_write
    async def remove_block(self, block_uuid: str) -> ContentResult:
        """
        Delete one block and its subtree, then verify the whole subtree is gone.

        Routed through `logseq.DB.removeBlock` over HTTP. A previous
        implementation used a CLI fallback because a hardcoded capability list
        reported this method as rejected; it is not.
        """
        block_uuid = self._require_entity(self._validated_uuid(block_uuid))
        block = await self._preflight_block(block_uuid, role="target")
        subtree = await self._subtree(block)

        response: Any = None
        timed_out = False
        try:
            response = await self._client.call(
                "logseq.DB.removeBlock", [block_uuid])
        except httpx.TimeoutException:
            timed_out = True

        current = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(block_uuid),
            lambda value: value is None,
        )
        if current is not None:
            return ContentResult(
                validation=None,
                response=response,
                verified_entities=(),
                recovered_after_timeout=timed_out,
                verified=False,
                diagnostic="Deletion was not observed; the block is still present",
                previous_entities=tuple(subtree),
                observed_entities=(current,),
            )

        remaining = []
        for entity in subtree[1:]:
            descendant = await poll_readback(
                self._client,
                lambda u=entity["uuid"]: self._optional_entity_by_uuid(u),
                lambda value: value is None,
            )
            if descendant is not None:
                remaining.append(descendant)
        if remaining:
            return ContentResult(
                validation=None,
                response=response,
                verified_entities=(),
                recovered_after_timeout=timed_out,
                verified=False,
                diagnostic="Target is absent but one or more descendants remain",
                previous_entities=tuple(subtree),
                observed_entities=tuple(remaining),
            )
        return ContentResult(
            validation=None,
            response=response,
            verified_entities=(),
            recovered_after_timeout=timed_out,
            diagnostic="Exact UUID and its subtree are absent after deletion",
            previous_entities=tuple(subtree),
        )

    # ------------------------------------------------------------- outlines

    async def create_page_of_blocks(
        self,
        page_uuid: str,
        outline: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Build an indented outline on a page.

        Costs 2d-1 calls for depth d, independent of width: create a level,
        read back the UUIDs Logseq assigned, create the next. The read-back is
        structural, not defensive -- creation does not return UUIDs and
        `page-id` will not resolve a title, so children have no way to name
        their parents until the level above exists.
        """
        page_uuid = self._require_entity(self._validated_uuid(page_uuid))
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")

        entries = _parse_outline(outline)
        if not entries:
            raise ValueError("Outline is empty")

        depth_max = max(len(path) for path, _ in entries) - 1
        if dry_run:
            return {
                "dry_run": True,
                "levels": depth_max + 1,
                "block_count": len(entries),
                "estimated_calls": 2 * (depth_max + 1) - 1,
            }

        parents: dict[tuple[int, ...], str] = {(): page_uuid}
        created: list[dict[str, Any]] = []

        for depth in range(depth_max + 1):
            level = [(p, t) for p, t in entries if len(p) == depth + 1]
            if not level:
                continue
            await self.create_many_blocks([
                {"parent_uuid": parents[path[:-1]], "title": title}
                for path, title in level
            ])
            for path, title in level:
                parent_uuid = parents[path[:-1]]
                siblings = await self._children_of(parent_uuid)
                match = [s for s in siblings if s.get("title") == title]
                if not match:
                    raise RuntimeError(
                        f"Outline level {depth} reported success but {title!r} "
                        f"is not present under {parent_uuid}"
                    )
                parents[path] = match[-1]["uuid"]
                created.append(match[-1])

        return {
            "verified": True,
            "page_uuid": page_uuid,
            "levels": depth_max + 1,
            "created": created,
        }

    async def _children_of(self, parent_uuid: str) -> list[dict[str, Any]]:
        query = (
            "[:find [(pull ?child [:db/id :block/uuid :block/title "
            ":block/order {:block/parent [:db/id]} {:block/page [:db/id]} "
            "{:block/refs [:db/id :block/uuid]}]) ...] :where "
            f"[?parent :block/uuid #uuid \"{parent_uuid}\"] "
            "[?child :block/parent ?parent]]"
        )
        children = await self._query_list(query, "Child lookup")
        children.sort(key=lambda c: str(c.get("order", "")))
        return children

    # --------------------------------------------------------- batch engine

    @serialized_write
    async def upsert_nodes(
        self,
        operations: list[dict[str, Any]],
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """
        Run a batch of add/edit operations and verify each one.

        `upsertNodes` accepts exactly three combinations: add+page, add+block,
        edit+block. `edit`+`page` is rejected by Logseq, and there is no
        removal verb at all -- `operation` offers only add and edit.
        """
        normalized = await self._validate_operations(operations)

        if dry_run:
            # Local validation only. There is no server-side dry run: the
            # options argument that used to request one is rejected by the API
            # and broke every write that carried it. So this proves the
            # arguments are well formed and the targets exist -- nothing more.
            # It cannot tell you the write will land.
            return ContentResult(
                validation={
                    "checked": "arguments and target existence, locally",
                    "not_checked": "whether the write route works on this build",
                    "operations": normalized,
                },
                response=None,
                verified_entities=(),
                diagnostic=(
                    "Dry run is local validation only and is not evidence that "
                    "the write will succeed."))

        # Snapshot per add so the read-back can tell a new entity from one
        # that already carried the same title. Titles are not unique, so the
        # snapshot is scoped the same way verification is: siblings for a
        # block, the whole graph for a page.
        before_ids: dict[int, set[int]] = {}
        for index, op in enumerate(normalized):
            if op["operation"] != "add":
                continue
            before_ids[index] = {
                e["id"] for e in await self._candidates_for(op)}

        response: Any = None
        timed_out = False
        try:
            # ONE argument. See the note on the options map above.
            response = await self._client.call(
                "logseq.DB.upsertNodes", [normalized])
        except httpx.TimeoutException:
            timed_out = True

        verified: list[dict[str, Any]] = []
        for index, operation in enumerate(normalized):
            if operation["operation"] == "edit":
                verified.append(await self._verify_edit(operation))
                continue
            verified.append(
                await self._verify_add(operation, before_ids[index]))

        return ContentResult(
            validation=None,
            response=response,
            verified_entities=tuple(verified),
            recovered_after_timeout=timed_out,
        )

    async def _verify_edit(self, operation: dict[str, Any]) -> dict[str, Any]:
        expected = operation["data"]["title"]
        entity = await poll_readback(
            self._client,
            lambda: self._entity_by_uuid(operation["id"]),
            lambda value: value.get("title") == expected,
        )
        if entity.get("title") != expected:
            raise RuntimeError(
                f"Block edit did not take effect for {operation['id']}; the "
                "call returned without error but the title is unchanged"
            )
        await self._verify_title_uuid_refs(entity, expected)
        return entity

    async def _candidates_for(
        self, operation: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        Entities a newly added one could be confused with.

        For a block that is its prospective siblings; for a page it is every
        entity sharing the title. Narrowing to siblings is what lets two
        sections each hold a child called "Notes".
        """
        title = operation["data"]["title"]
        if operation["entityType"] != "block":
            return await self._entities_by_title(title)
        siblings = await self._children_of(operation["data"]["page-id"])
        return [s for s in siblings if s.get("title") == title]

    async def _verify_add(
        self, operation: dict[str, Any], before: set[int]
    ) -> dict[str, Any]:
        title = operation["data"]["title"]
        matches = await poll_readback(
            self._client,
            lambda: self._candidates_for(operation),
            lambda values: any(e["id"] not in before for e in values),
        )
        created = [e for e in matches if e["id"] not in before]
        if len(created) != 1:
            scope = ("under the requested parent"
                     if operation["entityType"] == "block" else "in the graph")
            raise RuntimeError(
                f"Expected one new {operation['entityType']} titled {title!r} "
                f"{scope}, found {len(created)}. This API reports success for "
                "writes that do nothing; check the parent UUID."
            )
        entity = created[0]

        if operation["entityType"] == "page":
            if not entity.get("name"):
                raise RuntimeError("Created entity is not a page")
            return entity

        # A block's parent is whatever `page-id` named -- a page or a block.
        parent = await self._entity_by_uuid(operation["data"]["page-id"])
        if self._reference_id(entity.get("parent")) != parent["id"]:
            raise RuntimeError(
                "Created block is not parented to the requested target")
        expected_page = (
            parent["id"] if parent.get("name")
            else self._reference_id(parent.get("page"))
        )
        if self._reference_id(entity.get("page")) != expected_page:
            raise RuntimeError("Created block has the wrong owning page")
        await self._verify_title_uuid_refs(entity, title)
        return entity

    async def _validate_operations(
        self, operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_BATCH_OPERATIONS:
            raise ValueError(
                f"operations must contain 1 to {MAX_BATCH_OPERATIONS} items")
        # (parent uuid, title) for blocks; (None, title) for pages. Two blocks
        # may share a title in one batch as long as they have different
        # parents -- verification looks for each new block among its own
        # parent's children, so siblings are the only ambiguous case.
        add_keys: set[tuple[str | None, str]] = set()
        normalized: list[dict[str, Any]] = []

        for index, operation in enumerate(operations):
            if not isinstance(operation, dict) or not isinstance(
                    operation.get("data"), dict):
                raise ValueError(f"operation {index} must contain a data object")
            op_type = operation.get("operation")
            entity_type = operation.get("entityType")
            data = dict(operation["data"])

            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"operation {index} requires a non-empty title")
            self._validate_title(title)

            if op_type == "add" and entity_type == "page":
                if set(data) != {"title"}:
                    raise ValueError("Page add supports only data.title")
                # A page is a named entity, so the title scope applies here
                # even though it does not apply to block content.
                self._require_title(title)

            elif op_type == "add" and entity_type == "block":
                # Closed allowlist. `tags`, `order` and `parent-id` are all
                # rejected by the API as disallowed keys.
                if set(data) != {"title", "page-id"}:
                    raise ValueError(
                        "Block add supports only data.title and data.page-id")
                parent_uuid = self._validated_uuid(data.get("page-id"))
                # Parent may be a page OR a block -- this is how nesting works.
                await self._entity_by_uuid(parent_uuid)
                data["page-id"] = parent_uuid

            elif op_type == "edit" and entity_type == "block":
                if set(data) != {"title"}:
                    raise ValueError("Block edit supports only data.title")
                block_uuid = self._validated_uuid(operation.get("id"))
                block = await self._entity_by_uuid(block_uuid)
                if block.get("name"):
                    raise ValueError("Block edit id identifies a page")
                normalized.append(dict(operation, data=data, id=block_uuid))
                continue

            else:
                raise ValueError(
                    "Supported operations are add page, add block, and edit "
                    "block. Editing a page, tag or property is not supported "
                    "by Logseq, and there is no removal operation."
                )

            key = (data.get("page-id") if entity_type == "block" else None,
                   title)
            if key in add_keys:
                where = ("under the same parent" if key[0]
                         else "for a page")
                raise ValueError(
                    f"Two operations in this batch add {title!r} {where}; "
                    "verification could not tell the results apart. Titles "
                    "need only be unique among siblings."
                )
            add_keys.add(key)
            normalized.append(dict(operation, data=data))
        return normalized

    # --------------------------------------------------------------- shared

    async def _class_id(self, ident: str) -> int:
        value = await self._client.call(
            "logseq.DB.datascriptQuery",
            [f"[:find ?class . :where [?class :db/ident {ident}]]"])
        if not isinstance(value, int):
            raise RuntimeError(f"Could not resolve the class {ident}")
        return value

    async def _query_list(
        self, query: str, description: str, *params: Any
    ) -> list[Any]:
        result = await self._client.call(
            "logseq.DB.datascriptQuery", [query, *params])
        if result is None:
            return []
        if not isinstance(result, list):
            raise RuntimeError(f"{description} returned an unexpected shape")
        return [r for r in result if r is not None]

    async def _optional_entity_by_uuid(
        self, entity_uuid: str
    ) -> dict[str, Any] | None:
        entity_uuid = self._validated_uuid(entity_uuid)
        query = (
            "[:find (pull ?entity [*]) . :where "
            f"[?entity :block/uuid #uuid \"{entity_uuid}\"]]"
        )
        entity = await self._client.call("logseq.DB.datascriptQuery", [query])
        if entity is None:
            return None
        if not isinstance(entity, dict) or entity.get("uuid") != entity_uuid:
            raise RuntimeError("Entity lookup returned an unexpected result")
        return entity

    async def _entity_by_uuid(self, entity_uuid: str) -> dict[str, Any]:
        entity = await self._optional_entity_by_uuid(entity_uuid)
        if entity is None:
            raise LookupError(
                f"No entity exists with exact UUID {entity_uuid}")
        return entity

    async def _entities_by_title(self, title: str) -> list[dict[str, Any]]:
        query = (
            "[:find [(pull ?entity [*]) ...] :where "
            f"[?entity :block/title {json.dumps(title)}]]"
        )
        entities = await self._query_list(query, "Title lookup")
        return [e for e in entities if isinstance(e, dict)]

    async def _preflight_block(
        self, block_uuid: str, *, role: str
    ) -> dict[str, Any]:
        try:
            block = await self.get_block(block_uuid)
        except LookupError as error:
            raise LookupError(
                f"{role.capitalize()} block does not exist for UUID {block_uuid}"
            ) from error
        except ValueError as error:
            if "page, not a block" in str(error):
                raise ValueError(
                    f"{role.capitalize()} UUID identifies a page, not a block"
                ) from error
            raise

        missing = [
            name for name, ok in (
                ("id", isinstance(block.get("id"), int)),
                ("uuid", block.get("uuid") == block_uuid),
                ("parent", self._reference_id(block.get("parent")) is not None),
                ("page", self._reference_id(block.get("page")) is not None),
            ) if not ok
        ]
        if missing:
            raise RuntimeError(
                f"{role.capitalize()} block is missing required information: "
                f"{', '.join(missing)}"
            )
        return block

    async def _subtree(self, root: dict[str, Any]) -> list[dict[str, Any]]:
        """Read a bounded subtree using immediate-parent relationships."""
        result = [root]
        queue = [root]
        while queue:
            parent = queue.pop(0)
            query = (
                "[:find [(pull ?child [*]) ...] :in $ ?parent :where "
                "[?child :block/parent ?parent]]"
            )
            children = [
                c for c in await self._query_list(
                    query, "Child lookup", parent["id"])
                if isinstance(c, dict)
            ]
            result.extend(children)
            queue.extend(children)
            if len(result) > MAX_SUBTREE_NODES:
                raise RuntimeError(
                    f"Subtree exceeds the {MAX_SUBTREE_NODES}-node limit")
        return result

    @staticmethod
    def _reference_id(value: Any) -> int | None:
        return value.get("id") if isinstance(value, dict) else None

    @staticmethod
    def _reference_ids(references: Any) -> set[int]:
        if not isinstance(references, list):
            return set()
        return {
            r["id"] for r in references
            if isinstance(r, dict) and isinstance(r.get("id"), int)
        }

    async def _verify_title_uuid_refs(
        self, entity: dict[str, Any], title: str
    ) -> None:
        referenced = {
            m.group(1).lower()
            for m in re.finditer(r"\[\[(" + UUID_PATTERN.pattern[2:-2] + r")\]\]",
                                 title)
        }
        if not referenced:
            return
        known = {
            r.get("uuid", "").lower()
            for r in entity.get("refs", [])
            if isinstance(r, dict) and r.get("uuid")
        }
        query = "[:find ?uuid . :in $ ?entity :where [?entity :block/uuid ?uuid]]"
        for reference_id in self._reference_ids(entity.get("refs", [])):
            value = await self._client.call(
                "logseq.DB.datascriptQuery", [query, reference_id])
            if isinstance(value, str):
                known.add(value.lower())
        if not referenced <= known:
            raise RuntimeError("Block title UUID reference verification failed")


def _parse_outline(text: str) -> list[tuple[tuple[int, ...], str]]:
    """
    Parse an indented outline into (path, title) pairs.

    Indent width is taken from the first indented line, so 2-space and 4-space
    outlines both work provided they are internally consistent.
    """
    lines = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        expanded = raw.replace("\t", "    ")
        lines.append((lineno,
                      len(expanded) - len(expanded.lstrip(" ")),
                      raw.strip()))
    if not lines:
        return []

    unit = next((i for _, i, _ in lines if i > 0), 0) or 1
    entries: list[tuple[tuple[int, ...], str]] = []
    counts: dict[tuple[int, ...], int] = {}
    last_at_depth: dict[int, tuple[int, ...]] = {}

    for lineno, indent, title in lines:
        if indent % unit:
            raise ValueError(
                f"Line {lineno} indent ({indent}) is not a multiple of {unit}")
        depth = indent // unit
        if depth and depth - 1 not in last_at_depth:
            raise ValueError(
                f"Line {lineno} indents more than one level at once")
        parent = last_at_depth[depth - 1] if depth else ()
        index = counts.get(parent, 0)
        counts[parent] = index + 1
        path = parent + (index,)
        last_at_depth[depth] = path
        for deeper in [d for d in last_at_depth if d > depth]:
            del last_at_depth[deeper]
        entries.append((path, title))
    return entries

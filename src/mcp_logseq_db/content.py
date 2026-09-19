"""Verified page and block operations.

ROUTES
------
Block creation goes through `logseq.DB.insertBlock` and `insertBatchBlock`,
not `upsertNodes`. `upsertNodes` writes its single `page-id` into BOTH
`:block/parent` and `:block/page`, so a block parent produced a child whose
owning page was the parent block. `insertBlock` sets the two independently and
returns the created entity, which also removes the read-back cycle that
outline building otherwise needs.

`move_block` routes through `logseq.DB.moveBlock`. Three behaviours matter: it
no-ops when the position would not change, `placement=child` prepends, and a
move carries the subtree with the descendants' page following.

Page edits through `upsertNodes` are not possible: `edit` + `page` returns
"Editing a page, tag or property isn't supported yet" from Logseq itself.

A NOTE ON :block/page
---------------------
On some graphs a block's `:block/page` points at an ancestor block rather than
at the page. This is NOT damage. Logseq renders the outline from
`:block/parent`, so those blocks display normally; only a query written
against `:block/page` misses them. `find_orphans` and the `true_orphans` count
in `page_stats` report the condition to explain a surprising query result --
there is nothing to repair, and moving such blocks rewrites their order for no
benefit. A repair tool built on the opposite assumption was removed after
roughly 1,500 blocks had been moved pointlessly.

Reads therefore walk `:block/parent` rather than `:block/page`, so they see
every block regardless of which attribute disagrees.

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

        Primary lookup is `logseq.DB.getPage`, which accepts a name OR a UUID
        and, given a title held by both a page and a tag, returns the page.
        Its result is checked twice before being trusted:

          - it returns RECYCLED pages, carrying
            `:logseq.property/deleted-at` and parented under Recycle. Handing
            one back would resolve a title to a page the user deleted, and the
            split-identity repair depends on recycled pages NOT resolving.
          - it must be Page-classed, so a title held only by a tag resolves to
            nothing rather than to the tag.

        KNOWN TRADE-OFF: `getPage` returns one entity, so when it succeeds
        this does NOT detect two live pages sharing a title -- it resolves to
        whichever Logseq picked. That guard survives only on the fallback
        path. It is accepted because `createPage` is idempotent on title and
        refuses a taken one, so duplicates can no longer be created through
        this server; an older graph may still contain them. Ambiguity is
        refused rather than guessed at wherever it IS seen, because selecting
        a write target from a fuzzy match is how the wrong entity gets
        modified.
        """
        self._validate_title(title)
        page_class = await self._class_id(PAGE_CLASS)

        # Fast path: one call, name or UUID.
        direct = await self._client.call("logseq.DB.getPage", [title])
        if isinstance(direct, dict) and direct.get("name"):
            recycled = direct.get(":logseq.property/deleted-at") is not None
            is_page = any(self._reference_id(t) == page_class
                          or t == page_class
                          for t in (direct.get("tags") or []))
            if is_page and not recycled:
                return {"found": True, "title": title,
                        "page_uuid": direct.get("uuid")}

        # Fall back to the query, which can see every match and so can report
        # ambiguity. Reached when the fast path found nothing, found a
        # recycled page, or found something that is not Page-classed.
        query = (
            "[:find [(pull ?page [:db/id :block/uuid :block/name :block/title "
            ":logseq.property/deleted-at]) ...] :in $ ?class :where "
            "[?page :block/tags ?class] "
            f"[?page :block/title {json.dumps(title)}]]"
        )
        pages = await self._query_list(
            query, "Page title lookup", page_class)

        if not pages:
            # The normalized name is lowercased. Without this the exact string
            # Logseq stores in :block/name fails to resolve the page it names.
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

        # Structural attributes are excluded in Python rather than in the
        # query. :block/parent, :block/page and :block/order are themselves
        # Property-class entities, so the class filter does not remove them --
        # but Datascript has no clean negation for a string predicate, and
        # predicates have wedged the DB worker before.
        parsed = [row for row in rows
                  if isinstance(row, list) and len(row) == 3
                  and not _is_structural(row[0])]
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

        Named for the tool it backs. Walks `:block/parent` rather than
        `:block/page`: on some graphs a block's `:block/page` points at an
        ancestor block, and a page-scoped query misses it even though Logseq
        displays it normally. Parent traversal matches what the UI shows.
        """
        page_uuid = self._validated_uuid(page_uuid)
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")

        blocks = await self._descendants_by_parent(page_uuid)
        # Fractional-index strings sort lexicographically into document order.
        blocks.sort(key=lambda b: str(b.get("order", "")))
        return blocks

    async def _descendants_by_parent(
        self, root_uuid: str
    ) -> list[dict[str, Any]]:
        """
        Every descendant, found by walking :block/parent.

        Deliberately not `[?b :block/page ?page]`. On some graphs a block's
        :block/page points at an ancestor block, and such a block is missed by
        a page-scoped query even though Logseq displays it normally. Walking
        :block/parent matches what the UI shows.
        """
        # The pull pattern must name the attributes it wants. `...` recurses
        # the WHOLE pattern, so listing them once gives them at every level;
        # a pattern of only `{:block/_parent ...}` returns nodes carrying no
        # id, uuid or title, which is useless to every caller and reads as an
        # empty page.
        query = (
            "[:find (pull ?root [:db/id :block/uuid :block/title :block/name "
            ":block/order "
            "{:block/parent [:db/id :block/uuid]} "
            "{:block/page [:db/id :block/uuid]} "
            "{:block/_parent ...}]) . :where "
            f"[?root :block/uuid #uuid \"{root_uuid}\"]]"
        )
        root = await self._client.call(
            "logseq.DB.datascriptQuery", [query])
        if not isinstance(root, dict):
            return []

        flat: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            for child in node.get("_parent", []) or []:
                if not isinstance(child, dict):
                    continue
                flat.append(child)
                walk(child)

        walk(root)
        # Strip the raw reverse-pull key. Callers get one authoritative view
        # of structure rather than the same subtree repeated under two keys.
        return [{k: v for k, v in node.items() if k != "_parent"}
                for node in flat]

    async def _classify_subtree(
        self, page_uuid: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Walk the tree tracking which page each node SHOULD belong to.

        A nested page is a legitimate page boundary: blocks beneath it
        correctly carry the sub-page as their :block/page, not the root. The
        earlier version assumed a single boundary and so flagged every block
        under a nested page as damage -- on a container page that meant
        hundreds of correct blocks reported as corruption, with a diagnostic
        asserting a cause.

        Returns (own_blocks, nested_pages, true_orphans).
        """
        root = await self._entity_by_uuid(page_uuid)
        query = (
            "[:find (pull ?root [:db/id :block/uuid :block/title :block/name "
            "{:block/page [:db/id]} {:block/_parent ...}]) . :where "
            f"[?root :block/uuid #uuid \"{page_uuid}\"]]"
        )
        tree = await self._client.call("logseq.DB.datascriptQuery", [query])
        if not isinstance(tree, dict):
            return [], [], []

        own: list[dict[str, Any]] = []
        nested: list[dict[str, Any]] = []
        orphans: list[dict[str, Any]] = []

        def walk(node: dict[str, Any], expected_page: int) -> None:
            for child in node.get("_parent", []) or []:
                if not isinstance(child, dict):
                    continue
                entry = {k: v for k, v in child.items() if k != "_parent"}
                if child.get("name"):
                    # A page. Its own subtree is measured against itself.
                    nested.append(entry)
                    walk(child, child["id"])
                    continue
                if self._reference_id(child.get("page")) != expected_page:
                    orphans.append(entry)
                else:
                    own.append(entry)
                walk(child, expected_page)

        walk(tree, root["id"])
        return own, nested, orphans

    async def find_orphans(self, page_uuid: str) -> dict[str, Any]:
        """
        Blocks whose :block/parent and :block/page disagree with no page
        boundary between them.

        Nested pages are reported separately as structure. Only a block whose
        owning page differs from its nearest ancestor page is damage -- that
        is a real child no page-scoped query can see.
        """
        page_uuid = self._validated_uuid(page_uuid)
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")

        own, nested, orphans = await self._classify_subtree(page_uuid)

        if orphans:
            diagnostic = (
                f"{len(orphans)} block(s) have an owning page that differs "
                "from their nearest ancestor page. This is NOT damage: Logseq "
                "renders the outline from :block/parent, so they display "
                "normally in the UI. Only a query written against "
                ":block/page misses them, which is what this count explains. "
                "Do not move them to 'correct' it -- a move rewrites their "
                "order for no benefit."
            )
        elif nested:
            diagnostic = (
                f"No damage. {len(nested)} nested page(s) were found; blocks "
                "beneath them correctly belong to those pages rather than to "
                "this one, which is ordinary structure."
            )
        else:
            diagnostic = "No damage. Every block belongs to this page."

        return {
            "page_uuid": page_uuid,
            "own_blocks": len(own),
            "nested_pages": nested,
            "orphans": orphans,
            "diagnostic": diagnostic,
        }

    async def page_stats(self, page_uuid: str) -> dict[str, Any]:
        """
        Counts only, for triage across many pages.

        Every other read returns payload proportional to page size, so asking
        "is this page empty, does anything point at it" cost an unbounded
        response -- one container page returned 216 full blocks to communicate
        two integers. This returns integers only.

        `true_orphans` counts blocks whose :block/page differs from their
        nearest ancestor page. That is informational, NOT a fault -- see the
        module docstring.
        """
        page_uuid = self._validated_uuid(page_uuid)
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")
        page_id = page["id"]

        own, nested, orphans = await self._classify_subtree(page_uuid)

        # Counted in the query rather than returned and measured, so the
        # response size does not depend on the reference count.
        async def count(query: str, *params: Any) -> int:
            value = await self._client.call(
                "logseq.DB.datascriptQuery", [query, *params])
            return value if isinstance(value, int) else 0

        by_page = await count(
            "[:find (count ?b) . :in $ ?page :where "
            "[?b :block/page ?page]]", page_id)
        # createPage seeds every new page with one empty block, and Logseq
        # leaves a trailing empty block behind ordinary editing, so own_blocks
        # overstates content by however many of those exist. Counted
        # separately rather than subtracted silently: an empty block the user
        # typed is indistinguishable from a seed one, and hiding the
        # difference would replace a known overcount with an invisible
        # undercount. Matches :block/title "" only -- a block carrying no
        # title attribute at all is not counted here.
        empty = await count(
            "[:find (count ?b) . :in $ ?page :where "
            "[?b :block/page ?page] [?b :block/title \"\"]]", page_id)
        refs = await count(
            "[:find (count ?e) . :in $ ?target :where "
            "[?e :block/refs ?target]]", page_id)
        tag_holders = await count(
            "[:find (count ?e) . :in $ ?target :where "
            "[?e :block/tags ?target]]", page_id)
        # Counted as (holder, property) pairs so the figure agrees with
        # findBacklinks, which lists one row per pair. Counting distinct
        # holders made the two disagree whenever a block held the target
        # under more than one property.
        # Counted from the rows rather than with (count …), because the
        # structural attributes have to be filtered out and Datascript cannot
        # negate a string predicate cleanly. One row per property value, so
        # the payload stays small.
        value_rows = await self._query_list(
            "[:find (pull ?prop [:db/ident]) ?e :in $ ?target ?class :where "
            "[?prop :block/tags ?class] [?prop :db/ident ?attr] "
            "[?e ?attr ?target]]",
            "Property reference count", page_id,
            await self._class_id(PROPERTY_CLASS))
        property_values = sum(
            1 for row in value_rows
            if isinstance(row, list) and len(row) == 2
            and not _is_structural(row[0]))

        return {
            "page_uuid": page_uuid,
            "title": page.get("title"),
            "own_blocks": by_page,
            "empty_blocks": empty,
            "content_blocks": by_page - empty,
            "subtree_blocks": len(own) + len(nested) + len(orphans),
            "nested_pages": len(nested),
            "true_orphans": len(orphans),
            "refs": refs,
            "tag_holders": tag_holders,
            "property_values": property_values,
            "diagnostic": (
                f"{by_page - empty} block(s) with content, {by_page} own "
                f"block(s) including {empty} empty, {len(nested)} nested "
                f"page(s), "
                f"{refs + tag_holders + property_values} inbound reference(s)"
                + (f", {len(orphans)} ORPHANED block(s)" if orphans else "")
                + (". content_blocks is the figure to pair with the "
                   "reference count when judging whether a page is empty; "
                   "own_blocks counts the empty block createPage seeds and "
                   "so is never 0 on a page that was created through this "
                   "API." if empty else "")),
        }

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
        # Walk :block/parent rather than scoping to :block/page. A child whose
        # :block/page is wrong is a real child and must appear in the tree;
        # the page-scoped form reported `children: []` over exactly those.
        descendants = await self._descendants_by_parent(block_uuid)
        if descendants and not any(
                isinstance(e.get("id"), int) for e in descendants):
            raise RuntimeError(
                "Subtree traversal returned nodes with no :db/id. The pull "
                "pattern is not requesting attributes -- this would otherwise "
                "surface as an empty page rather than an error.")

        by_parent: dict[int, list[dict[str, Any]]] = {}
        for entity in descendants:
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
            result = {k: v for k, v in entity.items() if k != "_parent"}
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

    @serialized_write
    async def create_page(
        self, title: str, *, dry_run: bool = False
    ) -> ContentResult:
        """
        Create one page.

        Routed through `logseq.DB.createPage`, not `upsertNodes`. Two reasons:

          - `upsertNodes` fails on synced graphs. It returns "The Imported EDN
            has 3 validation error(s)" for a page add that the dry run accepts,
            while `createPage`, `insertBlock` and `createTag` all write to the
            same graph without complaint. Local graphs are unaffected, which is
            why this went unnoticed.
          - `createPage` is idempotent on title: calling it twice returns the
            same entity rather than creating a duplicate.

        The duplicate check below therefore reports a clearer error than
        Logseq would, but is no longer load-bearing.

        NOTE the second argument of `logseq.DB.createPage` is a PROPERTIES map,
        not options. Passing `{"dry-run": true}` creates the page anyway AND
        mints a `dry-run` property in the caller's namespace. So there is no
        server-side dry run here, and `dry_run` below validates locally only.
        """
        self._require_title(title)
        existing = await self._entities_by_title(title)
        if existing:
            kinds = ", ".join(
                "page" if e.get("name") else "block" for e in existing)
            raise ValueError(
                f"An entity titled {title!r} already exists ({kinds}). Pages, "
                "tags and blocks share a title space.")

        if dry_run:
            return ContentResult(
                validation={"title": title, "checked": "locally"},
                response=None, verified_entities=(), verified=False,
                diagnostic=(
                    "Dry run: nothing was written, so verified is false by "
                    "design. This checks the title locally -- createPage has "
                    "no server-side dry run, because its second argument is a "
                    "properties map rather than options."))

        response: Any = None
        timed_out = False
        try:
            response = await self._client.call(
                "logseq.DB.createPage", [title])
        except httpx.TimeoutException:
            timed_out = True

        created = self._created_uuid(response)
        page = None
        if created:
            page = await poll_readback(
                self._client,
                lambda: self._optional_entity_by_uuid(created),
                lambda value: value is not None)
        if page is None:
            # No usable identity came back; fall back to resolving the title.
            match = [e for e in await self._entities_by_title(title)
                     if e.get("name")]
            page = match[0] if match else None

        if page is None or not page.get("name"):
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    f"No page titled {title!r} is present after the write."))

        return ContentResult(
            validation=None, response=response, verified_entities=(page,),
            recovered_after_timeout=timed_out,
            diagnostic=(
                "createPage creates the page with one empty block; that block "
                "is counted by any later block read."))

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
        :logseq.property/deleted-at, and is reparented under a Recycle page.
        Inbound references are NOT rewritten, so anything linking to it keeps
        pointing at a page that no longer appears in listings -- which is why
        references are surfaced and require acknowledgement.

        The route accepts the page UUID. The name fallback below is retained
        because it costs one call only when the UUID form does nothing, and
        this API does nothing silently.
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

        # Property values are materialized as blocks on the holder's page, so
        # an unfiltered "delete every block" destroys them -- contradicting
        # this tool's contract that property values survive. A value block
        # carries :logseq.property/created-from-property.
        value_block_ids = await self._property_value_block_ids(page["id"])
        all_top_level = await self._children_of(page_uuid)
        top_level = [b for b in all_top_level
                     if b.get("id") not in value_block_ids]
        preserved = len(all_top_level) - len(top_level)

        if not top_level:
            return ContentResult(
                validation=None, response=None, verified_entities=(),
                diagnostic=(
                    "The page has no content blocks to remove"
                    + (f"; {preserved} property value block(s) left in place"
                       if preserved else "")),
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

        # Only content blocks should be gone. Property value blocks are
        # expected to remain, so the predicate cannot simply be "no blocks" --
        # that was why the read-back reported a state that did not hold.
        async def content_blocks_left() -> list[dict[str, Any]]:
            current = await self.get_block_uuid(page_uuid)
            keep = await self._property_value_block_ids(page["id"])
            return [b for b in current if b.get("id") not in keep]

        remaining = await poll_readback(
            self._client, content_blocks_left, lambda blocks: not blocks)

        if remaining:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=f"{len(remaining)} block(s) remain on the page",
                previous_entities=tuple(before),
                observed_entities=tuple(remaining))

        # Re-read the page so verified_entities reflects the post-write state
        # rather than the snapshot taken before it.
        current_page = await self._optional_entity_by_uuid(page_uuid) or page
        return ContentResult(
            validation=None, response=response,
            verified_entities=(current_page,),
            recovered_after_timeout=timed_out,
            diagnostic=(
                f"Removed {len(top_level)} top-level block(s)"
                + (f"; preserved {preserved} property value block(s)"
                   if preserved else "")),
            previous_entities=tuple(before))

    async def _page_by_uuid(self, page_uuid: str) -> dict[str, Any]:
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")
        return page

    async def _property_value_block_ids(self, page_id: int) -> set[int]:
        """
        Blocks on this page that exist only to hold a property value.

        Logseq materializes reference-typed property values as blocks, so they
        appear in any page-scoped block query. Deleting them silently discards
        the property values they carry.
        """
        query = (
            "[:find [?block ...] :in $ ?page :where "
            "[?block :block/page ?page] "
            "[?block :logseq.property/created-from-property _]]"
        )
        found = await self._query_list(
            query, "Property value block lookup", page_id)
        return {value for value in found if isinstance(value, int)}

    async def find_backlinks(self, target_uuid: str) -> dict[str, Any]:
        """
        Everything referring to an entity, by any mechanism.

        `deletePage` already computes this internally to decide whether it
        needs an acknowledgement, but nothing exposed it -- so "what links
        here" was unanswerable, and the reference count was only visible as a
        side effect of trying to delete something.

        Three mechanisms are reported separately because they behave
        differently:

          refs   -- :block/refs, what Logseq counts as a backlink
          tags   -- :block/tags, present when the target is a tag
          values -- a property whose value points at the target

        A property value is a reference in the DB but does not appear in the
        UI's backlink panel, so a caller auditing "what depends on this" needs
        all three and a caller reproducing the UI needs only the first.
        """
        target_uuid = self._validated_uuid(target_uuid)
        target = await self._entity_by_uuid(target_uuid)
        target_id = target["id"]

        holder = (
            "[:db/id :block/uuid :block/title :block/name "
            "{:block/page [:db/id :block/uuid :block/title]}]"
        )

        refs = await self._query_list(
            f"[:find [(pull ?e {holder}) ...] :in $ ?target :where "
            "[?e :block/refs ?target]]",
            "Backlink lookup", target_id)

        tagged = await self._query_list(
            f"[:find [(pull ?e {holder}) ...] :in $ ?target :where "
            "[?e :block/tags ?target]]",
            "Tag holder lookup", target_id)

        # Property values pointing at the target. The attribute varies per
        # property, so the property entity is joined in and its ident used as
        # the attribute -- the same variable-attribute form the property
        # readers use.
        property_class = await self._class_id(PROPERTY_CLASS)
        valued = await self._query_list(
            "[:find (pull ?e " + holder + ") "
            "(pull ?prop [:db/id :db/ident :block/title]) "
            ":in $ ?target ?class :where "
            "[?prop :block/tags ?class] [?prop :db/ident ?attr] "
            "[?e ?attr ?target]]",
            "Property reference lookup", target_id, property_class)

        by_property = [
            {"holder": row[0], "property": row[1]}
            for row in valued if isinstance(row, list) and len(row) == 2
        ]

        total = len(refs) + len(tagged) + len(by_property)
        return {
            "target_uuid": target_uuid,
            "target": target,
            "total": total,
            "refs": refs,
            "tagged": tagged,
            "property_values": by_property,
            "diagnostic": (
                f"{len(refs)} reference(s), {len(tagged)} tag holder(s), "
                f"{len(by_property)} property value(s). Deleting or recycling "
                "the target does not rewrite any of them."
                if total else "Nothing refers to this entity."),
        }

    async def _inbound_references(self, page_id: int) -> list[dict[str, Any]]:
        query = (
            "[:find [(pull ?entity [:db/id :block/uuid :block/title "
            ":block/name {:block/page [:db/id :block/title]}]) ...] "
            ":in $ ?page :where [?entity :block/refs ?page]]"
        )
        return await self._query_list(query, "Inbound reference lookup", page_id)

    # ---------------------------------------------------------- block writes

    @serialized_write
    async def create_block(
        self,
        parent_uuid: str,
        title: str,
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """
        Create one block under a page or another block.

        Routed through `insertBlock`, which sets :block/parent and :block/page
        independently. `upsertNodes` cannot: its `page-id` is written to both,
        so a block parent produced a child whose owning page was the parent
        block -- a real child, invisible to every page-scoped query.

        `insertBlock` also returns the created entity, so the UUID Logseq
        assigned is known without a follow-up read.
        """
        parent_uuid = self._require_entity(self._validated_uuid(parent_uuid))
        self._validate_title(title)
        parent = await self._entity_by_uuid(parent_uuid)

        if dry_run:
            return ContentResult(
                validation={"parent": parent, "title": title},
                response=None, verified_entities=(), verified=False,
                diagnostic=(
                    "Dry run: nothing was written, so verified is false by "
                    "design. The parent exists and the title is usable."))

        response: Any = None
        timed_out = False
        try:
            # sibling: false means "child of the target" rather than "next to
            # it". The target may be a page or a block.
            response = await self._client.call(
                "logseq.DB.insertBlock",
                [parent_uuid, title, {"sibling": False}])
        except httpx.TimeoutException:
            timed_out = True

        created = self._created_uuid(response)
        if created is None:
            # No usable identity came back, so fall back to finding it among
            # the parent's children.
            match = [c for c in await self._children_of(parent_uuid)
                     if c.get("title") == title]
            created = match[-1].get("uuid") if match else None
        if created is None:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic="The block was not observed under the requested parent",
                previous_entities=(parent,))

        return await self._verify_created(
            created, parent, response, timed_out)

    async def _verify_created(
        self, block_uuid: str, parent: dict[str, Any],
        response: Any, timed_out: bool,
    ) -> ContentResult:
        """
        Confirm the new block's parent AND owning page.

        Checking the parent alone is what let the ownership bug go unnoticed:
        the block appeared under the right parent while belonging to the wrong
        page.
        """
        block = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(block_uuid),
            lambda value: value is not None)
        if block is None:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic="The created block could not be read back",
                previous_entities=(parent,))

        expected_page = (parent["id"] if parent.get("name")
                         else self._reference_id(parent.get("page")))
        actual_parent = self._reference_id(block.get("parent"))
        actual_page = self._reference_id(block.get("page"))

        if actual_parent != parent["id"]:
            return ContentResult(
                validation=None, response=response,
                verified_entities=(block,),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic="The block was created under the wrong parent",
                previous_entities=(parent,), observed_entities=(block,))
        if actual_page != expected_page:
            return ContentResult(
                validation=None, response=response,
                verified_entities=(block,),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    "The block's owning page is wrong. It is a real child but "
                    "belongs to the wrong page, so it is invisible to every "
                    "page-scoped query -- run findOrphans and remove it."),
                previous_entities=(parent,), observed_entities=(block,))

        return ContentResult(
            validation=None, response=response, verified_entities=(block,),
            recovered_after_timeout=timed_out, previous_entities=(parent,))

    @staticmethod
    def _created_uuid(response: Any) -> str | None:
        """Pull a block UUID out of an insert response, which returns the
        created entity rather than nothing."""
        if isinstance(response, list):
            response = response[0] if response else None
        if isinstance(response, dict):
            value = response.get("uuid")
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _created_uuids(response: Any) -> list[str]:
        entries = response if isinstance(response, list) else [response]
        return [e["uuid"] for e in entries
                if isinstance(e, dict) and isinstance(e.get("uuid"), str)]

    @serialized_write
    async def update_block(
        self,
        block_uuid: str,
        title: str,
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """
        Edit one existing block title.

        Routed through `logseq.DB.updateBlock` rather than `upsertNodes`.
        `upsertNodes` fails outright on synced graphs -- it returns "The
        Imported EDN has N validation error(s)" for writes the dry run
        accepts, while every other route succeeds on the same graph.

        `updateBlock` does NOT guard against a page UUID: given one it
        rewrites the page's own title. The block check below is the only thing
        preventing that.

        Verification checks the title CHANGED rather than that it matches what
        was sent. Logseq parses content on write, so the stored title need not
        equal the request: `[[X]]` is rewritten to `[[uuid]]` and a markdown
        heading loses its marker.
        """
        block_uuid = self._require_entity(self._validated_uuid(block_uuid))
        self._validate_title(title)

        previous = await self._entity_by_uuid(block_uuid)
        if previous.get("name"):
            raise ValueError(
                "UUID identifies a page, not a block. updateBlock would "
                "rewrite the page's title; use renamePage instead.")

        if dry_run:
            return ContentResult(
                validation={"block": previous, "title": title},
                response=None, verified_entities=(), verified=False,
                diagnostic=(
                    "Dry run: nothing was written, so verified is false by "
                    "design. The block exists and the title is usable."),
                previous_entities=(previous,))

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.updateBlock", [block_uuid, title])

        unchanged = previous.get("title")
        current = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(block_uuid),
            lambda value: value is not None and value.get("title") != unchanged,
        )
        if current is None:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic="The block disappeared during the edit",
                previous_entities=(previous,))
        if current.get("title") == unchanged and title != unchanged:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    "The edit was not observed; the block still has its "
                    "original title. This API returns success for writes that "
                    "do nothing."),
                previous_entities=(previous,), observed_entities=(current,))

        return ContentResult(
            validation=None, response=response, verified_entities=(current,),
            recovered_after_timeout=timed_out,
            previous_entities=(previous,))

    @serialized_write
    async def move_block(
        self,
        block_uuid: str,
        target_uuid: str,
        *,
        placement: str = "child",
    ) -> ContentResult:
        """
        Move a block, and its subtree, relative to a target.

        `moveBlock` returns null whether it moved the block or did nothing, so
        the outcome is established by reading afterwards rather than from the
        response.

        Three things are checked, because a move can go wrong in three ways:
        the parent may not change; the owning page may not follow the block to
        a new page; and descendants may be left behind pointing at the old
        page. The last two are invisible to page-scoped queries, which is the
        same failure that made malformed nested writes undetectable.
        """
        block_uuid = self._require_entity(self._validated_uuid(block_uuid))
        target_uuid = self._validated_uuid(target_uuid)
        if placement not in {"child", "before", "after"}:
            raise ValueError("placement must be child, before, or after")

        block = await self._preflight_block(block_uuid, role="source")
        target = await self._entity_by_uuid(target_uuid)
        if block["id"] == target["id"]:
            raise ValueError("A block cannot be moved relative to itself")
        if placement != "child" and target.get("name"):
            raise ValueError(
                "A page has no siblings; use placement=child to move a block "
                "to the top level of a page")

        # Moving a block beneath its own descendant would detach the subtree
        # from the tree entirely.
        subtree = await self._descendants_by_parent(block_uuid)
        if any(d.get("id") == target["id"] for d in subtree):
            raise ValueError(
                "The target is inside the block's own subtree; moving there "
                "would detach it from the graph")

        expected_parent = (target["id"] if placement == "child"
                           else self._reference_id(target.get("parent")))
        expected_page = (target["id"] if target.get("name")
                         else self._reference_id(target.get("page")))
        if expected_parent is None or expected_page is None:
            raise RuntimeError(
                "The target is missing the parent or page reference needed to "
                "place a block relative to it")

        options = ({"children": True} if placement == "child"
                   else {"before": placement == "before"})
        response, timed_out = await self._call_ambiguous(
            "logseq.DB.moveBlock", [block_uuid, target_uuid, options])

        moved = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(block_uuid),
            lambda value: (
                value is not None
                and self._reference_id(value.get("parent")) == expected_parent
                and self._reference_id(value.get("page")) == expected_page),
        )
        if moved is None:
            return ContentResult(
                validation=None, response=response, verified_entities=(),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic="The block disappeared during the move",
                previous_entities=(block,))

        actual_parent = self._reference_id(moved.get("parent"))
        actual_page = self._reference_id(moved.get("page"))

        if actual_parent != expected_parent:
            return ContentResult(
                validation=None, response=response, verified_entities=(moved,),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    "The move was not observed; the block still has its "
                    "original parent. moveBlock returns null whether or not it "
                    "did anything, so this is what a silent no-op looks like."),
                previous_entities=(block,), observed_entities=(moved,))

        if actual_page != expected_page:
            return ContentResult(
                validation=None, response=response, verified_entities=(moved,),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    "The block moved but its owning page did not follow. It is "
                    "now a real child of the target while belonging to another "
                    "page, so no page-scoped query can see it. Run findOrphans "
                    "on both pages."),
                previous_entities=(block,), observed_entities=(moved,))

        # Descendants must have come along. A subtree left pointing at the old
        # page is the same invisible-orphan failure, one level down.
        stranded = [
            d for d in await self._descendants_by_parent(block_uuid)
            if self._reference_id(d.get("page")) != expected_page
        ]
        if stranded:
            return ContentResult(
                validation=None, response=response, verified_entities=(moved,),
                recovered_after_timeout=timed_out, verified=False,
                diagnostic=(
                    f"The block moved but {len(stranded)} descendant(s) still "
                    "belong to the old page. They are invisible to page-scoped "
                    "queries until repaired."),
                previous_entities=(block,), observed_entities=tuple(stranded))

        return ContentResult(
            validation=None, response=response, verified_entities=(moved,),
            recovered_after_timeout=timed_out, previous_entities=(block,))

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

        Costs one call per parent that has children. `insertBatchBlock`
        returns the entities it created, so a parent's UUID is known before
        its own children are inserted -- there is no read-back cycle, and
        duplicate titles among siblings are fine because nothing has to
        identify a new block by its title.

        Structure comes from INDENTATION ONLY. A leading markdown bullet is
        stripped; any other prefix becomes part of the title.

        Not atomic. A multi-level outline is several calls, so a failure
        partway leaves earlier levels committed; the error names the level
        that stopped.
        """
        page_uuid = self._require_entity(self._validated_uuid(page_uuid))
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise ValueError("UUID identifies a block, not a page")

        entries = _parse_outline(outline)
        if not entries:
            raise ValueError("Outline is empty")

        depth_max = max(len(path) for path, _ in entries) - 1
        # One call per parent that has children. The read-back cycle is gone:
        # insertBatchBlock returns the created entities, so each parent's UUID
        # is known before its own children are inserted.
        parent_count = len({path[:-1] for path, _ in entries})
        if dry_run:
            return {
                "dry_run": True,
                "levels": depth_max + 1,
                "block_count": len(entries),
                "estimated_calls": parent_count,
            }

        parents: dict[tuple[int, ...], str] = {(): page_uuid}
        created: list[dict[str, Any]] = []

        for depth in range(depth_max + 1):
            level = [(p, t) for p, t in entries if len(p) == depth + 1]
            if not level:
                continue
            # Group by parent so siblings go in one call, preserving the order
            # they were written in.
            by_parent: dict[tuple[int, ...], list[tuple[tuple[int, ...], str]]] = {}
            for path, title in level:
                by_parent.setdefault(path[:-1], []).append((path, title))

            for parent_path, items in by_parent.items():
                parent_uuid = parents[parent_path]
                parent = await self._entity_by_uuid(parent_uuid)
                response = await self._client.call(
                    "logseq.DB.insertBatchBlock",
                    [parent_uuid,
                     [{"content": title} for _, title in items],
                     {"sibling": False}])

                uuids = self._created_uuids(response)
                if len(uuids) != len(items):
                    raise RuntimeError(
                        f"Outline level {depth} under {parent_uuid} returned "
                        f"{len(uuids)} blocks for {len(items)} requested; the "
                        "tree is partially built and needs findOrphans.")

                for (path, title), block_uuid in zip(items, uuids):
                    result = await self._verify_created(
                        block_uuid, parent, response, False)
                    if not result.verified:
                        raise RuntimeError(
                            f"{title!r} was created but {result.diagnostic}")
                    parents[path] = block_uuid
                    created.extend(result.verified_entities)

        return {
            "verified": True,
            "page_uuid": page_uuid,
            "levels": depth_max + 1,
            "calls": parent_count,
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
            hint = ("check the parent UUID"
                    if operation["entityType"] == "block"
                    else "check whether the title is already taken")
            raise RuntimeError(
                f"Expected one new {operation['entityType']} titled {title!r} "
                f"{scope}, found {len(created)}. This API reports success for "
                f"writes that do nothing; {hint}."
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


# Node-structure attributes that are also Property-class entities, so a filter
# on the Property class does not exclude them. Named explicitly rather than
# matched by shape: the API strips `:block/` and `:db/` when serialising, so
# these arrive bare -- but so do `tags` and `alias`, which ARE real properties
# a user can set, and `alias` points at pages so it is a reference worth
# counting. Excluding every bare ident would discard both.
STRUCTURAL_IDENTS = frozenset({
    "parent", "page", "order", "title", "name", "uuid", "ident",
    "content", "full-title", "raw-title", "refs", "path-refs",
    "tx-id", "created-at", "updated-at", "format", "collapsed?",
    "journal-day", "journal?", "left",
})


def _is_structural(prop: Any) -> bool:
    """
    Is this property entity a node attribute rather than a real property?

    :block/parent, :block/page, :block/order and :block/title are all
    Property-class entities, so filtering on the Property class does not
    exclude them. Counting them as property values made a page with none
    report 29 -- one per block's `page`, plus one per top-level block's
    `parent`.
    """
    if not isinstance(prop, dict):
        return False
    ident = prop.get("ident") or prop.get(":db/ident")
    if not isinstance(ident, str):
        return False
    bare = ident.lstrip(":")
    if bare.startswith("block/") or bare.startswith("db/"):
        return True
    return "/" not in bare and bare in STRUCTURAL_IDENTS


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
        indent = len(expanded) - len(expanded.lstrip(" "))
        title = raw.strip()
        # Strip a leading markdown bullet. Structure comes from indentation
        # alone, so a bullet is decoration -- but keeping it produced blocks
        # literally titled "- A", which is silent and looks correct.
        stripped = re.sub(r"^[-*+]\s+", "", title)
        if stripped:
            title = stripped
        lines.append((lineno, indent, title))
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

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

from ._shared import VerifiedWriteHelpers, entity_digest, entity_digests
from .client import LogseqDBClient, poll_readback, serialized_write

MAX_SUBTREE_NODES = 1000

# Pages given counts in one `list_pages(with_counts=True)` call. The query
# cost is flat -- four calls whatever the graph size -- but four numbers per
# page is not, and a listing that quietly grew to thousands of rows would
# reintroduce the cost counts were added to remove.
MAX_COUNTED_ROWS = 500

# Blocks moved per moveBlocks call. Two API calls per block -- the move and
# its read-back -- plus a fixed handful, so 50 is a few seconds. 115 in one
# call would risk the client timing out MID-LIST, which is the one outcome
# worse than not starting: a partially moved chapter with no report of where
# it stopped.
MAX_MOVE_BATCH = 50

# Levels the stranded-descendant sweep will walk. One query per level, so
# this bounds a pathological chain rather than the ordinary case.
MAX_SUBTREE_DEPTH = 50

# Resolved to a :db/id at call time. Integer ids are renumbered when a graph is
# rebuilt, so nothing here hardcodes them.
PROPERTY_CLASS = ":logseq.class/Property"
PAGE_CLASS = ":logseq.class/Page"
TAG_CLASS = ":logseq.class/Tag"

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

    def to_dict(self, verbose: bool = True) -> dict[str, Any]:
        """
        The envelope, in full or reduced to what a caller acts on.

        Terse is SHAPING ONLY. The write still read back, still compared, and
        still reports the same `verified` -- the difference is that the entity
        payload is not serialised. That matters because the payload is the
        content itself, twice: `previous_entities` and `verified_entities` both
        carry the block, so moving a page of prose costs its own text twice
        over to learn one boolean and a parent id.

        Counts replace collections rather than being dropped, because "the
        subtree was 38 blocks" is the part of a destructive result a caller
        needs, and it costs an integer. On a FAILURE the observed entities are
        digested rather than counted: a caller cannot safely re-run the write
        to get detail, so the terse form has to stay actionable.
        """
        if verbose:
            return asdict(self)

        primary = self.verified_entities[0] if self.verified_entities else None
        body: dict[str, Any] = {
            "verified": self.verified,
            **entity_digest(primary),
            "diagnostic": self.diagnostic,
        }
        if len(self.verified_entities) > 1:
            body["verified_count"] = len(self.verified_entities)
        if self.previous_entities:
            body["previous_count"] = len(self.previous_entities)
        if self.recovered_after_timeout:
            body["recovered_after_timeout"] = True
        if not self.verified and self.observed_entities:
            body["observed"] = entity_digests(self.observed_entities)
            body["observed_count"] = len(self.observed_entities)
        return body


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

        KNOWN TRADE-OFF, NOW CLOSED: `getPage` returns ONE entity, so on its
        own it cannot tell a unique title from a duplicated one -- it resolves
        to whichever Logseq picked, and an ambiguous title looked resolved.
        That is exactly the case the split-identity repair has to detect, and
        `createPage` being idempotent does not help on a graph that was
        migrated with duplicates already in it. The fast path therefore
        confirms the title is unique before trusting its answer, at the cost
        of one count query. Ambiguity is refused rather than guessed at,
        because selecting a write target from a fuzzy match is how the wrong
        entity gets modified.
        """
        self._validate_title(title)
        page_class = await self._class_id(PAGE_CLASS)

        # Fast path: name or UUID, one call plus the uniqueness check.
        direct = await self._client.call("logseq.DB.getPage", [title])
        if isinstance(direct, dict) and direct.get("name"):
            recycled = direct.get(":logseq.property/deleted-at") is not None
            is_page = any(self._reference_id(t) == page_class
                          or t == page_class
                          for t in (direct.get("tags") or []))
            if (is_page and not recycled
                    and await self._title_is_unique(title, page_class)):
                return {"found": True, "title": title,
                        "page_uuid": direct.get("uuid")}

        # Fall back to the query, which can see every match and so can report
        # ambiguity. Reached when the fast path found nothing, found a
        # recycled page, found something that is not Page-classed, or found a
        # title more than one page holds.
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

    async def _title_is_unique(self, title: str, page_class: int) -> bool:
        """
        Is exactly one page-classed entity holding this title?

        A count is a fixed, tiny response, so this guard costs one query
        rather than the full pull the fallback path issues. Recycled pages are
        counted too, which only means a graph holding a recycled duplicate
        takes the fallback -- where recycling IS filtered and the single live
        page resolves normally.

        Anything other than exactly one, INCLUDING a count that cannot be
        read, sends the caller to the query path. An unreadable count must not
        read as "unique": that is the failure this guard exists to prevent.
        """
        count = await self._client.call(
            "logseq.DB.datascriptQuery",
            ["[:find (count ?page) . :in $ ?class :where "
             "[?page :block/tags ?class] "
             f"[?page :block/title {json.dumps(title)}]]", page_class])
        return count == 1

    async def is_title_available(self, title: str) -> dict[str, Any]:
        """
        Can this title be written, and if not, what holds it?

        THE DIVERGENCE THIS EXISTS TO EXPOSE. `get_page_uuid` is deliberately
        recycle-blind: a recycled page must not resolve, or a link would
        silently point at a page the user deleted. The write path is
        deliberately recycle-aware: recycling keeps the entity, so the title
        is still taken and `createPage` and `renamePage` both refuse it.

        Both behaviours are correct. Neither was discoverable, and the gap
        between them is expensive: a page recycled in the belief that its
        title would be released, discovered mid-repair to be still holding
        it, with recycling not reversible.

        This uses the WRITERS' rule -- `_entities_by_title`, the same call
        `create_page` and `rename_page` make -- so its answer cannot drift
        from theirs. That rule is broader than pages in two ways worth
        knowing: it counts recycled entities, and it counts blocks, tags and
        properties, because all four share one title space.

        `held_by` is a LIST rather than a single holder. Duplicate titles are
        one of the conditions this is used to untangle, and reporting one of
        two holders would hide the thing being looked for.
        """
        self._validate_title(title)
        holders = await self._entities_by_title(title)
        if not holders:
            return {
                "title": title,
                "available": True,
                "held_by": [],
                "diagnostic": (
                    "No entity holds this title. Pages, tags, blocks and "
                    "properties share one title space, and all four were "
                    "checked."),
            }

        kinds = await self._title_holder_kinds(holders)
        held_by = [
            {
                "uuid": holder.get("uuid"),
                "title": holder.get("title"),
                "kind": kind,
                "recycled": (
                    holder.get(":logseq.property/deleted-at") is not None),
            }
            for holder, kind in zip(holders, kinds)
        ]

        recycled_only = all(h["recycled"] for h in held_by)
        diagnostic = (
            f"{len(held_by)} entity(s) hold this title: "
            + ", ".join(sorted({h["kind"] for h in held_by}))
            + ". Pages, tags, blocks and properties share one title space."
        )
        if recycled_only:
            diagnostic += (
                " Every holder is RECYCLED. Recycling does not release a "
                "title -- the entity survives with its UUID, so createPage "
                "and renamePage both refuse it. getPageUUID reports the same "
                "title as not found, which is deliberate and not a "
                "contradiction: a recycled page must not resolve, or a link "
                "would point at a page the user deleted. listRecycled shows "
                "what is holding it.")
        return {
            "title": title,
            "available": False,
            "held_by": held_by,
            "diagnostic": diagnostic,
        }

    async def _title_holder_kinds(
        self, holders: list[dict[str, Any]]
    ) -> list[str]:
        """
        Classify each holder as page, tag, property or block.

        `:block/name` identifies a page without a query. The class ids for tag
        and property are resolved only if some holder is not a page, so the
        common answers -- nothing, or one page -- stay a single query.
        """
        if all(holder.get("name") for holder in holders):
            return ["page"] * len(holders)

        tag_class = await self._class_id(TAG_CLASS)
        property_class = await self._class_id(PROPERTY_CLASS)

        kinds = []
        for holder in holders:
            if holder.get("name"):
                kinds.append("page")
                continue
            classes = {
                self._reference_id(t) for t in (holder.get("tags") or [])
            }
            if tag_class in classes:
                kinds.append("tag")
            elif property_class in classes:
                kinds.append("property")
            else:
                kinds.append("block")
        return kinds

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
        # Two queries, because an alias relation is invisible in every count
        # below and is the one thing here a delete cannot undo. An empty page
        # with one inbound reference is indistinguishable from a dead stub
        # until you know it is a working alias.
        aliases = await self._alias_relations(page)

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

        alias_of = [h.get("uuid") for h in aliases["aliased_by"]
                    if h.get("uuid")]
        declared_aliases = [a.get("uuid") for a in aliases["aliases"]
                            if a.get("uuid")]

        alias_note = ""
        if alias_of or declared_aliases:
            alias_note = (
                ". ALIAS RELATION: this page "
                + " and ".join(filter(None, [
                    (f"is an alias of {len(alias_of)} page(s)"
                     if alias_of else ""),
                    (f"declares {len(declared_aliases)} alias(es)"
                     if declared_aliases else ""),
                ]))
                + ". Deleting either side breaks resolution, and `alias` is a "
                "built-in property outside this server's writable namespace, "
                "so it cannot be restored afterwards. An empty page in an "
                "alias relation is NOT a dead stub.")
        if len(alias_of) > 1:
            alias_note += (
                " More than one page claims this one as an alias, which is "
                "itself irregular -- read both before touching either.")

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
            # uuid or None rather than a list: a page is normally the alias of
            # exactly one page. Several is reported in the diagnostic, because
            # it is a condition to investigate rather than a shape to model.
            "is_alias_of": alias_of[0] if alias_of else None,
            "aliases": declared_aliases,
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
                   "API." if empty else "")
                + alias_note),
        }

    # ------------------------------------------------------ page listings

    async def list_journals(
        self, *, with_counts: bool = False, limit: int | None = None
    ) -> Any:
        """
        Journal pages, newest first, optionally with block and reference
        counts.

        Deciding which journals hold anything worth reading is the opening
        move of most migrations, and it cost one `pageStats` per journal --
        around fifty calls to answer one question. With `with_counts` the
        whole answer is four queries regardless of how many journals exist:
        the listing, then three aggregates joined back by entity id.

        Sorted by `:block/journal-day` descending, so a cap drops the oldest
        rather than an arbitrary slice.
        """
        pages = await self._query_list(
            "[:find [(pull ?page [:db/id :block/uuid :block/name "
            ":block/title :block/journal-day]) ...] "
            ":where [?page :block/journal-day _]]",
            "Journal listing")
        pages.sort(key=lambda p: p.get("journal-day") or 0, reverse=True)
        return await self._counted_listing(
            pages, key="journals", with_counts=with_counts, limit=limit)

    async def list_pages(
        self, *, with_counts: bool = False, limit: int | None = None
    ) -> Any:
        """
        Live pages, optionally with block and reference counts.

        Recycled pages are excluded -- they keep the Page class and would
        otherwise appear live.

        The same four-query shape as `list_journals`, but a graph has far more
        pages than journals, so the counted form is capped: see
        `_counted_listing`.
        """
        pages = await self._query_list(
            "[:find [(pull ?page [:db/id :block/uuid :block/name "
            ":block/title]) ...] :in $ ?class :where "
            "[?page :block/name] [?page :block/tags ?class] "
            "[(missing? $ ?page :logseq.property/deleted-at)]]",
            "Page listing", await self._class_id(PAGE_CLASS))
        pages.sort(key=lambda p: str(p.get("title") or "").lower())
        return await self._counted_listing(
            pages, key="pages", with_counts=with_counts, limit=limit)

    async def _counted_listing(
        self,
        pages: list[dict[str, Any]],
        *,
        key: str,
        with_counts: bool,
        limit: int | None,
    ) -> Any:
        """
        Return a listing, with counts attached if asked for.

        Without `with_counts` this returns the bare list it always returned,
        so the cheap listing stays cheap and its shape does not change.

        With counts it returns an envelope, because a capped result has to be
        able to say so. The cap exists for `list_pages` on a large graph: the
        QUERY cost is flat, but four numbers per page is not, and a listing
        that quietly grew to thousands of rows would reintroduce the cost this
        was meant to remove. Truncation is reported rather than silent, and
        the diagnostic names the way to get the rest.
        """
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ValueError("limit must be a positive integer")

        if not with_counts:
            return pages[:limit] if limit else pages

        total = len(pages)
        ceiling = limit or MAX_COUNTED_ROWS
        counted = pages[:ceiling]
        truncated = total > len(counted)

        own, empty, refs = await self._count_indexes(
            [p["id"] for p in counted if isinstance(p.get("id"), int)])

        rows = []
        for page in counted:
            page_id = page.get("id")
            own_blocks = own.get(page_id, 0)
            empty_blocks = empty.get(page_id, 0)
            rows.append({
                **page,
                "own_blocks": own_blocks,
                "content_blocks": own_blocks - empty_blocks,
                "refs": refs.get(page_id, 0),
            })

        body: dict[str, Any] = {
            key: rows,
            "total": total,
            "counted": len(rows),
            "truncated": truncated,
            "diagnostic": (
                "own_blocks includes the empty block createPage seeds and any "
                "trailing empty block, so content_blocks is the figure to "
                "pair with refs when judging whether a page carries "
                "anything."),
        }
        if truncated:
            body["diagnostic"] += (
                f" Counts cover the first {len(rows)} of {total}; raise or "
                "set limit for a different slice, or use pageStats for "
                "specific pages.")
        return body

    async def _count_indexes(
        self, page_ids: list[int]
    ) -> tuple[dict[int, int], dict[int, int], dict[int, int]]:
        """
        Own-block, empty-block and inbound-reference counts, by entity id.

        Three aggregate queries rather than three per page. Each is bound to
        the ids being listed with `:in $ [?page ...]`, so the response is one
        row per listed page rather than one per page in the graph.

        A page with no blocks does not appear in the join at all, which is why
        these are merged with `.get(id, 0)` rather than zipped -- the absence
        IS the zero, and a positional merge would silently shift every count
        by one.

        The attributes match `page_stats` exactly, so the two cannot disagree.
        """
        if not page_ids:
            return {}, {}, {}

        def index(rows: list[Any]) -> dict[int, int]:
            return {
                row[0]: row[1] for row in rows
                if isinstance(row, list) and len(row) == 2
                and isinstance(row[0], int) and isinstance(row[1], int)
            }

        own = index(await self._query_list(
            "[:find ?page (count ?block) :in $ [?page ...] "
            ":where [?block :block/page ?page]]",
            "Own block counts", page_ids))
        empty = index(await self._query_list(
            "[:find ?page (count ?block) :in $ [?page ...] "
            ":where [?block :block/page ?page] [?block :block/title \"\"]]",
            "Empty block counts", page_ids))
        refs = index(await self._query_list(
            "[:find ?target (count ?holder) :in $ [?target ...] "
            ":where [?holder :block/refs ?target]]",
            "Inbound reference counts", page_ids))
        return own, empty, refs

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
        self,
        page_uuid: str,
        *,
        acknowledge_reference_rewrite: bool = False,
        acknowledge_alias_loss: bool = False,
    ) -> ContentResult:
        """
        Delete a page, which on this build recycles rather than destroys it.

        A recycled page keeps its UUID, tags, refs and blocks, gaining
        :logseq.property/deleted-at, and is reparented under a Recycle page.
        Inbound references are NOT rewritten, so anything linking to it keeps
        pointing at a page that no longer appears in listings -- which is why
        references are surfaced and require acknowledgement.

        ALIAS RELATIONS require a separate acknowledgement, and it is the more
        serious of the two. A reference to a recycled page still points
        somewhere and can be repointed later; an alias relation cannot be
        rebuilt at all, because `alias` is a built-in property outside the
        plugin namespace this server is permitted to write. Nothing in a block
        or reference count reveals the relation either, so an empty page that
        is a functioning alias reads as a dead stub -- which is exactly the
        shape that invites an unattended delete.

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
        aliases = await self._alias_relations(page)
        previous = (page, *blocks)

        alias_related = aliases["aliases"] + aliases["aliased_by"]
        if alias_related and not acknowledge_alias_loss:
            return ContentResult(
                validation=None, response=None, verified_entities=(),
                verified=False,
                diagnostic=(
                    f"This page is in an ALIAS relation: it declares "
                    f"{len(aliases['aliases'])} alias(es) and "
                    f"{len(aliases['aliased_by'])} page(s) declare it as "
                    "one. Deleting it breaks resolution, and unlike a "
                    "reference this cannot be repaired afterwards -- `alias` "
                    "is a built-in property outside this server's writable "
                    "namespace. Read the related pages first; set "
                    "acknowledge_alias_loss=true to proceed anyway."),
                previous_entities=previous,
                observed_entities=tuple(alias_related))

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
                + (f" {len(alias_related)} alias relation(s) were broken and "
                   "cannot be rebuilt through this API."
                   if alias_related else "")
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

    @serialized_write
    async def retitle_over_duplicate(
        self,
        from_uuid: str,
        to_title: str,
        *,
        park_suffix: str = "(parked)",
    ) -> dict[str, Any]:
        """
        Give a page a title an empty duplicate is holding, by parking the
        holder out of the way first.

        TWO RENAMES, NOTHING ELSE. References are by UUID, so neither page's
        inbound links, tags or property values are touched: the typo fix that
        the documented route did as one `updateBlock` per referring block plus
        a `deletePage` plus a `repairLinks` sweep is two calls here, and
        lossless. A recycled page can be renamed, which is what releases its
        title -- so this works on the recycle-held titles that `getPageUUID`
        reports as free.

        DIRECTION IS THE CALLER'S. Which side keeps the title is a judgement
        about which one the graph is actually wired into, not about which is
        spelled correctly, so it is never inferred: `from_uuid` keeps its
        identity and gains the title. Both pages' inbound reference counts are
        reported either way, including on a refusal, because that is the
        evidence the judgement needs.

        REFUSALS, all reported rather than raised, since each hands back
        something the caller has to decide about:

          - the holder has content blocks. Merging content is a human
            decision and this tool will not make it.
          - the holder is in an alias relation. An alias is live wiring: one
            near-miss here was a page that looked like an abandoned typo and
            was a working alias of the page it resembled.
          - more than one entity holds the title, or the holder is a block,
            tag or property rather than a page. Parking is a page rename.

        NOT ATOMIC. If the second rename fails the first stands, so the
        parked page's UUID and original title are reported explicitly and the
        diagnostic says how to put it back.
        """
        from_uuid = self._require_entity(self._validated_uuid(from_uuid))
        self._require_title(to_title)
        if not isinstance(park_suffix, str) or not park_suffix.strip():
            raise ValueError("park_suffix must be a non-empty string")

        source = await self._page_by_uuid(from_uuid)
        holders = [e for e in await self._entities_by_title(to_title)
                   if e["id"] != source["id"]]

        def report(
            *,
            verified: bool,
            diagnostic: str,
            renamed: dict[str, Any] | None = None,
            parked: dict[str, Any] | None = None,
            references: dict[str, int] | None = None,
        ) -> dict[str, Any]:
            return {
                "verified": verified,
                "from_uuid": from_uuid,
                "to_title": to_title,
                "renamed": renamed,
                "parked": parked,
                "references": references or {},
                "diagnostic": diagnostic,
            }

        if len(holders) > 1:
            return report(
                verified=False,
                diagnostic=(
                    f"{len(holders)} entities hold {to_title!r}. Parking one "
                    "would leave the others holding it, so this is not a "
                    "duplicate pair -- resolve it with isTitleAvailable and "
                    "decide per holder."))

        if not holders:
            # Nothing to park. A plain rename is the whole operation, and
            # rename_page already refuses a clash and verifies by UUID.
            result = await self.rename_page(from_uuid, to_title)
            return report(
                verified=result.verified,
                renamed=self._retitle_state(result, source),
                diagnostic=(
                    f"No entity held {to_title!r}, so nothing was parked and "
                    "this was a plain rename."
                    if result.verified else
                    result.diagnostic or "The rename was not observed."))

        holder = holders[0]
        kind = (await self._title_holder_kinds([holder]))[0]
        if kind != "page":
            return report(
                verified=False,
                diagnostic=(
                    f"{to_title!r} is held by a {kind}, not a page. Parking "
                    "is a page rename, and pages, tags, blocks and "
                    "properties share one title space -- so this clash needs "
                    "a different remedy."))

        # Counts for both sides in one pass: the guard, and the evidence for
        # the direction the caller chose.
        own, empty, refs = await self._count_indexes(
            [holder["id"], source["id"]])
        references = {
            "from": refs.get(source["id"], 0),
            "holder": refs.get(holder["id"], 0),
        }
        holder_content = own.get(holder["id"], 0) - empty.get(holder["id"], 0)

        aliases = await self._alias_relations(holder)
        if aliases["aliases"] or aliases["aliased_by"]:
            return report(
                verified=False,
                references=references,
                diagnostic=(
                    f"{to_title!r} is held by a page in an ALIAS relation "
                    f"({len(aliases['aliases'])} alias(es), "
                    f"{len(aliases['aliased_by'])} page(s) aliasing it). An "
                    "alias is live wiring, not an abandoned duplicate: "
                    "parking or recycling it changes what resolves where. "
                    "Report this and stop."))

        if holder_content:
            return report(
                verified=False,
                references=references,
                diagnostic=(
                    f"{to_title!r} is held by a page with {holder_content} "
                    "content block(s). Moving the title would leave that "
                    "content under a parked name, and merging it is a human "
                    "decision -- read both pages and choose."))

        parked_title = f"{to_title} {park_suffix.strip()}"
        if await self._entities_by_title(parked_title):
            return report(
                verified=False,
                references=references,
                diagnostic=(
                    f"The parking title {parked_title!r} is itself taken. "
                    "Pass a different park_suffix."))

        park = await self.rename_page(holder["uuid"], parked_title)
        if not park.verified:
            return report(
                verified=False,
                references=references,
                diagnostic=(
                    "The holder could not be parked, so nothing was changed. "
                    + (park.diagnostic or "")))

        rename = await self.rename_page(from_uuid, to_title)
        parked_state = self._retitle_state(park, holder)
        if not rename.verified:
            return report(
                verified=False,
                parked=parked_state,
                references=references,
                diagnostic=(
                    f"PARTIALLY APPLIED. {to_title!r} was freed by parking "
                    f"its holder as {parked_title!r}, but the rename of "
                    f"{from_uuid} did not take effect. To undo, rename "
                    f"{holder['uuid']} back to {to_title!r}. "
                    + (rename.diagnostic or "")))

        return report(
            verified=True,
            renamed=self._retitle_state(rename, source),
            parked=parked_state,
            references=references,
            diagnostic=(
                f"Two renames, no block edits. References are by UUID, so "
                f"every inbound link, tag and property value on both pages "
                f"survived -- {references['from']} on the renamed page and "
                f"{references['holder']} on the parked one, now displaying "
                f"the parked title. The parked page still exists; recycle it "
                "only after checking findBacklinks."))

    @staticmethod
    def _retitle_state(
        result: ContentResult, before: dict[str, Any]
    ) -> dict[str, Any]:
        after = result.verified_entities[0] if result.verified_entities else {}
        return {
            "uuid": before.get("uuid"),
            "previous_title": before.get("title"),
            "title": after.get("title"),
            "recycled": before.get(":logseq.property/deleted-at") is not None,
        }

    async def _alias_relations(
        self, page: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Alias relations in both directions, as entities.

        Both directions matter and they are different facts. A page may
        DECLARE aliases (other pages that resolve to it), or it may BE one
        (something else declares it). Deleting either side breaks resolution,
        and neither is visible in a block or reference count: an empty page
        that is a working alias looks exactly like a dead stub.

        BOTH ATTRIBUTES are queried, and which one is live is NOT settled.
        Observed 2026-09-20 on 2.0.1-alpha+nightly.20260826: `:block/alias`
        held every relation in the graph and `:logseq.property/alias` had
        zero holders -- the opposite of what this docstring asserted for a
        year. Neither form is safe to assume, and a guard written against
        only one of them silently never fires, which is the worst possible
        outcome for a guard whose whole job is to stop a deletion. The
        or-join costs nothing and is the only reason the guard fired at all
        on that build. DO NOT narrow it to whichever attribute today's graph
        happens to use.

        Note that `alias` is a built-in property, outside the plugin sandbox
        this server can write. So an alias relation destroyed by a delete
        cannot be restored through this API at all.
        """
        entity = "[:db/id :block/uuid :block/title :block/name]"
        aliased_by = await self._query_list(
            f"[:find [(pull ?holder {entity}) ...] :in $ ?target :where "
            "(or-join [?holder ?target] "
            "[?holder :logseq.property/alias ?target] "
            "[?holder :block/alias ?target])]",
            "Alias holder lookup", page["id"])
        aliases = await self._query_list(
            f"[:find [(pull ?alias {entity}) ...] :in $ ?page :where "
            "(or-join [?page ?alias] "
            "[?page :logseq.property/alias ?alias] "
            "[?page :block/alias ?alias])]",
            "Alias lookup", page["id"])
        return {
            "aliases": [a for a in aliases if isinstance(a, dict)],
            "aliased_by": [h for h in aliased_by if isinstance(h, dict)],
        }

    @serialized_write
    async def move_blocks(
        self,
        block_uuids: list[str],
        target_uuid: str,
        *,
        placement: str = "last-child",
        all_or_nothing: bool = False,
    ) -> dict[str, Any]:
        """
        Relocate a list of blocks, in the given order, in one call.

        WHY THIS IS NOT A LOOP OVER `move_block`. That would cost about eight
        API calls per block -- preflight, target read, subtree check, sibling
        read, the move, and three read-backs -- so a 31-block chapter would be
        some 250 calls and a 115-block one would time out. Everything that
        does not change per block is hoisted: the target is read once, all the
        sources are read in one query, and the guards run once over the whole
        set. That leaves two calls per block, the move and its read-back.

        ORDER COMES FROM CHAINING, not from recomputing the last child each
        time. The first block is placed according to `placement`; every
        subsequent block is placed AFTER the one before it. So the list
        arrives in the order given, and the position is established by the
        block that just landed rather than by a query. `last-child` therefore
        appends the run to whatever the target already held, and `child`
        places the run at the top -- in order, in both cases.

        NOT ATOMIC, and the batch stops at the first block that does not
        verify. `moved` reports every block with its own verdict, so a partial
        result says exactly where it stopped and which UUIDs landed. The chain
        is why it stops rather than continuing: the next block's position is
        defined by the previous one, and placing it after a block that did not
        move would put it somewhere nobody asked for.

        `all_or_nothing` attempts to move the landed blocks back to their
        original parents. Read the limitation in `_rollback_moves` before
        relying on it: parentage is restorable, POSITION IS NOT, because
        nothing in this API can write `:block/order`, so the blocks come back
        grouped at the top of their old parent rather than where they sat.

        Capped at MAX_MOVE_BATCH. Anything beyond the cap is returned
        untouched in `not_attempted`, in order, so the next call continues
        where this one stopped -- with `last-child`, appending after what has
        already arrived.
        """
        if not isinstance(block_uuids, list) or not block_uuids:
            raise ValueError("block_uuids must be a non-empty list")
        if placement not in {"child", "last-child", "before", "after"}:
            raise ValueError(
                "placement must be child, last-child, before, or after")

        uuids = [self._require_entity(self._validated_uuid(u))
                 for u in block_uuids]
        if len(set(uuids)) != len(uuids):
            raise ValueError(
                "block_uuids contains the same block twice; the second move "
                "would relocate what the first placed")

        target_uuid = self._validated_uuid(target_uuid)
        if target_uuid in uuids:
            raise ValueError("The target cannot also be one of the blocks")

        target = await self._entity_by_uuid(target_uuid)
        if placement in {"before", "after"} and target.get("name"):
            raise ValueError(
                "A page has no siblings; use placement=child or last-child to "
                "move blocks to the top level of a page")

        sources = await self._entities_by_uuids(uuids)
        missing = [u for u in uuids if u not in sources]
        if missing:
            raise ValueError(
                f"{len(missing)} of the {len(uuids)} blocks do not exist: "
                f"{missing[:3]}")

        source_ids = {sources[u]["id"] for u in uuids}

        # Moving a block beneath its own descendant detaches the subtree. One
        # recursive parent pull gave every ancestor chain already, so this is
        # a set test rather than a query per block.
        target_ancestors = self._ancestor_ids(target) | {target["id"]}
        inside = [u for u in uuids if sources[u]["id"] in target_ancestors]
        if inside:
            raise ValueError(
                f"The target is inside the subtree of {len(inside)} of these "
                f"blocks ({inside[:3]}); moving there would detach them from "
                "the graph")

        # A block that is a descendant of another in the same list travels
        # with it, and then gets pulled back out -- an outcome nobody asks
        # for, so it is refused rather than performed.
        nested = [u for u in uuids
                  if self._ancestor_ids(sources[u]) & source_ids]
        if nested:
            raise ValueError(
                f"{len(nested)} of these blocks are descendants of others in "
                f"the same list ({nested[:3]}). Move the ancestors alone -- "
                "a move carries the whole subtree.")

        expected_parent = (target["id"] if placement in {"child", "last-child"}
                           else self._reference_id(target.get("parent")))
        expected_page = (target["id"] if target.get("name")
                         else self._reference_id(target.get("page")))
        if expected_parent is None or expected_page is None:
            raise RuntimeError(
                "The target is missing the parent or page reference needed to "
                "place blocks relative to it")

        attempted, not_attempted = (uuids[:MAX_MOVE_BATCH],
                                    uuids[MAX_MOVE_BATCH:])

        anchor, options = await self._first_placement(
            target, target_uuid, placement, source_ids)

        moved: list[dict[str, Any]] = []
        landed: list[str] = []
        stopped: str | None = None

        for block_uuid in attempted:
            _response, timed_out = await self._call_ambiguous(
                "logseq.DB.moveBlock", [block_uuid, anchor, options])
            current = await poll_readback(
                self._client,
                lambda u=block_uuid: self._entity_by_uuid(u),
                lambda e: (self._reference_id(e.get("parent"))
                           == expected_parent
                           and self._reference_id(e.get("page"))
                           == expected_page))
            landed_here = (
                self._reference_id(current.get("parent")) == expected_parent
                and self._reference_id(current.get("page")) == expected_page)
            moved.append({
                "uuid": block_uuid,
                "verified": landed_here,
                "recovered_after_timeout": timed_out,
            })
            if not landed_here:
                stopped = (
                    f"Stopped at {block_uuid}: the move returned without "
                    "error but the block is not under the target. Each "
                    "block's position is defined by the one before it, so "
                    "continuing would place the rest somewhere nobody asked "
                    "for.")
                break
            landed.append(block_uuid)
            # The block that just landed anchors the next one. This is what
            # preserves the given order without a query per block.
            anchor, options = block_uuid, {"before": False}

        return await self._summarise_moves(
            moved=moved, landed=landed, attempted=attempted,
            not_attempted=not_attempted, stopped=stopped,
            sources=sources, target=target, target_uuid=target_uuid,
            placement=placement, expected_page=expected_page,
            all_or_nothing=all_or_nothing)

    async def _first_placement(
        self,
        target: dict[str, Any],
        target_uuid: str,
        placement: str,
        source_ids: set[int],
    ) -> tuple[str, dict[str, Any]]:
        """
        Where the FIRST block goes. Every later one goes after its
        predecessor.

        `last-child` resolves to "after the target's current last child",
        skipping any child that is itself being moved -- anchoring to a block
        that is about to move would place the run relative to something that
        will not be there.
        """
        if placement == "last-child":
            siblings = [c for c in await self._children_of(target_uuid)
                        if c.get("id") not in source_ids]
            if siblings:
                return siblings[-1]["uuid"], {"before": False}
            return target_uuid, {"children": True}
        if placement == "child":
            return target_uuid, {"children": True}
        return target_uuid, {"before": placement == "before"}

    async def _entities_by_uuids(
        self, uuids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """
        Read many entities by UUID in one query, keyed by uuid.

        A UUID cannot be passed as a parameter and matched against
        `:block/uuid` -- a JSON string does not equal a uuid value -- so the
        literals are interpolated, which is safe because every one has already
        been through `_validated_uuid`. `or` rather than one query per block:
        50 round trips to read 50 blocks is the cost this tool exists to
        remove.

        `{:block/parent ...}` recurses, so each entity arrives with its whole
        ancestor chain. That is what makes the subtree guards set tests
        instead of queries.
        """
        clauses = " ".join(
            f'[?e :block/uuid #uuid "{u}"]' for u in uuids)
        found = await self._query_list(
            "[:find [(pull ?e [:db/id :block/uuid :block/title :block/order "
            "{:block/page [:db/id :block/uuid]} {:block/parent ...}]) ...] "
            f":where (or {clauses})]",
            "Bulk block lookup")
        return {e["uuid"]: e for e in found
                if isinstance(e, dict) and e.get("uuid")}

    @staticmethod
    def _ancestor_ids(entity: dict[str, Any]) -> set[int]:
        """Every id above this entity, from a recursive parent pull."""
        ancestors: set[int] = set()
        parent = entity.get("parent")
        while isinstance(parent, dict):
            if isinstance(parent.get("id"), int):
                ancestors.add(parent["id"])
            parent = parent.get("parent")
        return ancestors

    async def _summarise_moves(
        self,
        *,
        moved: list[dict[str, Any]],
        landed: list[str],
        attempted: list[str],
        not_attempted: list[str],
        stopped: str | None,
        sources: dict[str, dict[str, Any]],
        target: dict[str, Any],
        target_uuid: str,
        placement: str,
        expected_page: int,
        all_or_nothing: bool,
    ) -> dict[str, Any]:
        """
        Build the result, after two checks that only make sense over the
        whole set.

        Order is verified by reading the destination's children ONCE, not from
        the per-block read-backs. Each of those confirms a parent, and a
        correct parent with the wrong order is exactly the failure this tool
        exists to prevent.
        """
        landed_ids = {sources[u]["id"] for u in landed}
        stranded = await self._stranded_below(landed_ids, expected_page)
        order_preserved = await self._order_preserved(
            landed, target, target_uuid, placement)

        rolled_back: list[dict[str, Any]] = []
        rollback_note = ""
        if stopped and all_or_nothing and landed:
            rolled_back, rollback_note = await self._rollback_moves(
                landed, sources)
            landed = [u for u in landed
                      if not any(r["uuid"] == u and r["verified"]
                                 for r in rolled_back)]

        verified = (not stopped and not stranded
                    and order_preserved is not False
                    and not not_attempted)

        diagnostic = (
            f"{len(landed)} of {len(attempted)} attempted block(s) landed "
            f"under the target, in the order given."
            if not stopped else stopped)
        if stranded:
            diagnostic += (
                f" {len(stranded)} descendant(s) of moved blocks still belong "
                "to the old page and are invisible to page-scoped queries "
                "until repaired.")
        if order_preserved is False:
            diagnostic += (
                " The blocks are under the target but NOT in the order given "
                "-- read the destination before treating this as done.")
        elif order_preserved is None:
            diagnostic += (
                " Order could not be verified: the destination's own UUID was "
                "not available to read its children back.")
        if not_attempted:
            diagnostic += (
                f" {len(not_attempted)} block(s) were not attempted, capped "
                f"at {MAX_MOVE_BATCH} per call. Pass them in a further call "
                "with the same placement; last-child appends after what has "
                "already arrived.")
        diagnostic += rollback_note

        return {
            "verified": verified,
            "target_uuid": target_uuid,
            "placement": placement,
            "moved": moved,
            "summary": {
                "requested": len(attempted) + len(not_attempted),
                "attempted": len(attempted),
                "landed": len(landed),
                "failed": len(attempted) - len(landed) - len(rolled_back),
                "not_attempted": len(not_attempted),
            },
            "order_preserved": order_preserved,
            "not_attempted": not_attempted,
            "rolled_back": rolled_back,
            "stranded_descendants": entity_digests(stranded),
            "diagnostic": diagnostic,
        }

    async def _order_preserved(
        self,
        landed: list[str],
        target: dict[str, Any],
        target_uuid: str,
        placement: str,
    ) -> bool | None:
        """
        Do the landed blocks appear among their new siblings in the order
        given?

        Returns None rather than True when the destination cannot be read --
        an unverifiable claim must not be reported as a verified one.
        """
        if len(landed) < 2:
            return True
        parent_uuid = (
            target_uuid if placement in {"child", "last-child"}
            else (target.get("parent") or {}).get("uuid"))
        if not parent_uuid:
            return None
        siblings = await self._children_of(parent_uuid)
        observed = [c["uuid"] for c in siblings if c["uuid"] in set(landed)]
        return observed == landed

    async def _stranded_below(
        self, roots: set[int], expected_page: int
    ) -> list[dict[str, Any]]:
        """
        Descendants of the moved blocks whose owning page did not follow.

        Breadth-first, ONE QUERY PER LEVEL rather than per block: a flat run
        of siblings costs a single query, and depth is what costs more rather
        than width. A subtree left pointing at the old page is a real child of
        the target that no page-scoped query can see, which is the same
        invisible-orphan failure a single move checks for.
        """
        stranded: list[dict[str, Any]] = []
        frontier = set(roots)
        seen: set[int] = set(roots)
        for _ in range(MAX_SUBTREE_DEPTH):
            if not frontier:
                break
            children = await self._query_list(
                "[:find [(pull ?child [:db/id :block/uuid :block/title "
                "{:block/page [:db/id]} {:block/parent [:db/id]}]) ...] "
                ":in $ [?parent ...] :where [?child :block/parent ?parent]]",
                "Stranded descendant sweep", sorted(frontier))
            frontier = set()
            for child in children:
                if not isinstance(child, dict) or child["id"] in seen:
                    continue
                seen.add(child["id"])
                frontier.add(child["id"])
                if self._reference_id(child.get("page")) != expected_page:
                    stranded.append(child)
        return stranded

    async def _rollback_moves(
        self, landed: list[str], sources: dict[str, dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], str]:
        """
        Move the landed blocks back under their original parents.

        WHAT THIS DOES NOT RESTORE: position. Nothing in this API can write
        `:block/order`, so a block can only be put back UNDER its old parent,
        not back where it sat among that parent's children. Blocks returning
        to the same parent keep their order relative to each other, and
        nothing else about the original arrangement survives.

        That makes rollback a partial remedy, which is why it is opt-in and
        why the result says what it did rather than reporting "restored".
        Read it before relying on it: for a chapter pulled out of a journal,
        "back under the journal, at the end" may or may not be better than
        leaving it where it landed.
        """
        restored: list[dict[str, Any]] = []
        anchors: dict[str, str] = {}
        for block_uuid in landed:
            parent = sources[block_uuid].get("parent") or {}
            parent_uuid = parent.get("uuid")
            if not parent_uuid:
                restored.append({"uuid": block_uuid, "verified": False})
                continue
            if parent_uuid in anchors:
                anchor, options = anchors[parent_uuid], {"before": False}
            else:
                anchor, options = parent_uuid, {"children": True}
            await self._call_ambiguous(
                "logseq.DB.moveBlock", [block_uuid, anchor, options])
            current = await poll_readback(
                self._client,
                lambda u=block_uuid: self._entity_by_uuid(u),
                lambda e, p=parent.get("id"): (
                    self._reference_id(e.get("parent")) == p))
            back = self._reference_id(current.get("parent")) == parent.get("id")
            if back:
                anchors[parent_uuid] = block_uuid
            restored.append({"uuid": block_uuid, "verified": back})

        succeeded = sum(1 for r in restored if r["verified"])
        note = (
            f" all_or_nothing: {succeeded} of {len(restored)} landed block(s) "
            "were moved back under their original parents. POSITION WAS NOT "
            "RESTORED -- `:block/order` cannot be written through this API, "
            "so they sit grouped at the TOP of the original parent, in their "
            "original relative order, rather than where they were.")
        if succeeded != len(restored):
            note += (
                " The remainder are still under the target; read both places "
                "before acting further.")
        return restored, note

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

        PLACEMENT. `child` PREPENDS -- that is Logseq's behaviour, not a
        choice made here, and it is kept as-is because redefining it would
        break callers. `last-child` appends, which is what moving a sequence
        of blocks needs: with `child`, moving siblings in source order
        reverses them at the destination, silently and with no safe
        alternative. `before` and `after` place the block as a sibling of the
        target.

        `last-child` is implemented as "after the target's current last
        child" rather than by generating an order key. Nothing in this API can
        write `:block/order` -- `updateBlock` takes a title and nothing else --
        so the only way to obtain an order is to let Logseq compute one
        relative to an existing sibling. It costs one read of the target's
        children, which is also what makes the result checkable: appending is
        verified by the moved block being LAST, not merely by its parent.

        Three things are checked, because a move can go wrong in three ways:
        the parent may not change; the owning page may not follow the block to
        a new page; and descendants may be left behind pointing at the old
        page. The last two are invisible to page-scoped queries, which is the
        same failure that made malformed nested writes undetectable.
        """
        block_uuid = self._require_entity(self._validated_uuid(block_uuid))
        target_uuid = self._validated_uuid(target_uuid)
        if placement not in {"child", "last-child", "before", "after"}:
            raise ValueError(
                "placement must be child, last-child, before, or after")

        block = await self._preflight_block(block_uuid, role="source")
        target = await self._entity_by_uuid(target_uuid)
        if block["id"] == target["id"]:
            raise ValueError("A block cannot be moved relative to itself")
        if placement in {"before", "after"} and target.get("name"):
            raise ValueError(
                "A page has no siblings; use placement=child or last-child to "
                "move a block to the top level of a page")

        # Moving a block beneath its own descendant would detach the subtree
        # from the tree entirely.
        subtree = await self._descendants_by_parent(block_uuid)
        if any(d.get("id") == target["id"] for d in subtree):
            raise ValueError(
                "The target is inside the block's own subtree; moving there "
                "would detach it from the graph")

        expected_parent = (target["id"] if placement in {"child", "last-child"}
                           else self._reference_id(target.get("parent")))
        expected_page = (target["id"] if target.get("name")
                         else self._reference_id(target.get("page")))
        if expected_parent is None or expected_page is None:
            raise RuntimeError(
                "The target is missing the parent or page reference needed to "
                "place a block relative to it")

        anchor_uuid = target_uuid
        options: dict[str, Any] = ({"children": True}
                                   if placement in {"child", "last-child"}
                                   else {"before": placement == "before"})

        if placement == "last-child":
            siblings = await self._children_of(target_uuid)
            if siblings and siblings[-1].get("id") == block["id"]:
                # Already last. Reported as verified because the requested
                # state holds and was READ, not because a write was observed;
                # issuing the move anyway would return null and read as a
                # silent no-op.
                return ContentResult(
                    validation=None, response=None,
                    verified_entities=(block,),
                    diagnostic=(
                        "No move was needed: the block is already the last "
                        "child of the target."),
                    previous_entities=(block,))
            others = [s for s in siblings if s.get("id") != block["id"]]
            if others:
                # Append by placing after the current last child. Its parent
                # is the target, so expected_parent above still holds.
                anchor_uuid = others[-1]["uuid"]
                options = {"before": False}

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.moveBlock", [block_uuid, anchor_uuid, options])

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

        if placement == "last-child":
            # The point of this placement, so it is checked rather than
            # assumed: a correct parent with the wrong order is exactly the
            # outcome `child` produces, and the reason this exists.
            final = await poll_readback(
                self._client,
                lambda: self._children_of(target_uuid),
                lambda kids: bool(kids) and kids[-1].get("id") == moved["id"])
            if not final or final[-1].get("id") != moved["id"]:
                position = next(
                    (i for i, kid in enumerate(final)
                     if kid.get("id") == moved["id"]), None)
                return ContentResult(
                    validation=None, response=response,
                    verified_entities=(moved,),
                    recovered_after_timeout=timed_out, verified=False,
                    diagnostic=(
                        "The block is under the requested parent but not last: "
                        f"it is at position {position} of {len(final)}. "
                        "Moving a sequence from here would not preserve "
                        "source order."),
                    previous_entities=(block,),
                    observed_entities=tuple(final))

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

    @serialized_write
    async def create_page_of_blocks(
        self,
        page_uuid: str,
        outline: str,
        *,
        dry_run: bool = False,
        verbose: bool = True,
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
            # The outline was supplied by the caller, so echoing every created
            # block's title back is the one payload they already have.
            **({"created": created} if verbose else {
                "created_count": len(created),
                "created": entity_digests(created),
            }),
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
        # Fractional-index keys are designed for lexicographic comparison, so
        # sorting them as strings is correct for ordinary siblings. Two edges
        # are not: a child with no :block/order sorts under "" and one with an
        # explicit null under "None", which lands after digits. Since
        # `last-child` is verified by asking whether this list's final element
        # is the moved block, an order-less sibling can produce a spurious
        # verified:false -- or mask a real one. Read :block/order directly when
        # a position matters rather than trusting this ordering.
        children.sort(key=lambda c: str(c.get("order", "")))
        return children

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

"""
Page and block operations.

The fake graph below is deliberately more than a canned-response queue: the
behaviours worth testing here are relational -- which parent a block ends up
under, whether a subtree really went -- and a response queue cannot express
those. It also lets a test assert that a write did NOT happen, which is the
failure mode this API actually has.
"""

import itertools
import json
import re
from typing import Any

import pytest

from mcp_logseq_db.access import WriteAccessPolicy
from mcp_logseq_db.content import (
    VerifiedContent,
    _parse_outline,
    grouping_key,
    within_edit_distance,
)

PAGE_CLASS_ID = 4
PROPERTY_CLASS_ID = 3
TAG_CLASS_ID = 2


def order_key(value: float) -> str:
    """
    A fixed-width numeric string.

    Logseq's real orders are fractional-index strings that sort
    LEXICOGRAPHICALLY, which is how `Zz` ends up before `a0`. The code sorts
    by str(order) for that reason, so the fake has to produce keys where
    lexicographic and numeric order agree -- constant width does that, and a
    bare str(1.5) would not.
    """
    return f"{value:012.4f}"


def order_value(order: Any) -> float:
    try:
        return float(order)
    except (TypeError, ValueError):
        return 0.0


class FakeGraph:
    """A minimal DB with real parent/page relationships."""

    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self.entities: dict[str, dict[str, Any]] = {}
        self.add("Page", None, None, name="page", ident=":logseq.class/Page",
                 entity_id=PAGE_CLASS_ID)
        self.add("Property", None, None, name="property",
                 ident=":logseq.class/Property", entity_id=PROPERTY_CLASS_ID)
        # Present because classifying what holds a title has to tell a tag
        # from a block, and an unresolvable class ident is a hard error.
        self.add("Tag", None, None, name="tag", ident=":logseq.class/Tag",
                 entity_id=TAG_CLASS_ID)
        self.page = self.add("TEST-PAGE", None, None, name="test-page",
                             tags=[PAGE_CLASS_ID])

    def add(self, title, parent, page, *, name=None, tags=None, ident=None,
            entity_id=None, extra=None, order=None) -> dict[str, Any]:
        entity_id = entity_id if entity_id is not None else next(self._ids)
        uuid = "%08x-0000-4000-8000-000000000000" % entity_id
        entity: dict[str, Any] = {"id": entity_id, "uuid": uuid, "title": title}
        if name:
            entity["name"] = name
        if ident:
            entity["ident"] = ident
        if tags:
            entity["tags"] = [{"id": t} for t in tags]
        if parent is not None:
            entity["parent"] = {"id": parent}
            # Appended after existing siblings, which is what insertBlock does.
            # Order is modelled because placement is a real behaviour: without
            # it, a test asserting that three blocks arrive in source order
            # passes whatever the code does.
            entity["order"] = order if order is not None else order_key(
                max((order_value(s.get("order"))
                     for s in self.children(parent)), default=0.0) + 1.0)
        if page is not None:
            entity["page"] = {"id": page}
        if extra:
            entity.update(extra)
        self.entities[uuid] = entity
        return entity

    def ordered_children(self, parent_id: int) -> list[dict[str, Any]]:
        """Children in document order, as _children_of sorts them."""
        return sorted(self.children(parent_id),
                      key=lambda e: str(e.get("order", "")))

    def by_id(self, entity_id: int) -> dict[str, Any] | None:
        return next((e for e in self.entities.values()
                     if e["id"] == entity_id), None)

    def children(self, parent_id: int) -> list[dict[str, Any]]:
        return [e for e in self.entities.values()
                if e.get("parent", {}).get("id") == parent_id]

    def descendants(self, entity_id: int) -> list[dict[str, Any]]:
        out, queue = [], [entity_id]
        while queue:
            for child in self.children(queue.pop()):
                out.append(child)
                queue.append(child["id"])
        return out


class FakeClient:
    """Interprets the queries content.py actually issues."""

    def __init__(self, graph: FakeGraph, *, policy=None,
                 write_effective: bool = True) -> None:
        self.graph = graph
        self.write_policy = policy
        self.write_effective = write_effective
        self.calls: list[tuple[str, list[Any]]] = []

    async def call(self, method: str, args: list[Any]) -> Any:
        self.calls.append((method, args))
        if method == "logseq.DB.createPage":
            return self._create_page(*args)
        if method == "logseq.DB.getPage":
            return self._get_page(*args)
        if method == "logseq.DB.updateBlock":
            return self._update_block(*args)
        if method == "logseq.DB.renamePage":
            return self._rename(*args)
        if method == "logseq.DB.deletePage":
            return self._delete_page(args[0])
        if method == "logseq.DB.insertBlock":
            return self._insert_block(*args)
        if method == "logseq.DB.insertBatchBlock":
            return self._insert_batch(*args)
        if method == "logseq.DB.removeBlock":
            return self._remove(args[0])
        if method == "logseq.DB.moveBlock":
            return self._move(*args)
        if method == "logseq.DB.datascriptQuery":
            return self._query(args[0], args[1:])
        # upsertNodes is deliberately NOT handled: it fails on synced graphs
        # and nothing routes through it any more. A call here means something
        # regressed.
        raise AssertionError(f"unexpected method {method}")

    def _create_page(self, title, properties=None):
        """Idempotent on title -- a repeat returns the existing page rather
        than creating a duplicate. Creates one empty first block, as the real
        method does.

        The second argument is a PROPERTIES map, not options: passing
        {"dry-run": true} creates the page anyway and mints a property.
        """
        if not self.write_effective:
            return None
        existing = next(
            (e for e in self.graph.entities.values()
             if e.get("name") == str(title).lower()), None)
        if existing is not None:
            return dict(existing)
        page = self.graph.add(title, None, None, name=str(title).lower(),
                              tags=[PAGE_CLASS_ID], extra=properties or None)
        self.graph.add("", page["id"], page["id"])
        return dict(page)

    def _get_page(self, identifier):
        """Accepts a name OR a uuid, and returns recycled pages -- both
        behaviours the caller has to compensate for."""
        found = self.graph.entities.get(identifier)
        if found is None:
            found = next(
                (e for e in self.graph.entities.values()
                 if e.get("name") == str(identifier).lower()
                 or e.get("title") == identifier), None)
        return dict(found) if found else None

    def _update_block(self, block_uuid, title):
        if not self.write_effective:
            return None
        current = self.graph.entities[block_uuid]
        # Replace rather than mutate: the real client returns a fresh dict per
        # call, so an in-place edit would let an earlier snapshot alias it.
        self.graph.entities[block_uuid] = {**current, "title": title}
        return None

    def _with_page_titles(self, entity):
        """Resolve `{:block/page [...]}` to the pulled shape the row builder
        reads, rather than the bare `{"id": n}` the graph stores."""
        copy = dict(entity)
        page_id = (entity.get("page") or {}).get("id")
        page = self.graph.by_id(page_id) if page_id is not None else None
        if page is not None:
            copy["page"] = {"uuid": page["uuid"], "title": page.get("title")}
        else:
            copy.pop("page", None)
        return copy

    def _with_parent_chain(self, entity):
        """Attach the ancestor chain, as a recursive parent pull returns it.
        The subtree guards in move_blocks are set tests over this rather than
        a query per block, so the shape has to be faithful."""
        copy = dict(entity)
        chain = copy
        parent_id = (entity.get("parent") or {}).get("id")
        while parent_id is not None:
            parent = self.graph.by_id(parent_id)
            if parent is None:
                break
            nested = {"id": parent["id"], "uuid": parent["uuid"]}
            chain["parent"] = nested
            chain = nested
            parent_id = (parent.get("parent") or {}).get("id")
        return copy

    def _rename(self, page_uuid, new_title):
        """Updates :block/title AND :block/name. The tool verifies the name
        survived, because a rename that stripped page identity would
        otherwise look like success.

        Works on a RECYCLED page, which is what releases its title -- the
        behaviour retitleOverDuplicate is built on.
        """
        if not self.write_effective:
            return None
        current = self.graph.entities[page_uuid]
        self.graph.entities[page_uuid] = {
            **current, "title": new_title, "name": str(new_title).lower()}
        return None

    def _delete_page(self, identifier):
        """RECYCLES rather than destroys: the entity survives carrying
        :logseq.property/deleted-at, keeping its UUID, tags and blocks, and
        its inbound references are NOT rewritten.

        Accepts a uuid or a name, since which one the route keys on is
        unconfirmed and the tool tries both.
        """
        if not self.write_effective:
            return None
        found = self.graph.entities.get(identifier)
        if found is None:
            found = next(
                (e for e in self.graph.entities.values()
                 if e.get("name") == str(identifier).lower()), None)
        if found is None:
            return None
        self.graph.entities[found["uuid"]] = {
            **found, ":logseq.property/deleted-at": 1758240000}
        return None

    def _insert_block(self, target_uuid, title, options=None):
        """sibling: false means child of the target. Unlike upsertNodes this
        sets :block/parent and :block/page independently, and returns the
        created entity."""
        if not self.write_effective:
            return None
        target = self.graph.entities[target_uuid]
        page = target["id"] if target.get("name") else target["page"]["id"]
        return dict(self.graph.add(title, target["id"], page))

    def _insert_batch(self, target_uuid, blocks, options=None):
        if not self.write_effective:
            return None
        target = self.graph.entities[target_uuid]
        page = target["id"] if target.get("name") else target["page"]["id"]
        return [dict(self.graph.add(b["content"], target["id"], page))
                for b in blocks]

    def _move(self, block_uuid, target_uuid, options=None):
        """Reparent, carrying the subtree's page with it, and place the block
        within its new siblings. Returns nothing -- which is the whole reason
        the tool verifies by reading back.

        `{"children": true}` PREPENDS, which is Logseq's actual behaviour and
        the reason last-child exists; `{"before": …}` places the block next to
        the target.
        """
        if not self.write_effective:
            return None
        block = self.graph.entities[block_uuid]
        target = self.graph.entities[target_uuid]
        as_child = bool((options or {}).get("children"))
        parent = target["id"] if as_child else target["parent"]["id"]
        page = target["id"] if target.get("name") else target["page"]["id"]

        siblings = [e for e in self.graph.ordered_children(parent)
                    if e["id"] != block["id"]]
        # Halve toward zero rather than subtracting, so keys stay positive.
        # A negative key would break the fixed-width trick: "-00001" sorts
        # BEFORE "-00002" lexicographically, which is the wrong way round.
        if as_child:
            position = (order_value(siblings[0].get("order")) / 2.0
                        if siblings else 1.0)
        else:
            index = next(i for i, s in enumerate(siblings)
                         if s["id"] == target["id"])
            here = order_value(siblings[index].get("order"))
            if (options or {}).get("before"):
                position = ((order_value(siblings[index - 1].get("order"))
                             + here) / 2 if index else here / 2.0)
            else:
                below = (order_value(siblings[index + 1].get("order"))
                         if index + 1 < len(siblings) else here + 2.0)
                position = (here + below) / 2

        block["parent"] = {"id": parent}
        block["page"] = {"id": page}
        block["order"] = order_key(position)
        for descendant in self.graph.descendants(block["id"]):
            descendant["page"] = {"id": page}
        return None

    def _remove(self, uuid):
        if not self.write_effective:
            return None
        entity = self.graph.entities.get(uuid)
        if entity is None:
            return None
        for descendant in self.graph.descendants(entity["id"]):
            self.graph.entities.pop(descendant["uuid"], None)
        self.graph.entities.pop(uuid, None)
        return None

    def _query(self, query: str, params):
        if ":db/ident" in query and ":find ?class" in query:
            ident = query.split(":db/ident ")[1].split("]")[0]
            return next((e["id"] for e in self.graph.entities.values()
                         if e.get("ident") == ident), None)
        # ---- page listings and their count aggregates ---------------------
        # Placed before the generic branches below, which would otherwise
        # match on :block/page and return entities where rows are expected.
        if ":block/journal-day" in query and ":find [(pull ?page" in query:
            return [e for e in self.graph.entities.values()
                    if e.get("journal-day")]
        if "(missing? $ ?page :logseq.property/deleted-at)" in query:
            return [e for e in self.graph.entities.values()
                    if e.get("name")
                    and any(t.get("id") == params[0]
                            for t in e.get("tags", []))
                    and e.get(":logseq.property/deleted-at") is None]
        if "(count ?block)" in query and ":in $ [?page ...]" in query:
            wanted = set(params[0])
            empty_only = '[?block :block/title ""]' in query
            rows: dict[int, int] = {}
            for entity in self.graph.entities.values():
                page_id = (entity.get("page") or {}).get("id")
                if page_id not in wanted:
                    continue
                if empty_only and entity.get("title") != "":
                    continue
                rows[page_id] = rows.get(page_id, 0) + 1
            # A page with no blocks does not appear in the join at all, which
            # is the case the merge has to treat as zero.
            return [[page_id, n] for page_id, n in rows.items()]
        if "(count ?holder)" in query and ":block/refs ?target" in query:
            wanted = set(params[0])
            rows = {}
            for entity in self.graph.entities.values():
                for ref in entity.get("refs", []) or []:
                    target = ref.get("id") if isinstance(ref, dict) else None
                    if target in wanted:
                        rows[target] = rows.get(target, 0) + 1
            return [[target, n] for target, n in rows.items()]
        if "clojure.string/includes? ?title" in query:
            # The search predicate. Modelled faithfully in two respects that
            # the tool's contract rests on: matching is case-SENSITIVE, and
            # the scan covers every entity carrying :block/title, so pages
            # and definitions match alongside blocks.
            needle = json.loads(
                query.split("includes? ?title ")[1].split(")]")[0])
            scope = params[0] if ":in $ ?page" in query else None
            hits = [
                e for e in self.graph.entities.values()
                if needle in str(e.get("title") or "")
                and (scope is None
                     or (e.get("page") or {}).get("id") == scope)
            ]
            if "(count ?block)" in query:
                return len(hits)
            return [self._with_page_titles(e) for e in hits]
        if "(or [?e :block/uuid" in query:
            # The bulk read: many UUIDs in one query, since a uuid cannot be
            # bound as a parameter. Each entity comes back with its whole
            # ancestor chain, because `{:block/parent ...}` recurses.
            wanted = set(re.findall(r'#uuid "([0-9a-fA-F-]+)"', query))
            return [self._with_parent_chain(e)
                    for e in self.graph.entities.values()
                    if e["uuid"] in wanted]
        if ":in $ [?parent ...]" in query:
            # One level of the stranded-descendant sweep: children of a whole
            # frontier at once, rather than a query per block.
            wanted = set(params[0])
            return [e for e in self.graph.entities.values()
                    if (e.get("parent") or {}).get("id") in wanted]
        if ":in $ [?class ...]" in query and "(pull ?e" in query:
            # The title inventory: every page and tag in one query, which is
            # what keeps the duplicate sweep at six calls whatever the graph
            # size.
            wanted = set(params[0])
            return [e for e in self.graph.entities.values()
                    if any(t.get("id") in wanted
                           for t in (e.get("tags") or []))]
        if ":find ?holder ?target" in query and ":block/alias" in query:
            # The graph-wide alias sweep, as PAIRS rather than entities. Both
            # spellings are matched because the live graph carried every
            # relation on :block/alias and none on :logseq.property/alias.
            pairs = []
            for entity in self.graph.entities.values():
                declared = (entity.get("alias")
                            or entity.get(":logseq.property/alias") or [])
                for ref in declared:
                    if isinstance(ref, dict) and ref.get("id") is not None:
                        pairs.append([entity["id"], ref["id"]])
            return pairs
        if "(pull ?holder" in query and ":block/alias ?target" in query:
            # Inbound: something declares this page as ITS alias. Both
            # attribute spellings are matched, because the code queries both
            # -- a guard written against one of them silently never fires.
            return [e for e in self.graph.entities.values()
                    if any(a.get("id") == params[0]
                           for a in (e.get("alias")
                                     or e.get(":logseq.property/alias")
                                     or []))]
        if "(pull ?alias" in query and ":block/alias ?alias" in query:
            # Outbound: the aliases this page declares.
            page = self.graph.by_id(params[0]) or {}
            declared = (page.get("alias")
                        or page.get(":logseq.property/alias") or [])
            wanted = {a.get("id") for a in declared if isinstance(a, dict)}
            return [e for e in self.graph.entities.values()
                    if e["id"] in wanted]
        if ":block/_parent" in query and "#uuid" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            root = self.graph.entities.get(uuid)
            if root is None:
                return None

            # Project only what the pull pattern names. Returning everything
            # regardless is what let a pattern requesting NO attributes pass
            # the suite while returning unusable nodes against the real API.
            wanted = set(re.findall(r":block/(\w+)|:db/(id)", query))
            keys = {a or b for a, b in wanted}

            def project(entity):
                node = {k: v for k, v in entity.items()
                        if k in keys or k.lstrip(":").split("/")[-1] in keys}
                if "id" in keys and "id" in entity:
                    node["id"] = entity["id"]
                return node

            def build(entity):
                node = project(entity)
                kids = self.graph.children(entity["id"])
                if kids:
                    node["_parent"] = [build(k) for k in kids]
                return node

            return build(root)
        if "#uuid" in query and "[?child :block/parent ?parent]" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            entity = self.graph.entities.get(uuid)
            return self.graph.children(entity["id"]) if entity else []
        if "[?child :block/parent ?parent]" in query:
            return self.graph.children(params[0])
        if "#uuid" in query and ":find (pull ?entity" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            found = self.graph.entities.get(uuid)
            return dict(found) if found else None
        if "(count ?page)" in query and ':block/title "' in query:
            # getPage returns one entity, so the resolver counts page-classed
            # holders of the title before trusting it. Answered here so both
            # branches of that guard are exercised rather than only the
            # fallback.
            title = query.split(':block/title "')[1].split('"')[0]
            return len([e for e in self.graph.entities.values()
                        if e.get("title") == title
                        and any(t.get("id") == params[0]
                                for t in e.get("tags", []))])
        if ':block/title "' in query and ":find [(pull ?" in query:
            title = query.split(':block/title "')[1].split('"')[0]
            matches = [e for e in self.graph.entities.values()
                       if e.get("title") == title]
            if "[?page :block/name]" in query:
                matches = [e for e in matches if e.get("name")]
            return matches
        if ":logseq.property/created-from-property" in query:
            return [e["id"] for e in self.graph.entities.values()
                    if e.get("page", {}).get("id") == params[0]
                    and e.get(":logseq.property/created-from-property")]
        if "(count ?b)" in query and "block/page" in query:
            return len([e for e in self.graph.entities.values()
                        if e.get("page", {}).get("id") == params[0]])
        if "(count ?e)" in query and ":block/refs ?target" in query:
            return len([e for e in self.graph.entities.values()
                        if any(r.get("id") == params[0]
                               for r in e.get("refs", []))])
        if "(count ?e)" in query and ":block/tags ?target" in query:
            return len([e for e in self.graph.entities.values()
                        if any(t.get("id") == params[0]
                               for t in e.get("tags", []))])
        if "(count ?e)" in query and "?e ?attr ?target" in query:
            total = 0
            for prop in self.graph.entities.values():
                ident = prop.get("ident")
                if not ident or not any(
                        t.get("id") == params[1] for t in prop.get("tags", [])):
                    continue
                for holder in self.graph.entities.values():
                    held = holder.get(ident)
                    ids = held if isinstance(held, list) else [held]
                    if any(isinstance(i, dict) and i.get("id") == params[0]
                           for i in ids):
                        total += 1
            return total
        if "[?e :block/refs ?target]" in query:
            return [e for e in self.graph.entities.values()
                    if any(r.get("id") == params[0]
                           for r in e.get("refs", []))]
        if "[?e :block/tags ?target]" in query:
            return [e for e in self.graph.entities.values()
                    if any(t.get("id") == params[0]
                           for t in e.get("tags", []))]
        if "[?e ?attr ?target]" in query:
            out = []
            for prop in self.graph.entities.values():
                ident = prop.get("ident")
                if not ident or not any(
                        t.get("id") == params[1] for t in prop.get("tags", [])):
                    continue
                for holder in self.graph.entities.values():
                    held = holder.get(ident)
                    ids = held if isinstance(held, list) else [held]
                    if any(isinstance(i, dict) and i.get("id") == params[0]
                           for i in ids):
                        out.append([holder, prop])
            return out
        if "[?block :block/page ?page]" in query:
            return [e for e in self.graph.entities.values()
                    if e.get("page", {}).get("id") == params[0]]
        return []


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def content(graph) -> VerifiedContent:
    return VerifiedContent(FakeClient(graph))  # type: ignore[arg-type]


# ------------------------------------------------------------ block writes

async def test_create_block_on_a_page_makes_a_top_level_block(graph, content):
    result = await content.create_block(graph.page["uuid"], "First block")

    assert result.verified is True
    created = result.verified_entities[0]
    assert created["parent"]["id"] == graph.page["id"]
    assert created["page"]["id"] == graph.page["id"]


async def test_create_block_on_a_block_nests_it(graph, content):
    """`insertBlock`'s target argument takes a page OR a block uuid. That is
    the whole reason nested creation does not need a separate route."""
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]

    child = (await content.create_block(
        parent["uuid"], "Child")).verified_entities[0]

    assert child["parent"]["id"] == parent["id"]
    # Ownership still resolves to the page, not to the parent block.
    assert child["page"]["id"] == graph.page["id"]


async def test_a_write_that_does_nothing_is_not_reported_as_success(graph):
    """HTTP 200 with a null body means both 'done' and 'nothing happened'.
    Without the read-back this would pass silently."""
    client = FakeClient(graph, write_effective=False)

    result = await VerifiedContent(client).create_block(  # type: ignore[arg-type]
        graph.page["uuid"], "Never created")

    assert result.verified is False
    assert graph.children(graph.page["id"]) == []


async def test_remove_block_deletes_the_whole_subtree(graph, content):
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    child = (await content.create_block(
        parent["uuid"], "Child")).verified_entities[0]

    result = await content.remove_block(parent["uuid"])

    assert result.verified is True
    assert parent["uuid"] not in graph.entities
    assert child["uuid"] not in graph.entities


async def test_remove_block_refuses_a_page_uuid(graph, content):
    with pytest.raises(ValueError, match="page, not a block"):
        await content.remove_block(graph.page["uuid"])


async def test_update_block_verifies_the_new_title(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Before")).verified_entities[0]

    result = await content.update_block(block["uuid"], "After")

    assert result.verified_entities[0]["title"] == "After"


# ---------------------------------------------------------------- batching





# ---------------------------------------------------------------- outlines

@pytest.mark.parametrize(
    ("outline", "expected"),
    [
        ("A\nB\n", [((0,), "A"), ((1,), "B")]),
        ("A\n    A1\n    A2\nB\n",
         [((0,), "A"), ((0, 0), "A1"), ((0, 1), "A2"), ((1,), "B")]),
        # Two-space indent works as well as four, provided it is consistent.
        ("A\n  A1\n    A1a\n",
         [((0,), "A"), ((0, 0), "A1"), ((0, 0, 0), "A1a")]),
    ],
)
def test_outline_paths_encode_the_tree(outline, expected):
    assert _parse_outline(outline) == expected


def test_outline_infers_its_indent_unit_from_the_first_indented_line():
    """An 8-space child is a single level, not a skipped one -- the unit is
    whatever the outline uses, not a fixed width."""
    assert _parse_outline("A\n        A1a\n") == [((0,), "A"), ((0, 0), "A1a")]


def test_outline_rejects_a_skipped_level():
    with pytest.raises(ValueError, match="more than one level"):
        _parse_outline("A\n    A1\n            A1a\n")


def test_outline_rejects_inconsistent_indentation():
    with pytest.raises(ValueError, match="not a multiple"):
        _parse_outline("A\n    A1\n      A1a\n")


async def test_outline_builds_a_real_tree(graph, content):
    await content.create_page_of_blocks(
        graph.page["uuid"],
        "Section 1\n    Alpha\n    Beta\nSection 2\n    Charly\n")

    sections = {e["title"]: e for e in graph.children(graph.page["id"])}
    assert set(sections) == {"Section 1", "Section 2"}
    assert {c["title"] for c in graph.children(sections["Section 1"]["id"])} == {
        "Alpha", "Beta"}
    assert {c["title"] for c in graph.children(sections["Section 2"]["id"])} == {
        "Charly"}


async def test_outline_allows_repeated_titles_in_different_branches(graph, content):
    await content.create_page_of_blocks(
        graph.page["uuid"], "Section 1\n    Notes\nSection 2\n    Notes\n")

    sections = {e["title"]: e for e in graph.children(graph.page["id"])}
    for section in sections.values():
        assert [c["title"] for c in graph.children(section["id"])] == ["Notes"]


async def test_outline_dry_run_writes_nothing(graph, content):
    result = await content.create_page_of_blocks(
        graph.page["uuid"], "A\n    A1\n", dry_run=True)

    assert result["dry_run"] is True
    assert result["levels"] == 2
    # One call per parent that has children, not 2d-1: the batch response
    # carries the created entities, so there is no read-back cycle.
    assert result["estimated_calls"] == 2
    assert graph.children(graph.page["id"]) == []


async def test_dry_run_validates_without_writing(graph, content):
    """A dry run is a real API call that validates the payload. It is not
    evidence the write will land: a graph carrying invalid entities passes
    validation and still rejects the transaction."""
    result = await content.create_block(
        graph.page["uuid"], "Never created", dry_run=True)

    assert result.verified_entities == ()
    assert graph.children(graph.page["id"]) == []
    assert result.validation is not None
    assert result.verified is False
    assert "nothing was written" in (result.diagnostic or "")


# ------------------------------------------------------------- write scope

async def test_title_scope_does_not_apply_to_block_content(graph):
    """LOGSEQ_WRITE_TITLE_PREFIXES scopes named entities. Applying it to block
    bodies would mean every sentence had to start with the prefix."""
    client = FakeClient(graph, policy=WriteAccessPolicy(
        title_prefixes=("MCP ",)))

    result = await VerifiedContent(client).create_block(  # type: ignore[arg-type]
        graph.page["uuid"], "ordinary block text")

    assert result.verified is True


async def test_entity_scope_denies_an_out_of_scope_target(graph):
    client = FakeClient(graph, policy=WriteAccessPolicy(
        entity_uuids=frozenset({"11111111-1111-4111-8111-111111111111"})))

    with pytest.raises(PermissionError):
        await VerifiedContent(client).create_block(  # type: ignore[arg-type]
            graph.page["uuid"], "Denied")


# ------------------------------------------------------------------ reads

async def test_get_page_uuid_resolves_a_unique_title(graph, content):
    """The fast path's positive branch: getPage answers, the uniqueness count
    confirms it, and the result is trusted without the full pull."""
    page = graph.add("Only One", None, None, name="only one",
                     tags=[PAGE_CLASS_ID])

    result = await content.get_page_uuid("Only One")

    assert result["found"] is True
    assert result["page_uuid"] == page["uuid"]


async def test_get_page_uuid_refuses_an_ambiguous_title(graph, content):
    graph.add("Twin", None, None, name="twin", tags=[PAGE_CLASS_ID])
    graph.add("Twin", None, None, name="twin", tags=[PAGE_CLASS_ID])

    result = await content.get_page_uuid("Twin")

    assert result["found"] is False
    assert "share this title" in result["reason"]
    assert len(result["candidates"]) == 2


async def test_get_page_uuid_reports_a_missing_page(graph, content):
    assert (await content.get_page_uuid("Nonexistent"))["found"] is False


async def test_get_block_uuid_returns_nested_blocks_too(graph, content):
    """The read walks `:block/parent` RECURSIVELY rather than scoping to
    `:block/page`, so depth is covered and a block whose owning page
    disagrees is still seen."""
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    await content.create_block(parent["uuid"], "Deep child")

    blocks = await content.get_block_uuid(graph.page["uuid"])

    assert {b["title"] for b in blocks} == {"Parent", "Deep child"}


async def test_get_page_rejects_a_block_uuid(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "A block")).verified_entities[0]

    result = await content.get_page(block["uuid"])

    assert result["found"] is False
    assert result["reason"] == "target is a block, not a page"


async def test_get_page_rejects_an_unknown_detail(graph, content):
    with pytest.raises(ValueError, match="detail must be one of"):
        await content.get_page(graph.page["uuid"], "everything")


async def test_find_block_reports_missing_rather_than_raising(content):
    result = await content.find_block("11111111-1111-4111-8111-111111111111")
    assert result == {
        "found": False,
        "block_uuid": "11111111-1111-4111-8111-111111111111",
        "block": None,
    }


# --------------------------------------------------------- guard consistency

async def test_create_page_refuses_a_duplicate_before_writing(graph, content):
    """Pre-write, like renamePage. Relying on Logseq to no-op surfaced the
    failure as a readback mismatch, which reads like a transport problem."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="already exists"):
        await verified.create_page("TEST-PAGE")

    assert not any(m == "logseq.DB.createPage" for m, _ in client.calls)


async def test_dry_run_does_not_report_verified_true(graph, content):
    """Nothing was written, so anything reading the boolean alone must not
    see a success."""
    result = await content.create_block(
        graph.page["uuid"], "Never created", dry_run=True)

    assert result.verified is False
    assert result.verified_entities == ()
    assert graph.children(graph.page["id"]) == []


async def test_update_block_envelope_carries_the_prior_title(graph, content):
    """Without this an edit is the one write whose previous state cannot be
    recovered from its own result."""
    block = (await content.create_block(
        graph.page["uuid"], "Before")).verified_entities[0]

    result = await content.update_block(block["uuid"], "After")

    assert result.previous_entities
    assert result.previous_entities[0]["title"] == "Before"
    assert result.verified_entities[0]["title"] == "After"


# ----------------------------------------------------- clearPage preservation

async def test_clear_page_preserves_property_value_blocks(graph, content):
    """Property values are materialized as blocks on the page. An unfiltered
    delete takes them, contradicting the tool's contract."""
    await content.create_block(graph.page["uuid"], "real content")
    graph.add("42", graph.page["id"], graph.page["id"],
              extra={":logseq.property/created-from-property": {"id": 900}})

    result = await content.clear_page(graph.page["uuid"])

    assert result.verified is True
    survivors = graph.children(graph.page["id"])
    assert [b["title"] for b in survivors] == ["42"]
    assert "preserved 1" in (result.diagnostic or "")


async def test_clear_page_on_a_page_of_only_value_blocks(graph, content):
    graph.add("42", graph.page["id"], graph.page["id"],
              extra={":logseq.property/created-from-property": {"id": 900}})

    result = await content.clear_page(graph.page["uuid"])

    assert result.verified is True
    assert len(graph.children(graph.page["id"])) == 1


# ------------------------------------------- orphan visibility and detection

async def test_reads_see_a_block_whose_page_pointer_is_wrong(graph, content):
    """The failure mode that made nested writes unauditable: a real child with
    :block/page pointing at its parent block. A page-scoped query cannot see
    it, so a clean read was reported over a broken page."""
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    # :block/page deliberately wrong, :block/parent correct.
    graph.add("Orphaned child", parent["id"], parent["id"])

    blocks = await content.get_block_uuid(graph.page["uuid"])

    assert "Orphaned child" in {b["title"] for b in blocks}


async def test_block_tree_sees_a_child_with_a_wrong_page_pointer(graph, content):
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    graph.add("Orphaned child", parent["id"], parent["id"])

    tree = await content.find_block_tree(parent["uuid"])

    assert tree["node_count"] == 2
    assert [c["title"] for c in tree["block"]["children"]] == ["Orphaned child"]


async def test_find_orphans_reports_the_disagreement(graph, content):
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    graph.add("Orphaned child", parent["id"], parent["id"])

    report = await content.find_orphans(graph.page["uuid"])

    assert len(report["orphans"]) == 1
    assert report["orphans"][0]["title"] == "Orphaned child"
    assert report["nested_pages"] == []


async def test_find_orphans_is_quiet_on_a_healthy_page(graph, content):
    await content.create_block(graph.page["uuid"], "Fine")

    report = await content.find_orphans(graph.page["uuid"])

    assert report["orphans"] == []
    assert "Every block" in report["diagnostic"]


# ------------------------------------------- ownership after nested creation

async def test_nested_create_sets_parent_and_page_independently(graph, content):
    """The bug this route change exists to fix. upsertNodes wrote its single
    `page-id` into both attributes, so a block parent produced a child owned
    by its parent rather than by the page."""
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]

    child = (await content.create_block(
        parent["uuid"], "Child")).verified_entities[0]

    assert child["parent"]["id"] == parent["id"]
    assert child["page"]["id"] == graph.page["id"]      # NOT parent["id"]


async def test_creation_is_rejected_when_ownership_is_wrong(graph):
    """Verifying the parent alone is what let the bug go unnoticed: the block
    appeared under the right parent while belonging to the wrong page."""
    class WrongPageClient(FakeClient):
        def _insert_block(self, target_uuid, title, options=None):
            target = self.graph.entities[target_uuid]
            # Deliberately wrong: ownership follows the parent.
            return dict(self.graph.add(title, target["id"], target["id"]))

    client = WrongPageClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    parent = (await verified.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]

    result = await verified.create_block(parent["uuid"], "Child")

    assert result.verified is False
    assert "owning page is wrong" in (result.diagnostic or "")


async def test_outline_costs_one_call_per_parent(graph, content):
    """Read-backs are gone: the batch response carries each created entity,
    so a parent's UUID is known before its children are inserted."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]

    await verified.create_page_of_blocks(
        graph.page["uuid"], "A\n    A1\n    A2\nB\n    B1\n")

    inserts = [m for m, _ in client.calls if m == "logseq.DB.insertBatchBlock"]
    # One for the top level, one for A's children, one for B's.
    assert len(inserts) == 3


async def test_outline_children_belong_to_the_page_at_every_depth(graph, content):
    await content.create_page_of_blocks(
        graph.page["uuid"], "A\n    A1\n        A1a\n")

    for block in graph.entities.values():
        if block.get("title") in {"A", "A1", "A1a"}:
            assert block["page"]["id"] == graph.page["id"]


# ------------------------------------------------------------------- moving

async def test_move_reparents_and_keeps_the_page(graph, content):
    first = (await content.create_block(
        graph.page["uuid"], "First")).verified_entities[0]
    second = (await content.create_block(
        graph.page["uuid"], "Second")).verified_entities[0]

    result = await content.move_block(second["uuid"], first["uuid"])

    assert result.verified is True
    moved = result.verified_entities[0]
    assert moved["parent"]["id"] == first["id"]
    assert moved["page"]["id"] == graph.page["id"]


async def test_move_carries_descendants(graph, content):
    """A subtree left pointing at the old page is the same invisible-orphan
    failure, one level down."""
    first = (await content.create_block(
        graph.page["uuid"], "First")).verified_entities[0]
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    child = (await content.create_block(
        parent["uuid"], "Child")).verified_entities[0]

    result = await content.move_block(parent["uuid"], first["uuid"])

    assert result.verified is True
    assert graph.entities[child["uuid"]]["page"]["id"] == graph.page["id"]


async def test_move_reports_a_silent_no_op(graph):
    """moveBlock returns null whether or not it did anything, so a write that
    changed nothing looks identical to one that worked."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    first = (await verified.create_block(
        graph.page["uuid"], "First")).verified_entities[0]
    second = (await verified.create_block(
        graph.page["uuid"], "Second")).verified_entities[0]
    client.write_effective = False

    result = await verified.move_block(second["uuid"], first["uuid"])

    assert result.verified is False
    assert "silent no-op" in (result.diagnostic or "")


async def test_move_detects_a_stranded_page_pointer(graph):
    class StrandingClient(FakeClient):
        def _move(self, block_uuid, target_uuid, options=None):
            # Reparents but leaves the owning page behind.
            block = self.graph.entities[block_uuid]
            target = self.graph.entities[target_uuid]
            block["parent"] = {"id": target["id"]}
            return None

    graph2 = FakeGraph()
    other = graph2.add("OTHER", None, None, name="other",
                       tags=[PAGE_CLASS_ID])
    client = StrandingClient(graph2)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    block = (await verified.create_block(
        graph2.page["uuid"], "Wanderer")).verified_entities[0]

    result = await verified.move_block(block["uuid"], other["uuid"])

    assert result.verified is False
    assert "owning page did not follow" in (result.diagnostic or "")


async def test_move_refuses_a_target_inside_its_own_subtree(graph, content):
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    child = (await content.create_block(
        parent["uuid"], "Child")).verified_entities[0]

    with pytest.raises(ValueError, match="own subtree"):
        await content.move_block(parent["uuid"], child["uuid"])


async def test_move_refuses_a_page_target_for_sibling_placement(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Block")).verified_entities[0]

    with pytest.raises(ValueError, match="page has no siblings"):
        await content.move_block(
            block["uuid"], graph.page["uuid"], placement="after")


async def test_move_rejects_an_unknown_placement(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Block")).verified_entities[0]

    with pytest.raises(ValueError, match="child, last-child, before, or after"):
        await content.move_block(
            block["uuid"], graph.page["uuid"], placement="first-child")


# ------------------------------------------------- last-child appends
#
# `child` prepends, which is Logseq's behaviour and is left alone. The cost of
# having no append was three pages of reversed content: moving siblings in
# source order with `child` puts each new arrival in front of the last.

async def test_child_placement_still_prepends(graph, content):
    """Pinned deliberately. `child` is Logseq's behaviour and callers depend
    on it; last-child was added rather than redefining this."""
    target = (await content.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]
    await content.create_block(target["uuid"], "Sitting there")
    arrival = (await content.create_block(
        graph.page["uuid"], "Arriving")).verified_entities[0]

    await content.move_block(arrival["uuid"], target["uuid"])

    children = [c["title"] for c in await content._children_of(target["uuid"])]
    assert children == ["Arriving", "Sitting there"]


async def test_last_child_appends_rather_than_prepending(graph, content):
    existing = (await content.create_block(
        graph.page["uuid"], "Already here")).verified_entities[0]
    target = (await content.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]
    await content.move_block(existing["uuid"], target["uuid"])
    arrival = (await content.create_block(
        graph.page["uuid"], "Arriving")).verified_entities[0]

    result = await content.move_block(
        arrival["uuid"], target["uuid"], placement="last-child")

    assert result.verified is True
    children = [c["title"] for c in await content._children_of(target["uuid"])]
    assert children == ["Already here", "Arriving"]


async def test_moving_a_sequence_with_last_child_preserves_source_order(
        graph, content):
    """The acceptance case. With `child` this same loop yields Third, Second,
    First -- silently, and only visible by reading the destination back."""
    source = [
        (await content.create_block(graph.page["uuid"], title)
         ).verified_entities[0]
        for title in ("First", "Second", "Third")
    ]
    destination = graph.add("DEST", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])

    for block in source:
        result = await content.move_block(
            block["uuid"], destination["uuid"], placement="last-child")
        assert result.verified is True

    moved = await content._children_of(destination["uuid"])
    assert [b["title"] for b in moved] == ["First", "Second", "Third"]


async def test_last_child_into_an_empty_parent(graph, content):
    """No existing sibling to anchor after, so it falls back to the plain
    child call -- where prepending and appending are the same thing."""
    target = (await content.create_block(
        graph.page["uuid"], "Empty target")).verified_entities[0]
    block = (await content.create_block(
        graph.page["uuid"], "Only child")).verified_entities[0]

    result = await content.move_block(
        block["uuid"], target["uuid"], placement="last-child")

    assert result.verified is True
    assert result.verified_entities[0]["parent"]["id"] == target["id"]


async def test_last_child_on_a_block_already_last_writes_nothing(graph):
    """The requested state already holds. Issuing the move would return null
    and read as a silent no-op, so it is reported from the READ instead."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    target = (await verified.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]
    block = (await verified.create_block(
        target["uuid"], "Last already")).verified_entities[0]

    result = await verified.move_block(
        block["uuid"], target["uuid"], placement="last-child")

    assert result.verified is True
    assert "already the last child" in (result.diagnostic or "")
    assert not any(m == "logseq.DB.moveBlock" for m, _ in client.calls)


async def test_last_child_reports_a_move_that_did_not_reach_the_end(graph):
    """A correct parent with the wrong order is exactly what `child` produces,
    so appending cannot be verified by the parent alone."""
    class PrependingClient(FakeClient):
        def _move(self, block_uuid, target_uuid, options=None):
            # Ignores the anchor and always prepends under the target's
            # parent, which is the behaviour last-child exists to avoid.
            block = self.graph.entities[block_uuid]
            anchor = self.graph.entities[target_uuid]
            parent = (anchor["id"] if anchor.get("name")
                      else anchor["parent"]["id"])
            first = self.graph.ordered_children(parent)
            block["parent"] = {"id": parent}
            block["page"] = {"id": self.graph.page["id"]}
            block["order"] = order_key(
                order_value(first[0].get("order")) / 2.0 if first else 1.0)
            return None

    client = PrependingClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    target = (await verified.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]
    await verified.create_block(target["uuid"], "Sibling")
    arrival = (await verified.create_block(
        graph.page["uuid"], "Arriving")).verified_entities[0]

    result = await verified.move_block(
        arrival["uuid"], target["uuid"], placement="last-child")

    assert result.verified is False
    assert "not last" in (result.diagnostic or "")


# ------------------------------------------------------- terse responses
#
# The verification path is unchanged: every write below still reads back and
# still reports the same `verified`. What terse removes is the SERIALISED
# payload, which for a write envelope is the block's own content twice over --
# once as previous_entities and once as verified_entities.

LONG_PROSE = (
    "The archivists of the lower vault kept their ledgers in a hand nobody "
    "living could read, which is how the inventory came to be argued about "
    "rather than consulted. " * 12
)


async def test_terse_move_does_not_echo_the_block_text(graph, content):
    """The acceptance case: moving a long block should cost roughly its
    identifiers, not its prose."""
    block = (await content.create_block(
        graph.page["uuid"], LONG_PROSE)).verified_entities[0]
    target = (await content.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]

    result = await content.move_block(block["uuid"], target["uuid"])
    terse = result.to_dict(verbose=False)

    assert terse["verified"] is True
    assert terse["uuid"] == block["uuid"]
    assert terse["parent"] == target["id"]
    assert terse["page"] == graph.page["id"]
    assert LONG_PROSE not in json.dumps(terse)
    # The verbose form is what it is being compared against, and it carries
    # the prose twice.
    assert json.dumps(result.to_dict()).count("archivists") >= 2
    assert len(json.dumps(terse)) < 400


async def test_terse_still_verifies_and_stays_actionable_on_failure(graph):
    """Shaping must not soften a failure. A caller cannot re-run a write to
    get detail, so the terse form keeps the observed entities -- as digests."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    first = (await verified.create_block(
        graph.page["uuid"], LONG_PROSE)).verified_entities[0]
    second = (await verified.create_block(
        graph.page["uuid"], "Second")).verified_entities[0]
    client.write_effective = False

    result = await verified.move_block(second["uuid"], first["uuid"])
    terse = result.to_dict(verbose=False)

    assert terse["verified"] is False
    assert "silent no-op" in terse["diagnostic"]
    assert terse["observed"][0]["uuid"] == second["uuid"]
    assert "title" not in terse["observed"][0]
    # The read-back happened either way.
    assert any(m == "logseq.DB.datascriptQuery" for m, _ in client.calls)


async def test_clear_page_keeps_the_record_of_what_it_destroyed(graph, content):
    """clearPage's payload is the only remaining copy of the deleted text, so
    verbose stays the default here and terse reduces it to a count."""
    await content.create_block(graph.page["uuid"], LONG_PROSE)

    result = await content.clear_page(graph.page["uuid"])

    assert LONG_PROSE in json.dumps(result.to_dict())
    terse = result.to_dict(verbose=False)
    assert LONG_PROSE not in json.dumps(terse)
    assert terse["previous_count"] == 1


async def test_terse_outline_returns_uuids_not_the_outline_back(graph, content):
    result = await content.create_page_of_blocks(
        graph.page["uuid"], "Alpha\n    Beta\n", verbose=False)

    assert result["created_count"] == 2
    assert all(set(block) <= {"uuid", "ident", "parent", "page", "order"}
               for block in result["created"])
    assert "Alpha" not in json.dumps(result)


async def test_terse_page_creation_reports_no_parent(graph, content):
    """A page has no parent, and the absence is information -- so the key is
    present and null rather than missing."""
    result = await content.create_page("Fresh Page")
    terse = result.to_dict(verbose=False)

    assert terse["verified"] is True
    assert terse["parent"] is None
    assert "order" not in terse


# ------------------------------------------------ counted page listings
#
# Triaging which journals hold content cost one pageStats per journal -- about
# fifty calls to answer one question. The counted listing is four queries
# however many journals there are, so the tests below care about the CALL
# COUNT as much as the numbers.

def add_journal(graph, day: int, title: str):
    return graph.add(title, None, None, name=title.lower(),
                     tags=[PAGE_CLASS_ID], extra={"journal-day": day})


async def test_journal_listing_stays_cheap_by_default(graph, content):
    """The bare listing keeps its old shape -- a list, no counts -- so adding
    the option does not make the cheap call expensive."""
    add_journal(graph, 20260101, "Jan 1st, 2026")

    journals = await content.list_journals()

    assert isinstance(journals, list)
    assert [j["title"] for j in journals] == ["Jan 1st, 2026"]
    assert "own_blocks" not in journals[0]


async def test_journal_counts_cost_four_calls_not_one_per_journal(graph):
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    add_journal(graph, 20260101, "Jan 1st, 2026")
    second = add_journal(graph, 20260102, "Jan 2nd, 2026")
    add_journal(graph, 20260103, "Jan 3rd, 2026")
    graph.add("real content", second["id"], second["id"])
    graph.add("", second["id"], second["id"])
    referrer = graph.add("points at it", graph.page["id"], graph.page["id"])
    referrer["refs"] = [{"id": second["id"]}]

    result = await verified.list_journals(with_counts=True)

    queries = [m for m, _ in client.calls if m == "logseq.DB.datascriptQuery"]
    assert len(queries) == 4      # the listing, then three aggregates
    assert result["total"] == 3
    assert result["truncated"] is False

    rows = {r["title"]: r for r in result["journals"]}
    assert rows["Jan 2nd, 2026"]["own_blocks"] == 2
    assert rows["Jan 2nd, 2026"]["content_blocks"] == 1
    assert rows["Jan 2nd, 2026"]["refs"] == 1
    # A journal with nothing on it is absent from every join, and the absence
    # has to read as zero rather than as a missing key.
    assert rows["Jan 3rd, 2026"]["own_blocks"] == 0
    assert rows["Jan 3rd, 2026"]["refs"] == 0


async def test_journals_come_back_newest_first(graph, content):
    """So a cap drops the oldest rather than an arbitrary slice."""
    add_journal(graph, 20260101, "Jan 1st, 2026")
    add_journal(graph, 20260307, "Mar 7th, 2026")
    add_journal(graph, 20251114, "Nov 14th, 2025")

    result = await content.list_journals(with_counts=True)

    assert [r["journal-day"] for r in result["journals"]] == [
        20260307, 20260101, 20251114]


async def test_counted_listing_reports_truncation(graph, content):
    for day in (20260101, 20260102, 20260103):
        add_journal(graph, day, f"Day {day}")

    result = await content.list_journals(with_counts=True, limit=2)

    assert result["counted"] == 2
    assert result["total"] == 3
    assert result["truncated"] is True
    assert "pageStats" in result["diagnostic"]


async def test_a_meaningless_limit_is_refused(content):
    with pytest.raises(ValueError, match="positive integer"):
        await content.list_journals(limit=0)


async def test_counted_page_listing_excludes_recycled_pages(graph, content):
    """Recycled pages keep the Page class, so they would otherwise appear
    live -- and a triage listing is exactly where that would mislead."""
    graph.add("Gone", None, None, name="gone", tags=[PAGE_CLASS_ID],
              extra={":logseq.property/deleted-at": 1})
    await content.create_block(graph.page["uuid"], "something")

    result = await content.list_pages(with_counts=True)

    rows = {r["title"]: r for r in result["pages"]}
    assert "Gone" not in rows
    assert rows["TEST-PAGE"]["content_blocks"] == 1


# ------------------------------------------------------ page migration
#
# The journal-to-page workflow. The tool is mechanical on purpose: which
# blocks belong on which page was the irreducibly human part of the job, and
# these tests pin that the tool does not attempt it.

async def test_a_journal_migrates_in_order(graph, content):
    journal = graph.add("Feb 10th, 2026", None, None, name="feb 10th, 2026",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Chapter One", None, None, name="chapter one",
                            tags=[PAGE_CLASS_ID])
    for n in range(4):
        graph.add(f"Para {n}", journal["id"], journal["id"])

    result = await content.migrate_page(
        journal["uuid"], destination["uuid"])

    assert result["verified"] is True
    assert result["remaining"] == 0
    assert result["order_preserved"] is True
    arrived = [b["title"]
               for b in await content._children_of(destination["uuid"])]
    assert arrived == ["Para 0", "Para 1", "Para 2", "Para 3"]


async def test_a_dry_run_writes_nothing_and_returns_the_plan(graph):
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    journal = graph.add("Feb 11th", None, None, name="feb 11th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Chapter", None, None, name="chapter",
                            tags=[PAGE_CLASS_ID])
    graph.add("A long paragraph " * 10, journal["id"], journal["id"])

    result = await verified.migrate_page(
        journal["uuid"], destination["uuid"], dry_run=True)

    assert result["verified"] is False
    assert "DRY RUN" in result["diagnostic"]
    assert len(result["planned"]) == 1
    # A preview, not the block: enough to recognise it.
    assert len(result["planned"][0]["preview"]) <= 81
    assert not any(m == "logseq.DB.moveBlock" for m, _ in client.calls)
    assert await verified._children_of(destination["uuid"]) == []


async def test_the_substring_selector_is_the_callers_rule(graph, content):
    """The only selection this tool does. No clustering, no similarity --
    placement judgement stays with the person."""
    journal = graph.add("Feb 10th", None, None, name="feb 10th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Mechanics", None, None, name="mechanics",
                            tags=[PAGE_CLASS_ID])
    graph.add("[mechanics] dice pools", journal["id"], journal["id"])
    graph.add("a diary entry", journal["id"], journal["id"])
    graph.add("[mechanics] modifiers", journal["id"], journal["id"])

    result = await content.migrate_page(
        journal["uuid"], destination["uuid"], contains="[mechanics]")

    assert result["verified"] is True
    assert result["remaining"] == 1
    arrived = [b["title"]
               for b in await content._children_of(destination["uuid"])]
    assert arrived == ["[mechanics] dice pools", "[mechanics] modifiers"]
    left = [b["title"] for b in await content._children_of(journal["uuid"])]
    assert left == ["a diary entry"]


async def test_the_selector_is_case_sensitive(graph, content):
    """Consistent with searchBlocks, and deliberate: a selector that guesses
    at intent is the thing this tool refuses to be."""
    journal = graph.add("Feb 10th", None, None, name="feb 10th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Dest", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])
    graph.add("Mechanics note", journal["id"], journal["id"])

    result = await content.migrate_page(
        journal["uuid"], destination["uuid"], contains="mechanics")

    assert result["planned"] == []
    assert result["remaining"] == 1
    assert "none containing" in result["diagnostic"]


async def test_nested_blocks_travel_with_their_parent(graph, content):
    """Only top-level blocks are selected, because a move carries the
    subtree -- selecting a child directly would tear it out of context."""
    journal = graph.add("Feb 10th", None, None, name="feb 10th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Dest", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])
    parent = graph.add("Section", journal["id"], journal["id"])
    graph.add("nested detail", parent["id"], journal["id"])

    result = await content.migrate_page(
        journal["uuid"], destination["uuid"])

    assert len(result["planned"]) == 1        # the parent only
    assert result["verified"] is True
    moved = await content._children_of(destination["uuid"])
    assert [b["title"] for b in moved] == ["Section"]
    assert [b["title"] for b in await content._children_of(parent["uuid"])] \
        == ["nested detail"]


async def test_remaining_is_read_back_not_subtracted(graph):
    """Arithmetic would agree with itself even if a move silently did
    nothing, which is the failure this whole server is built around."""
    client = FakeClient(graph)
    client.write_effective = False
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    journal = graph.add("Feb 10th", None, None, name="feb 10th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Dest", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])
    graph.add("Para", journal["id"], journal["id"])

    result = await verified.migrate_page(
        journal["uuid"], destination["uuid"])

    assert result["verified"] is False
    assert result["remaining"] == 1           # still on the source


async def test_an_empty_source_is_not_an_error(graph, content):
    journal = graph.add("Feb 12th", None, None, name="feb 12th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Dest", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])

    result = await content.migrate_page(
        journal["uuid"], destination["uuid"])

    assert result["verified"] is True
    assert result["planned"] == []
    assert "the page is empty" in result["diagnostic"]


async def test_migrating_a_page_onto_itself_is_refused(graph, content):
    journal = graph.add("Feb 10th", None, None, name="feb 10th",
                        tags=[PAGE_CLASS_ID])

    with pytest.raises(ValueError, match="same page"):
        await content.migrate_page(journal["uuid"], journal["uuid"])


async def test_a_blank_selector_is_refused(graph, content):
    journal = graph.add("Feb 10th", None, None, name="feb 10th",
                        tags=[PAGE_CLASS_ID])
    destination = graph.add("Dest", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])

    with pytest.raises(ValueError, match="contains cannot be blank"):
        await content.migrate_page(
            journal["uuid"], destination["uuid"], contains="   ")


# ------------------------------------------------------- splitting a block
#
# updateBlock sets a title and nothing else, so a heading fused onto the tail
# of a pasted paragraph could not be promoted out of it. The safety property
# is the ORDER: parts created first, original truncated last, so a mid-way
# failure duplicates prose rather than losing it.

FUSED = ("Both dice succeed [1]\n\n**Modifiers:**\n\n**Equipment:**")


async def test_a_fused_heading_splits_into_ordered_blocks(graph, content):
    """The acceptance case: one call frees every trapped heading, in order,
    with no text lost."""
    block = (await content.create_block(
        graph.page["uuid"], FUSED)).verified_entities[0]

    result = await content.split_block(block["uuid"], delimiter="\n\n")

    assert result["verified"] is True
    assert result["parts"] == 3
    titles = [b["title"]
              for b in await content._children_of(graph.page["uuid"])]
    assert titles == [
        "Both dice succeed [1]",
        "**Modifiers:**",
        "**Equipment:**",
    ]


async def test_no_text_is_lost_in_a_split(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], FUSED)).verified_entities[0]

    await content.split_block(block["uuid"], delimiter="\n\n")

    titles = [b["title"]
              for b in await content._children_of(graph.page["uuid"])]
    assert "\n\n".join(titles) == FUSED


async def test_an_offset_split_preserves_text_exactly(graph, content):
    """An exact index is a claim about exact text, so unlike a delimiter
    split nothing is stripped."""
    block = (await content.create_block(
        graph.page["uuid"], "HeadTail")).verified_entities[0]

    result = await content.split_block(block["uuid"], offset=4)

    assert result["verified"] is True
    titles = [b["title"]
              for b in await content._children_of(graph.page["uuid"])]
    assert titles == ["Head", "Tail"]


async def test_the_original_is_truncated_last(graph):
    """THE SAFETY PROPERTY. If the truncation fails, the tail must still
    exist in the original -- duplicated prose is repairable, lost prose is
    not."""
    class UpdateFailsClient(FakeClient):
        def _update_block(self, block_uuid, title):
            return None          # reports success, changes nothing

    client = UpdateFailsClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    block = (await verified.create_block(
        graph.page["uuid"], "Head\n\nTail")).verified_entities[0]

    result = await verified.split_block(block["uuid"], delimiter="\n\n")

    assert result["verified"] is False
    assert "NO TEXT IS LOST" in result["diagnostic"]
    # The created part is named so it can be removed to undo.
    assert result["created"][0]["uuid"]
    # And the original still holds the whole text.
    assert graph.entities[block["uuid"]]["title"] == "Head\n\nTail"


async def test_a_missing_delimiter_is_refused_before_any_write(graph):
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    block = (await verified.create_block(
        graph.page["uuid"], "One block")).verified_entities[0]
    client.calls.clear()

    with pytest.raises(ValueError, match="does not occur"):
        await verified.split_block(block["uuid"], delimiter="\n\n")

    assert not any(m for m, _ in client.calls
                   if m != "logseq.DB.datascriptQuery")


async def test_a_split_producing_an_empty_part_is_refused(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Head\n\n\n\nTail")).verified_entities[0]

    with pytest.raises(ValueError, match="empty part"):
        await content.split_block(block["uuid"], delimiter="\n\n")


async def test_an_offset_at_either_end_is_refused(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Whole")).verified_entities[0]

    with pytest.raises(ValueError, match="offset must be between"):
        await content.split_block(block["uuid"], offset=0)
    with pytest.raises(ValueError, match="offset must be between"):
        await content.split_block(block["uuid"], offset=5)


async def test_both_or_neither_split_argument_is_refused(graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Head\n\nTail")).verified_entities[0]

    with pytest.raises(ValueError, match="exactly one of offset or"):
        await content.split_block(block["uuid"])
    with pytest.raises(ValueError, match="exactly one of offset or"):
        await content.split_block(
            block["uuid"], offset=2, delimiter="\n\n")


async def test_a_part_logseq_would_truncate_is_refused(graph, content):
    """A part beginning a line with '- ' would be truncated on write, so the
    split is refused rather than performed and lost."""
    block = (await content.create_block(
        graph.page["uuid"], "Head")).verified_entities[0]
    graph.entities[block["uuid"]]["title"] = "Head\n\nTail\n- bullet"

    with pytest.raises(ValueError, match="truncates the block"):
        await content.split_block(block["uuid"], delimiter="\n\n")


async def test_splitting_a_nested_block_keeps_it_nested(graph, content):
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    block = (await content.create_block(
        parent["uuid"], "Head\n\nTail")).verified_entities[0]

    result = await content.split_block(block["uuid"], delimiter="\n\n")

    assert result["verified"] is True
    titles = [b["title"] for b in await content._children_of(parent["uuid"])]
    assert titles == ["Head", "Tail"]


# ------------------------------------------------ duplicate title triage
#
# 330 titles were once read by eye for near-matches. These tests care about
# two things the eye also got wrong: that an ALIAS is not reported as a
# duplicate, and that a plural is not assumed to be a mistake.

def add_page(graph, title, *, recycled=False):
    extra = {":logseq.property/deleted-at": 1} if recycled else None
    return graph.add(title, None, None, name=title.lower(),
                     tags=[PAGE_CLASS_ID], extra=extra)


async def test_an_exact_duplicate_is_grouped_as_a_dead_stub(graph, content):
    real = add_page(graph, "Creativity")
    await content.create_block(real["uuid"], "actual content")
    stub = add_page(graph, "Creativity")

    result = await content.find_duplicate_titles()

    group = next(g for g in result["groups"] if "Creativity" in g["titles"])
    assert group["classification"] == "dead_stub"
    assert group["rank"] == 0
    members = {m["uuid"]: m for m in group["members"]}
    assert members[real["uuid"]]["content_blocks"] == 1
    assert members[stub["uuid"]]["content_blocks"] == 0
    assert members[stub["uuid"]]["refs"] == 0


async def test_an_alias_is_flagged_as_an_alias_not_a_duplicate(
        graph, content):
    """The acceptance case, and the trap. `Abilties` is empty, referenced
    once and one character off `Abilities` -- indistinguishable from a dead
    stub by counts alone. Recommending its deletion is unrepairable."""
    real = add_page(graph, "Abilities")
    await content.create_block(real["uuid"], "content")
    alias = add_page(graph, "Abilties")
    owner = add_page(graph, "Attribute")
    owner["alias"] = [{"id": alias["id"]}]

    result = await content.find_duplicate_titles(normalize="fuzzy")

    group = next(g for g in result["groups"] if "Abilties" in g["titles"])
    assert group["classification"] == "alias"
    assert group["rank"] == 5          # ranked below everything actionable
    assert "NOT a duplicate" in group["reading"]
    assert next(m for m in group["members"]
                if m["uuid"] == alias["uuid"])["alias"] is True


async def test_a_typo_pair_is_found_only_with_fuzzy(graph, content):
    add_page(graph, "Persuade")
    add_page(graph, "Presuade")

    loose = await content.find_duplicate_titles()
    fuzzy = await content.find_duplicate_titles(normalize="fuzzy")

    assert loose["groups"] == []
    assert any("Presuade" in g["titles"] for g in fuzzy["groups"])


async def test_punctuation_and_case_fold_without_fuzzy(graph, content):
    add_page(graph, "Loom-Weaver")
    add_page(graph, "loom weaver")

    result = await content.find_duplicate_titles()

    assert len(result["groups"]) == 1


async def test_exact_mode_does_not_fold_punctuation(graph, content):
    add_page(graph, "Loom-Weaver")
    add_page(graph, "Loom Weaver")

    result = await content.find_duplicate_titles(normalize="exact")

    assert result["groups"] == []


async def test_two_pages_with_content_are_a_human_decision(graph, content):
    left = add_page(graph, "Mechanics")
    right = add_page(graph, "mechanics")
    await content.create_block(left["uuid"], "one")
    await content.create_block(right["uuid"], "two")

    result = await content.find_duplicate_titles()

    group = result["groups"][0]
    assert group["classification"] == "genuine_split"
    assert group["rank"] == 4
    assert "human decision" in group["reading"]


async def test_a_referenced_empty_page_is_a_split_identity(graph, content):
    real = add_page(graph, "Dawnspire")
    await content.create_block(real["uuid"], "content")
    empty = add_page(graph, "dawnspire")
    referrer = graph.add("points at it", graph.page["id"], graph.page["id"])
    referrer["refs"] = [{"id": empty["id"]}]

    result = await content.find_duplicate_titles()

    group = next(g for g in result["groups"] if "Dawnspire" in g["titles"])
    assert group["classification"] == "split_identity"
    assert "retitleOverDuplicate" in group["reading"]


async def test_a_tag_clashing_with_a_page_is_reported(graph, content):
    """Tags and pages share one title space, so this is a real clash."""
    add_page(graph, "Industrial")
    graph.add("Industrial", None, None, ident=":user.class/industrial-x",
              tags=[TAG_CLASS_ID])

    result = await content.find_duplicate_titles()

    group = result["groups"][0]
    assert {m["kind"] for m in group["members"]} == {"page", "tag"}


async def test_a_recycled_page_holding_a_title_is_reported(graph, content):
    """It still holds the title, which is what surprises people."""
    add_page(graph, "Creativity")
    add_page(graph, "Creativity", recycled=True)

    included = await content.find_duplicate_titles()
    excluded = await content.find_duplicate_titles(include_recycled=False)

    assert any(m["recycled"] for m in included["groups"][0]["members"])
    assert excluded["groups"] == []


async def test_nothing_is_written(graph):
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    add_page(graph, "Twin")
    add_page(graph, "Twin")

    await verified.find_duplicate_titles(normalize="fuzzy")

    assert not any(m for m, _ in client.calls
                   if m != "logseq.DB.datascriptQuery")


async def test_a_bad_normalize_value_is_refused(content):
    with pytest.raises(ValueError, match="exact, loose, or fuzzy"):
        await content.find_duplicate_titles(normalize="aggressive")


def test_the_plural_fold_is_conservative():
    """A wrong fold silently merges two real pages; a missed one only reports
    them separately. So the rule errs toward missing."""
    assert grouping_key("Threads") == "thread"
    assert grouping_key("Thread") == "thread"
    # Double-s words keep their ending, but their plural still folds to it.
    assert grouping_key("Class") == "class"
    assert grouping_key("Classes") == "class"
    # Short words are left alone rather than mangled.
    assert grouping_key("Its") == "its"


def test_exact_mode_interprets_nothing():
    """`exact` promises identical titles only. It once folded case and
    punctuation anyway, because the mode was passed as a boolean that only
    switched off the plural rule."""
    assert grouping_key("Loom-Weaver", mode="exact") == "Loom-Weaver"
    assert (grouping_key("Loom-Weaver", mode="exact")
            != grouping_key("Loom Weaver", mode="exact"))
    assert (grouping_key("Creativity", mode="exact")
            != grouping_key("creativity", mode="exact"))
    # And loose still folds all three.
    assert grouping_key("Loom-Weaver") == grouping_key("loom weaver")


def test_edit_distance_exits_early_on_hopeless_pairs():
    assert within_edit_distance("creativity", "creatvity", 1)
    assert not within_edit_distance("creativity", "mechanics", 2)
    assert not within_edit_distance("cat", "category", 1)


# ------------------------------------------------ title availability
#
# The two resolution paths disagree on purpose, and the disagreement caused
# real damage: a page recycled in the belief the title would be released,
# found mid-repair to be still holding it, with recycling not reversible.
# getPageUUID is recycle-BLIND so a link never resolves to a deleted page;
# the writers are recycle-AWARE because the entity survives. Both are right.
# These tests pin that isTitleAvailable answers with the WRITERS' rule.

async def test_a_free_title_is_available(content):
    result = await content.is_title_available("Nothing Holds This")

    assert result["available"] is True
    assert result["held_by"] == []


async def test_a_live_page_holds_its_title(graph, content):
    result = await content.is_title_available("TEST-PAGE")

    assert result["available"] is False
    assert result["held_by"][0]["kind"] == "page"
    assert result["held_by"][0]["uuid"] == graph.page["uuid"]
    assert result["held_by"][0]["recycled"] is False


async def test_a_recycled_page_still_holds_its_title(graph, content):
    """The acceptance case. getPageUUID reports this same title as not found,
    which is correct and is exactly what misled a repair."""
    recycled = graph.add("Creativity", None, None, name="creativity",
                         tags=[PAGE_CLASS_ID],
                         extra={":logseq.property/deleted-at": 1758240000})

    result = await content.is_title_available("Creativity")

    assert result["available"] is False
    assert result["held_by"][0]["recycled"] is True
    assert result["held_by"][0]["uuid"] == recycled["uuid"]
    assert "RECYCLED" in result["diagnostic"]
    # And the divergence it exists to explain, in the same graph state.
    assert (await content.get_page_uuid("Creativity"))["found"] is False


async def test_availability_agrees_with_what_the_writer_does(graph, content):
    """The guarantee that matters: one rule, asked twice. If these ever
    disagree, the tool is worse than useless -- it would license the write
    that then fails."""
    graph.add("Creativity", None, None, name="creativity",
              tags=[PAGE_CLASS_ID],
              extra={":logseq.property/deleted-at": 1758240000})

    assert (await content.is_title_available("Creativity"))[
        "available"] is False
    with pytest.raises(ValueError, match="already exists"):
        await content.create_page("Creativity")


async def test_a_block_holds_a_title_too(graph, content):
    """Surprising, and the writers' rule, so it is reported: a plain block
    with this title makes createPage refuse."""
    await content.create_block(graph.page["uuid"], "Just A Block")

    result = await content.is_title_available("Just A Block")

    assert result["available"] is False
    assert result["held_by"][0]["kind"] == "block"
    assert result["held_by"][0]["recycled"] is False


async def test_a_tag_holding_a_title_is_reported_as_a_tag(graph, content):
    """createPage refuses a title a tag holds, so the kind has to be named --
    the remedy for a tag clash is different from the remedy for a page one."""
    graph.add("Shared Name", None, None, ident=":user.class/shared-abc",
              tags=[TAG_CLASS_ID])

    result = await content.is_title_available("Shared Name")

    assert result["available"] is False
    assert result["held_by"][0]["kind"] == "tag"


async def test_every_holder_of_a_duplicated_title_is_reported(graph, content):
    """held_by is a list because untangling duplicates is one of the reasons
    to call this, and reporting one of two holders would hide the problem."""
    graph.add("Twin", None, None, name="twin", tags=[PAGE_CLASS_ID])
    graph.add("Twin", None, None, name="twin", tags=[PAGE_CLASS_ID])

    result = await content.is_title_available("Twin")

    assert len(result["held_by"]) == 2


# ------------------------------------------- retitling over a duplicate
#
# Two renames fix a typo pair losslessly, because references are by UUID and
# neither page's wiring is touched. The documented route was one updateBlock
# per referring block plus a deletePage plus a repairLinks sweep -- more
# calls, and lossy. The guards are the whole risk surface: an alias holder
# looks exactly like an abandoned typo.

async def test_a_typo_pair_is_fixed_in_two_renames(graph, content):
    """The acceptance case: Creatvity -> Creativity, zero block edits."""
    keeper = graph.add("Creatvity", None, None, name="creatvity",
                       tags=[PAGE_CLASS_ID])
    holder = graph.add("Creativity", None, None, name="creativity",
                       tags=[PAGE_CLASS_ID])
    referrer = graph.add("see [[uuid]]", graph.page["id"], graph.page["id"])
    referrer["refs"] = [{"id": keeper["id"]}]

    result = await content.retitle_over_duplicate(
        keeper["uuid"], "Creativity")

    assert result["verified"] is True
    assert result["renamed"]["title"] == "Creativity"
    assert result["renamed"]["previous_title"] == "Creatvity"
    assert result["parked"]["uuid"] == holder["uuid"]
    assert result["parked"]["title"] == "Creativity (parked)"
    assert result["references"] == {"from": 1, "holder": 0}
    # Nothing edited a block, and the reference still points at the same UUID.
    assert graph.entities[referrer["uuid"]]["refs"] == [{"id": keeper["id"]}]


async def test_it_works_when_the_holder_is_recycled(graph, content):
    """Renaming a recycled page is what releases its title -- the whole
    reason this beats recycling and waiting for the title to free up, which
    never happens."""
    keeper = graph.add("Presuade", None, None, name="presuade",
                       tags=[PAGE_CLASS_ID])
    graph.add("Persuade", None, None, name="persuade", tags=[PAGE_CLASS_ID],
              extra={":logseq.property/deleted-at": 1758240000})

    result = await content.retitle_over_duplicate(keeper["uuid"], "Persuade")

    assert result["verified"] is True
    assert result["parked"]["recycled"] is True
    assert (await content.get_page_uuid("Persuade"))[
        "page_uuid"] == keeper["uuid"]


async def test_a_content_bearing_holder_is_refused(graph, content):
    keeper = graph.add("Activty", None, None, name="activty",
                       tags=[PAGE_CLASS_ID])
    holder = graph.add("Activity", None, None, name="activity",
                       tags=[PAGE_CLASS_ID])
    graph.add("real content here", holder["id"], holder["id"])

    result = await content.retitle_over_duplicate(keeper["uuid"], "Activity")

    assert result["verified"] is False
    assert result["parked"] is None
    assert "content block" in result["diagnostic"]
    # Refused with the evidence, not just a refusal.
    assert result["references"] == {"from": 0, "holder": 0}
    assert graph.entities[holder["uuid"]]["title"] == "Activity"


async def test_an_alias_holder_is_refused(graph, content):
    """The near-miss this guard exists for: a page that reads as an abandoned
    typo and is a working alias of the page it resembles."""
    keeper = graph.add("Abilities", None, None, name="abilities",
                       tags=[PAGE_CLASS_ID])
    holder = graph.add("Abilties", None, None, name="abilties",
                       tags=[PAGE_CLASS_ID])
    attribute = graph.add("Attribute", None, None, name="attribute",
                          tags=[PAGE_CLASS_ID])
    attribute["alias"] = [{"id": holder["id"]}]

    result = await content.retitle_over_duplicate(keeper["uuid"], "Abilties")

    assert result["verified"] is False
    assert "ALIAS" in result["diagnostic"]
    assert graph.entities[holder["uuid"]]["title"] == "Abilties"


async def test_direction_is_reported_even_when_it_looks_wrong(graph, content):
    """Which side keeps the title is the caller's call, so a holder with more
    inbound references is not refused -- but both counts come back, because
    that is the evidence the choice needed."""
    keeper = graph.add("Cheet Sheet", None, None, name="cheet sheet",
                       tags=[PAGE_CLASS_ID])
    holder = graph.add("Cheat Sheet", None, None, name="cheat sheet",
                       tags=[PAGE_CLASS_ID])
    for n in range(3):
        referrer = graph.add(f"ref {n}", graph.page["id"], graph.page["id"])
        referrer["refs"] = [{"id": holder["id"]}]

    result = await content.retitle_over_duplicate(
        keeper["uuid"], "Cheat Sheet")

    assert result["verified"] is True
    assert result["references"] == {"from": 0, "holder": 3}


async def test_a_free_title_needs_no_parking(graph, content):
    keeper = graph.add("Loom-Weaver", None, None, name="loom-weaver",
                       tags=[PAGE_CLASS_ID])

    result = await content.retitle_over_duplicate(
        keeper["uuid"], "Loom Weaver")

    assert result["verified"] is True
    assert result["parked"] is None
    assert "nothing was parked" in result["diagnostic"]


async def test_a_taken_parking_title_is_refused_before_any_write(
        graph, content):
    keeper = graph.add("Flexability", None, None, name="flexability",
                       tags=[PAGE_CLASS_ID])
    holder = graph.add("Flexibility", None, None, name="flexibility",
                       tags=[PAGE_CLASS_ID])
    graph.add("Flexibility (parked)", None, None,
              name="flexibility (parked)", tags=[PAGE_CLASS_ID])

    result = await content.retitle_over_duplicate(
        keeper["uuid"], "Flexibility")

    assert result["verified"] is False
    assert "park_suffix" in result["diagnostic"]
    assert graph.entities[holder["uuid"]]["title"] == "Flexibility"


async def test_a_partial_application_reports_how_to_undo(graph):
    """Not atomic, so the failure mode has to be legible: the title was
    freed, the second rename did not land, and the parked page is sitting
    under a name nobody chose."""
    class SecondRenameFailsClient(FakeClient):
        def __init__(self, graph):
            super().__init__(graph)
            self.renames = 0

        def _rename(self, page_uuid, new_title):
            self.renames += 1
            if self.renames > 1:
                return None      # reports success, does nothing
            return super()._rename(page_uuid, new_title)

    keeper = graph.add("Creatvity", None, None, name="creatvity",
                       tags=[PAGE_CLASS_ID])
    holder = graph.add("Creativity", None, None, name="creativity",
                       tags=[PAGE_CLASS_ID])
    client = SecondRenameFailsClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]

    result = await verified.retitle_over_duplicate(
        keeper["uuid"], "Creativity")

    assert result["verified"] is False
    assert "PARTIALLY APPLIED" in result["diagnostic"]
    assert holder["uuid"] in result["diagnostic"]
    assert result["parked"]["title"] == "Creativity (parked)"
    assert graph.entities[keeper["uuid"]]["title"] == "Creatvity"


# ------------------------------------------------- moving a list of blocks
#
# Flat sibling runs are the dominant shape: one journal day was 115 of them.
# One call per block was the wrong granularity, and a loop over move_block
# would be ~8 calls each. These tests care about call count and order as much
# as about the blocks arriving.

async def test_a_chapter_moves_in_one_call_in_order(graph):
    """The acceptance case: 31 blocks, one call, source order preserved."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    titles = [f"Paragraph {n:02d}" for n in range(31)]
    blocks = [graph.add(t, graph.page["id"], graph.page["id"]) for t in titles]
    destination = graph.add("CHAPTER", None, None, name="chapter",
                            tags=[PAGE_CLASS_ID])

    result = await verified.move_blocks(
        [b["uuid"] for b in blocks], destination["uuid"])

    assert result["verified"] is True
    assert result["summary"] == {
        "requested": 31, "attempted": 31, "landed": 31,
        "failed": 0, "not_attempted": 0}
    assert result["order_preserved"] is True
    arrived = await verified._children_of(destination["uuid"])
    assert [b["title"] for b in arrived] == titles


async def test_the_bulk_move_does_not_cost_a_read_per_block(graph):
    """Two calls per block plus a fixed handful. A loop over move_block would
    be roughly eight each, which is what made chained moves unaffordable."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    blocks = [graph.add(f"B{n}", graph.page["id"], graph.page["id"])
              for n in range(10)]
    destination = graph.add("DEST", None, None, name="dest",
                           tags=[PAGE_CLASS_ID])
    client.calls.clear()

    await verified.move_blocks(
        [b["uuid"] for b in blocks], destination["uuid"])

    assert len([m for m, _ in client.calls
                if m == "logseq.DB.moveBlock"]) == 10
    assert len(client.calls) < 30


async def test_the_run_appends_after_what_was_already_there(graph, content):
    existing = graph.add("Was here first", None, None, name="dest",
                         tags=[PAGE_CLASS_ID])
    resident = graph.add("Resident", existing["id"], existing["id"])
    arrivals = [graph.add(f"New {n}", graph.page["id"], graph.page["id"])
                for n in range(3)]

    await content.move_blocks(
        [a["uuid"] for a in arrivals], existing["uuid"])

    titles = [c["title"]
              for c in await content._children_of(existing["uuid"])]
    assert titles == ["Resident", "New 0", "New 1", "New 2"]
    assert resident["id"] is not None


async def test_placement_child_puts_the_run_at_the_top_still_in_order(
        graph, content):
    destination = graph.add("DEST", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])
    graph.add("Resident", destination["id"], destination["id"])
    arrivals = [graph.add(f"New {n}", graph.page["id"], graph.page["id"])
                for n in range(3)]

    await content.move_blocks(
        [a["uuid"] for a in arrivals], destination["uuid"],
        placement="child")

    titles = [c["title"]
              for c in await content._children_of(destination["uuid"])]
    assert titles == ["New 0", "New 1", "New 2", "Resident"]


async def test_a_mid_list_failure_reports_exactly_what_landed(graph):
    """The state flagged as worse than not starting. It must be legible: which
    UUIDs moved, where it stopped, and why it did not carry on."""
    class FailsOnThirdClient(FakeClient):
        def __init__(self, graph):
            super().__init__(graph)
            self.moves = 0

        def _move(self, block_uuid, target_uuid, options=None):
            self.moves += 1
            if self.moves == 3:
                return None          # reports success, does nothing
            return super()._move(block_uuid, target_uuid, options)

    client = FailsOnThirdClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    blocks = [graph.add(f"B{n}", graph.page["id"], graph.page["id"])
              for n in range(5)]
    destination = graph.add("DEST", None, None, name="dest",
                           tags=[PAGE_CLASS_ID])

    result = await verified.move_blocks(
        [b["uuid"] for b in blocks], destination["uuid"])

    assert result["verified"] is False
    assert result["summary"]["landed"] == 2
    assert [m["verified"] for m in result["moved"]] == [True, True, False]
    assert blocks[2]["uuid"] in result["diagnostic"]
    assert "position is defined by the one before it" in result["diagnostic"]
    # It stopped rather than placing the rest relative to a block that never
    # moved.
    assert len(result["moved"]) == 3


async def test_all_or_nothing_moves_the_landed_blocks_back(graph):
    class FailsOnThirdClient(FakeClient):
        def __init__(self, graph):
            super().__init__(graph)
            self.moves = 0

        def _move(self, block_uuid, target_uuid, options=None):
            self.moves += 1
            if self.moves == 3:
                return None
            return super()._move(block_uuid, target_uuid, options)

    client = FailsOnThirdClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    origin = graph.add("ORIGIN", None, None, name="origin",
                       tags=[PAGE_CLASS_ID])
    blocks = [graph.add(f"B{n}", origin["id"], origin["id"])
              for n in range(4)]
    destination = graph.add("DEST", None, None, name="dest",
                           tags=[PAGE_CLASS_ID])

    result = await verified.move_blocks(
        [b["uuid"] for b in blocks], destination["uuid"],
        all_or_nothing=True)

    assert result["verified"] is False
    assert [r["verified"] for r in result["rolled_back"]] == [True, True]
    # Back under the original parent...
    back = await verified._children_of(origin["uuid"])
    assert {b["title"] for b in back} >= {"B0", "B1"}
    # ...but the result must not claim the position was restored.
    assert "POSITION WAS NOT RESTORED" in result["diagnostic"]


async def test_beyond_the_cap_is_returned_untouched_and_in_order(graph):
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]
    blocks = [graph.add(f"B{n:03d}", graph.page["id"], graph.page["id"])
              for n in range(55)]
    destination = graph.add("DEST", None, None, name="dest",
                           tags=[PAGE_CLASS_ID])

    result = await verified.move_blocks(
        [b["uuid"] for b in blocks], destination["uuid"])

    assert result["summary"]["attempted"] == 50
    assert result["summary"]["not_attempted"] == 5
    assert result["not_attempted"] == [b["uuid"] for b in blocks[50:]]
    # A capped call is not a finished job.
    assert result["verified"] is False
    assert "capped" in result["diagnostic"]


async def test_a_nested_pair_is_refused(graph, content):
    """A move carries the subtree, so moving a parent and its child in the
    same list would carry the child along and then pull it back out."""
    parent = graph.add("Parent", graph.page["id"], graph.page["id"])
    child = graph.add("Child", parent["id"], graph.page["id"])
    destination = graph.add("DEST", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])

    with pytest.raises(ValueError, match="descendants of others"):
        await content.move_blocks(
            [parent["uuid"], child["uuid"]], destination["uuid"])


async def test_a_repeated_uuid_is_refused(graph, content):
    block = graph.add("Once", graph.page["id"], graph.page["id"])
    destination = graph.add("DEST", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])

    with pytest.raises(ValueError, match="same block twice"):
        await content.move_blocks(
            [block["uuid"], block["uuid"]], destination["uuid"])


async def test_moving_into_the_lists_own_subtree_is_refused(graph, content):
    parent = graph.add("Parent", graph.page["id"], graph.page["id"])
    child = graph.add("Child", parent["id"], graph.page["id"])
    other = graph.add("Other", graph.page["id"], graph.page["id"])

    with pytest.raises(ValueError, match="inside the subtree"):
        await content.move_blocks(
            [parent["uuid"], other["uuid"]], child["uuid"])


async def test_the_subtree_of_a_moved_block_follows_it(graph, content):
    parent = graph.add("Parent", graph.page["id"], graph.page["id"])
    graph.add("Descendant", parent["id"], graph.page["id"])
    destination = graph.add("DEST", None, None, name="dest",
                            tags=[PAGE_CLASS_ID])

    result = await content.move_blocks([parent["uuid"]], destination["uuid"])

    assert result["verified"] is True
    assert result["stranded_descendants"] == []


# ----------------------------------------------------------- searching
#
# There was no way to find a string in the graph, so a typo noticed while
# reading could not be located again once the page left context. The tool to
# fix it existed; the tool to find it did not.
#
# The predicate runs in Logseq's DB worker, which is the one query shape known
# to be able to wedge it -- hence the count-first design and the refusal to
# fetch an unbounded row set.

async def test_a_typo_is_found_with_its_page_in_one_call(graph, content):
    """The acceptance case."""
    block = graph.add("He weighed his Opions carefully",
                      graph.page["id"], graph.page["id"])

    found = await content.search_blocks("Opions")

    assert found["matches"] == 1
    assert found["returned"] == 1
    row = found["results"][0]
    assert row["uuid"] == block["uuid"]
    assert row["kind"] == "block"
    assert row["page"]["title"] == "TEST-PAGE"
    assert "Opions" in row["title"]


async def test_no_match_is_a_definite_zero(graph, content):
    """The distinction the count exists for: an empty row set could mean the
    result was too large, but a count of 0 cannot."""
    found = await content.search_blocks("Nothingcontainsthis")

    assert found["matches"] == 0
    assert found["results"] == []
    assert found["truncated"] is False
    assert "definite zero" in found["diagnostic"]


async def test_a_huge_match_set_is_counted_not_fetched(graph):
    """Above the ceiling it reports the count and reads nothing. A truncated
    row set would not say which matches were dropped, and a response big
    enough to hit the byte cap is indistinguishable from finding nothing."""
    class ManyMatchesClient(FakeClient):
        def _query(self, query, params):
            if "(count ?block)" in query:
                return 5000
            raise AssertionError("rows must not be fetched above the ceiling")

    verified = VerifiedContent(ManyMatchesClient(graph))  # type: ignore[arg-type]

    found = await verified.search_blocks("the")

    assert found["matches"] == 5000
    assert found["returned"] == 0
    assert found["truncated"] is True
    assert "Narrow the search" in found["diagnostic"]


async def test_search_is_scoped_to_a_page_when_asked(graph, content):
    elsewhere = graph.add("OTHER", None, None, name="other",
                          tags=[PAGE_CLASS_ID])
    graph.add("Agressive stance", graph.page["id"], graph.page["id"])
    graph.add("Agressive again", elsewhere["id"], elsewhere["id"])

    everywhere = await content.search_blocks("Agressive")
    scoped = await content.search_blocks(
        "Agressive", page_uuid=elsewhere["uuid"])

    assert everywhere["matches"] == 2
    assert scoped["matches"] == 1
    assert scoped["results"][0]["title"] == "Agressive again"


async def test_matching_is_case_sensitive(graph, content):
    """Pinned as a deliberate limitation: case folding would mean a second
    predicate over every title, and an exact misspelling is the target."""
    graph.add("Benifit of the doubt", graph.page["id"], graph.page["id"])

    assert (await content.search_blocks("Benifit"))["matches"] == 1
    assert (await content.search_blocks("benifit"))["matches"] == 0


async def test_non_latin_text_is_found(graph, content):
    """A stray 麻木 mid-sentence was one of the things that could not be found
    again. The needle goes through json.dumps, so it survives as an EDN string
    literal whatever it contains."""
    graph.add("the road was 麻木 and long",
              graph.page["id"], graph.page["id"])

    found = await content.search_blocks("麻木")

    assert found["matches"] == 1


async def test_a_quote_in_the_search_text_does_not_break_the_query(
        graph, content):
    graph.add('he said "Sesion Zero" twice', graph.page["id"],
              graph.page["id"])

    found = await content.search_blocks('"Sesion Zero"')

    assert found["matches"] == 1


async def test_a_page_title_typo_is_reported_as_a_page(graph, content):
    graph.add("Campaing Notes", None, None, name="campaing notes",
              tags=[PAGE_CLASS_ID])

    found = await content.search_blocks("Campaing")

    assert found["results"][0]["kind"] == "page"
    assert found["results"][0]["page"] is None


async def test_regex_refines_rather_than_widens(graph, content):
    graph.add("Initive order 3", graph.page["id"], graph.page["id"])
    graph.add("Initive order twelve", graph.page["id"], graph.page["id"])

    found = await content.search_blocks("Initive", regex=r"\d+$")

    assert found["matches"] == 2          # what the substring matched
    assert found["returned"] == 1         # what the regex kept
    assert found["results"][0]["title"] == "Initive order 3"
    assert "did not widen" in found["diagnostic"]


async def test_limit_truncates_and_says_so(graph, content):
    for n in range(6):
        graph.add(f"fufill {n}", graph.page["id"], graph.page["id"])

    found = await content.search_blocks("fufill", limit=2)

    assert found["matches"] == 6
    assert found["returned"] == 2
    assert found["truncated"] is True
    assert "raise limit" in found["diagnostic"]


async def test_a_too_short_search_is_refused(content):
    with pytest.raises(ValueError, match="at least 2 characters"):
        await content.search_blocks("a")


async def test_an_invalid_regex_is_refused_before_querying(graph):
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="regex is not valid"):
        await verified.search_blocks("Eqipment", regex="(unclosed")

    assert client.calls == []


async def test_an_absurd_limit_is_refused(content):
    with pytest.raises(ValueError, match="limit must be between"):
        await content.search_blocks("Advanatage", limit=10_000)


# --------------------------------------------- outline is the batching path

async def test_outline_allows_duplicate_titles_across_branches(graph, content):
    """createManyBlocks was removed because batching across arbitrary parents
    can commit partially. The outline builder keeps the batch benefit while
    knowing what it is building."""
    await content.create_page_of_blocks(
        graph.page["uuid"], "A\n    Notes\nB\n    Notes\n")

    sections = {e["title"]: e for e in graph.children(graph.page["id"])}
    for section in sections.values():
        assert [c["title"] for c in graph.children(section["id"])] == ["Notes"]


async def test_outline_resolves_every_parent_before_writing(graph, content):
    """A parent resolved mid-run meant an early level committed before a later
    failure -- reported as a validation error, which reads as 'nothing was
    written'."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]

    await verified.create_page_of_blocks(graph.page["uuid"], "A\n    A1\n")

    inserts = [m for m, _ in client.calls if m == "logseq.DB.insertBatchBlock"]
    assert len(inserts) == 2


# --------------------------------------------------------------- backlinks

async def test_find_backlinks_reports_nothing_for_an_unreferenced_block(
        graph, content):
    block = (await content.create_block(
        graph.page["uuid"], "Lonely")).verified_entities[0]

    result = await content.find_backlinks(block["uuid"])

    assert result["total"] == 0
    assert "Nothing refers" in result["diagnostic"]


async def test_find_backlinks_separates_the_three_mechanisms(graph, content):
    """A property value is a reference in the DB but does not appear in the
    UI's backlink panel, so the three cannot be merged into one count."""
    target = (await content.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]

    referrer = graph.add("Refers to it", graph.page["id"], graph.page["id"])
    referrer["refs"] = [{"id": target["id"]}]

    tagger = graph.add("Tagged with it", graph.page["id"], graph.page["id"])
    tagger["tags"] = [{"id": target["id"]}]

    prop = graph.add("Related", None, None,
                     ident=":plugin.property._test_plugin/Related",
                     tags=[PROPERTY_CLASS_ID])
    holder = graph.add("Points at it", graph.page["id"], graph.page["id"])
    holder[":plugin.property._test_plugin/Related"] = {"id": target["id"]}

    result = await content.find_backlinks(target["uuid"])

    assert result["total"] == 3
    assert [r["title"] for r in result["refs"]] == ["Refers to it"]
    assert [t["title"] for t in result["tagged"]] == ["Tagged with it"]
    assert result["property_values"][0]["holder"]["title"] == "Points at it"
    assert result["property_values"][0]["property"]["ident"] == prop["ident"]


async def test_find_backlinks_warns_that_deletes_do_not_rewrite(graph, content):
    target = (await content.create_block(
        graph.page["uuid"], "Target")).verified_entities[0]
    referrer = graph.add("Refers", graph.page["id"], graph.page["id"])
    referrer["refs"] = [{"id": target["id"]}]

    result = await content.find_backlinks(target["uuid"])

    assert "does not rewrite" in result["diagnostic"]


# ------------------------------------------- nested pages are not damage

async def test_nested_page_is_structure_not_damage(graph, content):
    """A page nested under another page is a legitimate page boundary. Blocks
    beneath it correctly belong to it, and reporting them as orphans invites
    repair of correct structure."""
    sub = graph.add("SUBPAGE", graph.page["id"], None,
                    name="subpage", tags=[PAGE_CLASS_ID])
    graph.add("child of subpage", sub["id"], sub["id"])

    report = await content.find_orphans(graph.page["uuid"])

    assert report["orphans"] == []
    assert len(report["nested_pages"]) == 1
    assert "No damage" in report["diagnostic"]
    assert "nested write" not in report["diagnostic"]


async def test_a_real_orphan_is_still_reported(graph, content):
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    graph.add("Orphaned", parent["id"], parent["id"])

    report = await content.find_orphans(graph.page["uuid"])

    assert len(report["orphans"]) == 1
    # The diagnostic must say what the condition IS without calling it damage:
    # a repair tool built on the opposite reading reordered ~1,500 blocks.
    assert "NOT damage" in report["diagnostic"]
    assert ":block/page misses them" in report["diagnostic"]


async def test_page_stats_returns_counts_not_payload(graph, content):
    """The point of the tool: integers regardless of page size, so a
    full-graph audit costs a fixed payload per page."""
    parent = (await content.create_block(
        graph.page["uuid"], "Parent")).verified_entities[0]
    await content.create_block(parent["uuid"], "Child")
    sub = graph.add("SUBPAGE", graph.page["id"], None,
                    name="subpage", tags=[PAGE_CLASS_ID])
    graph.add("under the subpage", sub["id"], sub["id"])

    stats = await content.page_stats(graph.page["uuid"])

    assert stats["own_blocks"] == 2
    assert stats["nested_pages"] == 1
    assert stats["true_orphans"] == 0
    # No block payload anywhere. `aliases` is a list of UUIDs, which is
    # bounded by the alias count rather than by page size, so the rule is
    # "no ENTITIES", not "no lists".
    assert not any(
        isinstance(value, list)
        and any(isinstance(item, dict) for item in value)
        for value in stats.values())
    assert stats["aliases"] == []
    assert stats["is_alias_of"] is None


# --------------------------------------------------------- alias visibility
#
# An alias relation appears in NO count: an empty page with one inbound
# reference that is a working alias is indistinguishable from a dead stub.
# It was found once only because findBacklinks happened to show a property
# holder. And it cannot be repaired after a delete -- `alias` is a built-in
# property, outside the namespace this server may write.

def make_alias_pair(graph, *, owner="Attribute", alias="Abilties",
                    attribute="alias"):
    """`owner` declares `alias` as one of its aliases.

    `attribute` switches between the two spellings Logseq has used, because
    the code queries both and a guard written against one silently never
    fires.
    """
    alias_page = graph.add(alias, None, None, name=alias.lower(),
                           tags=[PAGE_CLASS_ID])
    owner_page = graph.add(owner, None, None, name=owner.lower(),
                           tags=[PAGE_CLASS_ID])
    owner_page[attribute] = [{"id": alias_page["id"]}]
    return owner_page, alias_page


async def test_page_stats_shows_that_a_page_is_an_alias(graph, content):
    """The acceptance case: pageStats on Abilties reveals its relationship to
    Attribute, which every count above it hides."""
    owner, alias_page = make_alias_pair(graph)

    stats = await content.page_stats(alias_page["uuid"])

    assert stats["own_blocks"] == 0          # reads as a dead stub
    assert stats["is_alias_of"] == owner["uuid"]
    assert stats["aliases"] == []
    assert "ALIAS RELATION" in stats["diagnostic"]
    assert "NOT a dead stub" in stats["diagnostic"]


async def test_page_stats_shows_the_aliases_a_page_declares(graph, content):
    owner, alias_page = make_alias_pair(graph)

    stats = await content.page_stats(owner["uuid"])

    assert stats["aliases"] == [alias_page["uuid"]]
    assert stats["is_alias_of"] is None
    assert "ALIAS RELATION" in stats["diagnostic"]


async def test_the_newer_alias_attribute_is_seen_too(graph, content):
    """DB graphs carry the built-in as :logseq.property/alias. Querying only
    :block/alias would make every guard below silently pass."""
    owner, alias_page = make_alias_pair(
        graph, attribute=":logseq.property/alias")

    stats = await content.page_stats(alias_page["uuid"])

    assert stats["is_alias_of"] == owner["uuid"]


async def test_delete_refuses_an_alias_holder_without_acknowledgement(
        graph, content):
    """Stats only help if the caller looks, so the destructive path checks
    too. This is the shape that invites an unattended delete: no blocks, no
    refs, and a working alias."""
    _, alias_page = make_alias_pair(graph)

    result = await content.delete_page(alias_page["uuid"])

    assert result.verified is False
    assert "ALIAS relation" in (result.diagnostic or "")
    assert "cannot be repaired" in (result.diagnostic or "")
    assert graph.entities[alias_page["uuid"]].get(
        ":logseq.property/deleted-at") is None


async def test_delete_refuses_a_page_that_declares_aliases(graph, content):
    """Both directions: deleting the owner orphans the aliases pointing at
    it, which is the same loss seen from the other side."""
    owner, _ = make_alias_pair(graph)

    result = await content.delete_page(owner["uuid"])

    assert result.verified is False
    assert "ALIAS relation" in (result.diagnostic or "")


async def test_an_acknowledged_alias_delete_proceeds_and_says_what_broke(
        graph, content):
    _, alias_page = make_alias_pair(graph)

    result = await content.delete_page(
        alias_page["uuid"], acknowledge_alias_loss=True)

    assert result.verified is True
    assert "alias relation(s) were broken" in (result.diagnostic or "")


async def test_an_ordinary_page_still_deletes_without_the_alias_flag(
        graph, content):
    """The guard must not become a toll on every delete."""
    page = graph.add("Plain", None, None, name="plain", tags=[PAGE_CLASS_ID])

    result = await content.delete_page(page["uuid"])

    assert result.verified is True


# ------------------------------------------------------- outline formatting

def test_outline_strips_a_leading_bullet():
    """Keeping it produced blocks literally titled "- A" -- silent, and a
    structurally correct tree full of garbage."""
    assert _parse_outline("- A\n    - A1\n") == [((0,), "A"), ((0, 0), "A1")]


def test_outline_keeps_other_prefixes():
    """Only a markdown bullet is decoration; anything else is content."""
    assert _parse_outline("1. A\n> B\n") == [((0,), "1. A"), ((1,), "> B")]


def test_outline_mixes_bulleted_and_plain_lines():
    assert _parse_outline("A\n    - A1\n") == [((0,), "A"), ((0, 0), "A1")]

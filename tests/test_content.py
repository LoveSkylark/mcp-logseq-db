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
from mcp_logseq_db.content import VerifiedContent, _parse_outline

PAGE_CLASS_ID = 4
PROPERTY_CLASS_ID = 3


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
    # No block payload anywhere in the result.
    assert not any(isinstance(v, list) for v in stats.values())


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

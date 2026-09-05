"""
Page and block operations.

The fake graph below is deliberately more than a canned-response queue: the
behaviours worth testing here are relational -- which parent a block ends up
under, whether a subtree really went -- and a response queue cannot express
those. It also lets a test assert that a write did NOT happen, which is the
failure mode this API actually has.
"""

import itertools
from typing import Any

import pytest

from mcp_logseq_db.access import WriteAccessPolicy
from mcp_logseq_db.content import VerifiedContent, _parse_outline

PAGE_CLASS_ID = 4
PROPERTY_CLASS_ID = 3


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
            entity_id=None) -> dict[str, Any]:
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
        if page is not None:
            entity["page"] = {"id": page}
        self.entities[uuid] = entity
        return entity

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
        if method == "logseq.DB.upsertNodes":
            return self._upsert(*args)
        if method == "logseq.DB.removeBlock":
            return self._remove(args[0])
        if method == "logseq.DB.datascriptQuery":
            return self._query(args[0], args[1:])
        raise AssertionError(f"unexpected method {method}")

    def _upsert(self, operations, options):
        if options.get("dry-run"):
            return "Dry run: ok"
        if not self.write_effective:
            return None          # the silent no-op
        for op in operations:
            if op["operation"] == "edit":
                self.graph.entities[op["id"]]["title"] = op["data"]["title"]
                continue
            data = op["data"]
            if op["entityType"] == "page":
                self.graph.add(data["title"], None, None,
                               name=data["title"].lower(), tags=[PAGE_CLASS_ID])
                continue
            parent = self.graph.entities[data["page-id"]]
            page = (parent["id"] if parent.get("name")
                    else parent["page"]["id"])
            self.graph.add(data["title"], parent["id"], page)
        return {"block": len(operations)}

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
        if "#uuid" in query and "[?child :block/parent ?parent]" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            entity = self.graph.entities.get(uuid)
            return self.graph.children(entity["id"]) if entity else []
        if "[?child :block/parent ?parent]" in query:
            return self.graph.children(params[0])
        if "#uuid" in query and ":find (pull ?entity" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            return self.graph.entities.get(uuid)
        if ':block/title "' in query and ":find [(pull ?" in query:
            title = query.split(':block/title "')[1].split('"')[0]
            matches = [e for e in self.graph.entities.values()
                       if e.get("title") == title]
            if "[?page :block/name]" in query:
                matches = [e for e in matches if e.get("name")]
            return matches
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
    """`page-id` is a parent pointer, not a page pointer. This is the whole
    reason nested creation does not need a separate route."""
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

    with pytest.raises(RuntimeError, match="reports success for writes"):
        await VerifiedContent(client).create_block(  # type: ignore[arg-type]
            graph.page["uuid"], "Never created")


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

async def test_create_many_blocks_creates_all_of_them(graph, content):
    result = await content.create_many_blocks([
        {"parent_uuid": graph.page["uuid"], "title": f"Block {n}"}
        for n in range(3)
    ])

    assert len(result.verified_entities) == 3
    assert len(graph.children(graph.page["id"])) == 3


async def test_batch_rejects_duplicate_titles_under_one_parent(graph, content):
    """Two identical siblings cannot be told apart by read-back, so this is a
    real ambiguity rather than an arbitrary restriction."""
    with pytest.raises(ValueError, match="under the same parent"):
        await content.create_many_blocks([
            {"parent_uuid": graph.page["uuid"], "title": "Notes"},
            {"parent_uuid": graph.page["uuid"], "title": "Notes"},
        ])


async def test_batch_allows_the_same_title_under_different_parents(graph, content):
    """The common outline shape: two sections each with a child called Notes."""
    first = (await content.create_block(
        graph.page["uuid"], "Section 1")).verified_entities[0]
    second = (await content.create_block(
        graph.page["uuid"], "Section 2")).verified_entities[0]

    result = await content.create_many_blocks([
        {"parent_uuid": first["uuid"], "title": "Notes"},
        {"parent_uuid": second["uuid"], "title": "Notes"},
    ])

    assert len(result.verified_entities) == 2
    parents = {e["parent"]["id"] for e in result.verified_entities}
    assert parents == {first["id"], second["id"]}


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
    assert result["estimated_calls"] == 3
    assert graph.children(graph.page["id"]) == []


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
    """`:block/page` reaches any depth; `:block/parent` would stop at one."""
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
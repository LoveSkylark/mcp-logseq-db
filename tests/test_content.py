"""
Page and block operations.

The fake graph below is deliberately more than a canned-response queue: the
behaviours worth testing here are relational -- which parent a block ends up
under, whether a subtree really went -- and a response queue cannot express
those. It also lets a test assert that a write did NOT happen, which is the
failure mode this API actually has.
"""

import itertools
import re
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
            entity_id=None, extra=None) -> dict[str, Any]:
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
        if extra:
            entity.update(extra)
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
        if method == "logseq.DB.insertBlock":
            return self._insert_block(*args)
        if method == "logseq.DB.insertBatchBlock":
            return self._insert_batch(*args)
        if method == "logseq.DB.removeBlock":
            return self._remove(args[0])
        if method == "logseq.DB.datascriptQuery":
            return self._query(args[0], args[1:])
        raise AssertionError(f"unexpected method {method}")

    def _upsert(self, operations, options):
        # TWO arguments. The options map is required -- sending one argument
        # makes every write fail with "The Imported EDN has 4 validation
        # error(s)". The fake enforces the arity so dropping it again fails
        # here rather than on a live graph.
        if options.get("dry-run"):
            return "Dry run: ok"
        if not self.write_effective:
            return None          # the silent no-op
        for op in operations:
            if op["operation"] == "edit":
                # Replace rather than mutate: the real client returns a fresh
                # dict per call, so an in-place edit would let a caller's
                # earlier snapshot alias the updated entity.
                current = self.graph.entities[op["id"]]
                self.graph.entities[op["id"]] = {
                    **current, "title": op["data"]["title"]}
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
        if "[?block :block/page ?page]" in query:
            return [e for e in self.graph.entities.values()
                    if e.get("page", {}).get("id") == params[0]]
        return []


def test_upsert_nodes_sends_the_options_map() -> None:
    """Pinned because omitting it is not a harmless simplification: the API
    rejects the whole call and every write tool fails at once. This was
    removed once on the strength of a single call that appeared to work."""
    import inspect
    from mcp_logseq_db import content as content_module

    source = inspect.getsource(content_module.VerifiedContent.upsert_nodes)
    assert '[normalized, {"dry-run": True}]' in source
    assert '[normalized, {"dry-run": False}]' in source


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

async def test_create_many_blocks_creates_all_of_them(graph, content):
    result = await content.create_many_blocks([
        {"parent_uuid": graph.page["uuid"], "title": f"Block {n}"}
        for n in range(3)
    ])

    assert len(result.verified_entities) == 3
    assert len(graph.children(graph.page["id"])) == 3


async def test_batch_allows_duplicate_titles_under_one_parent(graph, content):
    """Previously rejected, because verification identified new blocks by
    title. insertBatchBlock returns the created entities, so identical
    siblings are no longer ambiguous."""
    result = await content.create_many_blocks([
        {"parent_uuid": graph.page["uuid"], "title": "Notes"},
        {"parent_uuid": graph.page["uuid"], "title": "Notes"},
    ])

    assert len(result.verified_entities) == 2
    assert len({e["uuid"] for e in result.verified_entities}) == 2


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


# --------------------------------------------------------- guard consistency

async def test_create_page_refuses_a_duplicate_before_writing(graph, content):
    """Pre-write, like renamePage. Relying on Logseq to no-op surfaced the
    failure as a readback mismatch, which reads like a transport problem."""
    client = FakeClient(graph)
    verified = VerifiedContent(client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="already exists"):
        await verified.create_page("TEST-PAGE")

    assert not any(m == "logseq.DB.upsertNodes" for m, _ in client.calls)


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
    assert report["reachable_by_parent"] > report["reachable_by_page"]


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

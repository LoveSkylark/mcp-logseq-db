"""
Tag and property mutations.

Two properties of this API shape most of these tests:

  - a write with the wrong identifier TYPE returns success and does nothing,
    so several tests assert that a call was refused BEFORE reaching the API
  - the page/block distinction does not exist for tags or property values,
    so every such operation is tested against both kinds of target
"""

import itertools
from typing import Any

import pytest

from mcp_logseq_db.access import WriteAccessPolicy
from mcp_logseq_db.identifiers import IdentifierError
from mcp_logseq_db.mutations import MutationVerificationError, VerifiedMutations

TAG_CLASS_ID = 2
PROPERTY_CLASS_ID = 3
PAGE_CLASS_ID = 4
WRITABLE = ":plugin.property._test_plugin/Effort"


class FakeGraph:
    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self.entities: dict[str, dict[str, Any]] = {}
        self.add("Tag", ident=":logseq.class/Tag", entity_id=TAG_CLASS_ID)
        self.add("Property", ident=":logseq.class/Property",
                 entity_id=PROPERTY_CLASS_ID)
        self.add("Page", ident=":logseq.class/Page", entity_id=PAGE_CLASS_ID)
        self.page = self.add("TEST-PAGE", name="test-page", tags=[PAGE_CLASS_ID])
        self.block = self.add("A block", parent=self.page["id"],
                              page=self.page["id"])
        self.tag = self.add("xzy", ident=":user.class/xzy-bc0auNqC",
                            tags=[TAG_CLASS_ID])
        self.prop = self.add("Effort", ident=WRITABLE, tags=[PROPERTY_CLASS_ID])
        self.user_prop = self.add("fun", ident=":user.property/fun-W8dp1CaI",
                                  tags=[PROPERTY_CLASS_ID])

    def add(self, title, *, name=None, ident=None, tags=None, parent=None,
            page=None, entity_id=None) -> dict[str, Any]:
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


class FakeClient:
    def __init__(self, graph: FakeGraph, *, policy=None,
                 write_effective: bool = True,
                 writable_property_prefix: str = "plugin.property._test_plugin/"
                 ) -> None:
        self.graph = graph
        self.write_policy = policy
        self.write_effective = write_effective
        self.writable_property_prefix = writable_property_prefix
        self.calls: list[tuple[str, list[Any]]] = []

    async def call(self, method: str, args: list[Any]) -> Any:
        self.calls.append((method, args))
        handler = getattr(self, "_" + method.split(".")[-1], None)
        if handler is None:
            raise AssertionError(f"unexpected method {method}")
        return handler(*args)

    # -- writes ------------------------------------------------------------
    def _addBlockTag(self, target, tag):
        if not self.write_effective:
            return None
        entity = self.graph.entities[target]
        tag_id = self.graph.entities[tag]["id"]
        entity.setdefault("tags", []).append({"id": tag_id})
        return None

    def _removeBlockTag(self, target, tag):
        if not self.write_effective:
            return None
        entity = self.graph.entities[target]
        tag_id = self.graph.entities[tag]["id"]
        entity["tags"] = [t for t in entity.get("tags", [])
                          if t["id"] != tag_id]
        return None

    def _createTag(self, title, options=None):
        return self.graph.add(title, ident=f":user.class/{title}-abc123",
                              tags=[TAG_CLASS_ID])

    def _deletePage(self, identifier):
        if not self.write_effective:
            return None
        self.graph.entities.pop(identifier, None)
        return None

    def _upsertProperty(self, title, schema=None, options=None):
        ident = f":plugin.property._test_plugin/{title}"
        return self.graph.add(title, ident=ident, tags=[PROPERTY_CLASS_ID])

    def _removeProperty(self, ident):
        if not self.write_effective:
            return None
        target = next((u for u, e in self.graph.entities.items()
                       if e.get("ident") == ident), None)
        self.graph.entities.pop(target, None)
        return None

    def _upsertBlockProperty(self, target, ident, value, options=None):
        if not self.write_effective:
            return None
        self.graph.entities[target][ident] = value
        return None

    def _removeBlockProperty(self, target, ident):
        if not self.write_effective:
            return None
        self.graph.entities[target].pop(ident, None)
        return None

    # -- reads -------------------------------------------------------------
    def _getTagsByName(self, title):
        return [e for e in self.graph.entities.values()
                if e.get("title") == title
                and any(t["id"] == TAG_CLASS_ID for t in e.get("tags", []))]

    def _datascriptQuery(self, query, *params):
        if ":find ?class" in query:
            ident = query.split(":db/ident ")[1].split("]")[0]
            return next((e["id"] for e in self.graph.entities.values()
                         if e.get("ident") == ident), None)
        if "#uuid" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            return self.graph.entities.get(uuid)
        if ":find (pull ?prop [*])" in query or ":db/ident " in query:
            ident = query.split(":db/ident ")[1].split("]")[0]
            return next((e for e in self.graph.entities.values()
                         if e.get("ident") == ident), None)
        return []


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def mutations(graph) -> VerifiedMutations:
    return VerifiedMutations(FakeClient(graph))  # type: ignore[arg-type]


# ----------------------------------------------------------------- tags

@pytest.mark.parametrize("target", ["page", "block"])
async def test_add_tag_works_on_pages_and_blocks(graph, mutations, target):
    """A page IS a block in the DB, which is why there is one tool and not
    two. Both routes go through the same API method."""
    entity = graph.page if target == "page" else graph.block

    result = await mutations.add_tag(entity["uuid"], graph.tag["uuid"])

    assert result.verified is True
    assert any(t["id"] == graph.tag["id"]
               for t in result.verified_state.get("tags", []))


@pytest.mark.parametrize("target", ["page", "block"])
async def test_remove_tag_leaves_other_tags_alone(graph, mutations, target):
    entity = graph.page if target == "page" else graph.block
    other = graph.add("keepme", ident=":user.class/keepme-x",
                      tags=[TAG_CLASS_ID])
    await mutations.add_tag(entity["uuid"], graph.tag["uuid"])
    await mutations.add_tag(entity["uuid"], other["uuid"])

    result = await mutations.remove_tag(entity["uuid"], graph.tag["uuid"])

    remaining = {t["id"] for t in result.verified_state.get("tags", [])}
    assert graph.tag["id"] not in remaining
    assert other["id"] in remaining


async def test_removing_a_tag_from_a_page_keeps_it_a_page(graph, mutations):
    """Stripping :logseq.class/Page would stop the entity being a page. The
    dedicated route cannot do that -- an upsert overwrite could."""
    await mutations.add_tag(graph.page["uuid"], graph.tag["uuid"])

    result = await mutations.remove_tag(graph.page["uuid"], graph.tag["uuid"])

    assert result.verified_state.get("name") == "test-page"


async def test_tag_change_that_does_nothing_is_reported(graph):
    client = FakeClient(graph, write_effective=False)

    with pytest.raises(MutationVerificationError, match="was not observed"):
        await VerifiedMutations(client).add_tag(  # type: ignore[arg-type]
            graph.page["uuid"], graph.tag["uuid"])


async def test_get_tag_uuid_refuses_an_ambiguous_title(graph, mutations):
    graph.add("xzy", ident=":user.class/xzy-second", tags=[TAG_CLASS_ID])

    result = await mutations.get_tag_uuid("xzy")

    assert result["found"] is False
    assert len(result["candidates"]) == 2


async def test_get_tag_rejects_an_entity_that_is_not_a_tag(graph, mutations):
    with pytest.raises(ValueError, match="not a tag"):
        await mutations.get_tag(graph.page["uuid"])


async def test_create_tag_reads_back_its_generated_identity(graph, mutations):
    """Tag idents carry a random suffix, so the identity cannot be predicted
    from the title and must come from the graph."""
    result = await mutations.create_tag("brand-new")

    assert result.verified is True
    assert result.verified_state["ident"].startswith(":user.class/brand-new")


# ------------------------------------------------------------ properties

@pytest.mark.parametrize("target", ["page", "block"])
async def test_set_and_clear_property_on_either_target(graph, mutations, target):
    entity = graph.page if target == "page" else graph.block

    await mutations.set_property(entity["uuid"], WRITABLE, 5)
    assert graph.entities[entity["uuid"]][WRITABLE] == 5

    await mutations.clear_property(entity["uuid"], WRITABLE)
    assert WRITABLE not in graph.entities[entity["uuid"]]


async def test_user_namespace_properties_are_refused_before_the_call(graph):
    """Logseq sandboxes property writes to the caller's own namespace. The
    guard is local so the failure is an error, not a silent no-op."""
    client = FakeClient(graph)

    with pytest.raises(ValueError, match="outside this caller's namespace"):
        await VerifiedMutations(client).set_property(  # type: ignore[arg-type]
            graph.page["uuid"], ":user.property/fun-W8dp1CaI", 1)

    assert client.calls == []          # never reached the API


async def test_builtin_properties_are_also_outside_the_sandbox(graph):
    client = FakeClient(graph)

    with pytest.raises(ValueError, match="outside this caller's namespace"):
        await VerifiedMutations(client).set_property(  # type: ignore[arg-type]
            graph.page["uuid"], ":logseq.property/status", 77)


@pytest.mark.parametrize(
    "ident", ["Effort", "plugin.property._test_plugin", TAG_CLASS_ID,
              "6a9a1a1c-cede-430f-8768-7a3609d4039b"])
async def test_property_ident_must_be_namespaced(graph, mutations, ident):
    """A UUID passed where an ident belongs is the exact mistake that returns
    success and does nothing."""
    with pytest.raises(ValueError, match="namespaced property ident"):
        await mutations.set_property(graph.page["uuid"], ident, 1)


async def test_create_property_rejects_a_namespaced_title(graph, mutations):
    """Logseq treats the first argument as a page name and rejects the '/'."""
    with pytest.raises(ValueError, match="plain title"):
        await mutations.create_property(WRITABLE, {"type": "number"})


async def test_create_property_returns_the_assigned_ident(graph, mutations):
    result = await mutations.create_property("Budget", {"type": "number"})

    assert result.verified_state["ident"] == (
        ":plugin.property._test_plugin/Budget")


async def test_property_write_that_does_nothing_is_reported(graph):
    client = FakeClient(graph, write_effective=False)

    with pytest.raises(MutationVerificationError, match="was not set"):
        await VerifiedMutations(client).set_property(  # type: ignore[arg-type]
            graph.page["uuid"], WRITABLE, 5)


async def test_delete_property_that_does_nothing_is_reported(graph):
    """A UUID given to removeProperty returns success and leaves it in place;
    only the read-back catches that."""
    client = FakeClient(graph, write_effective=False)

    with pytest.raises(MutationVerificationError, match="still present"):
        await VerifiedMutations(client).delete_property(  # type: ignore[arg-type]
            WRITABLE)


# ------------------------------------------------------------ identifiers

@pytest.mark.parametrize(
    "value", ["$TARGET-UUID", "TEST-PAGE", ":user.class/xzy-bc0auNqC", "859"])
async def test_target_must_be_a_uuid(graph, mutations, value):
    with pytest.raises(IdentifierError):
        await mutations.add_tag(value, graph.tag["uuid"])


async def test_entity_scope_applies_to_the_target_being_changed(graph):
    client = FakeClient(graph, policy=WriteAccessPolicy(
        entity_uuids=frozenset({graph.block["uuid"]})))
    mutations = VerifiedMutations(client)  # type: ignore[arg-type]

    await mutations.add_tag(graph.block["uuid"], graph.tag["uuid"])

    with pytest.raises(PermissionError):
        await mutations.add_tag(graph.page["uuid"], graph.tag["uuid"])
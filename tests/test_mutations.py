"""
Tag and property mutations.

Two properties of this API shape most of these tests:

  - a write with the wrong identifier TYPE returns success and does nothing,
    so several tests assert that a call was refused BEFORE reaching the API
  - the page/block distinction does not exist for tags or property values,
    so every such operation is tested against both kinds of target
"""

import itertools
import re
from typing import Any

import pytest

from mcp_logseq_db.access import WriteAccessPolicy
from mcp_logseq_db.identifiers import IdentifierError
from mcp_logseq_db.mutations import MutationVerificationError, VerifiedMutations

TAG_CLASS_ID = 2
PROPERTY_CLASS_ID = 3
PAGE_CLASS_ID = 4
WRITABLE = ":plugin.property._test_plugin/Effort"
NODE_PROP = ":plugin.property._test_plugin/Related"
MANY_PROP = ":plugin.property._test_plugin/Labels"


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
        self.prop = self.add("Effort", ident=WRITABLE, tags=[PROPERTY_CLASS_ID],
                             extra={":logseq.property/type": "number",
                                    "cardinality": ":db.cardinality/one"})
        self.node_prop = self.add(
            "Related", ident=NODE_PROP, tags=[PROPERTY_CLASS_ID],
            extra={":logseq.property/type": "node",
                   "cardinality": ":db.cardinality/one"})
        self.many_prop = self.add(
            "Labels", ident=MANY_PROP, tags=[PROPERTY_CLASS_ID],
            extra={":logseq.property/type": "default",
                   "cardinality": ":db.cardinality/many"})
        self.user_prop = self.add("fun", ident=":user.property/fun-W8dp1CaI",
                                  tags=[PROPERTY_CLASS_ID])

    def add(self, title, *, name=None, ident=None, tags=None, parent=None,
            page=None, entity_id=None, extra=None) -> dict[str, Any]:
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
        return self.graph.add(
            title, ident=ident, tags=[PROPERTY_CLASS_ID],
            extra={":logseq.property/type": (schema or {}).get("type",
                                                               "default"),
                   "cardinality": ":db.cardinality/one"})

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

    def _removeBlock(self, uuid):
        if not self.write_effective:
            return None
        self.graph.entities.pop(uuid, None)
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
        # Value blocks belonging to a property definition.
        if ":logseq.property/created-from-property ?property" in query:
            return [e["uuid"] for e in self.graph.entities.values()
                    if (e.get(":logseq.property/created-from-property") or {})
                    .get("id") == params[0]]
        # Tag usage: [?holder :block/tags ?tag]
        if "[?holder :block/tags ?tag]" in query:
            return [e for e in self.graph.entities.values()
                    if any(t.get("id") == params[0]
                           for t in e.get("tags", []))]
        # Property usage: [?holder <ident> ?value]
        match = re.search(r"\[\?holder (:[\w.]+/[\w.-]+) \?value\]", query)
        if match:
            ident = match.group(1)
            return [[e, e[ident]] for e in self.graph.entities.values()
                    if ident in e]
        # Batch entity resolution: [?e ?a _] with an [?e ...] binding
        if ":in $ [?e ...]" in query:
            wanted = set(params[0])
            return [e for e in self.graph.entities.values()
                    if e["id"] in wanted]
        if ':block/title "' in query and ":find [(pull ?e" in query:
            title = query.split(':block/title "')[1].split('"')[0]
            return [e for e in self.graph.entities.values()
                    if e.get("title") == title and e.get("name")]
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


# ------------------------------------------------- value type and cardinality

async def test_reference_property_rejects_a_literal(graph):
    """The silent-miswrite case: Logseq accepts a string for a node property,
    mints a value entity named after it, and the read-back sees a value and
    passes. Rejecting before the call is the only way to catch it."""
    client = FakeClient(graph)

    with pytest.raises(ValueError, match="must be an entity id"):
        await VerifiedMutations(client).set_property(  # type: ignore[arg-type]
            graph.page["uuid"], NODE_PROP, "just a string")

    # Reads to resolve the property and target are expected; the point is that
    # no write was sent.
    assert not any(m == "logseq.DB.upsertBlockProperty" for m, _ in client.calls)


@pytest.mark.parametrize("value", [859, {"db/id": 859}])
async def test_reference_property_accepts_an_entity(graph, mutations, value):
    result = await mutations.set_property(graph.page["uuid"], NODE_PROP, value)
    assert result.verified is True


async def test_cardinality_many_does_not_duplicate(graph):
    """Writing the same value twice to a many property creates a third value
    entity rather than replacing. An import run twice would silently double.

    The held value is an entity id and the incoming one a literal, so the
    comparison has to resolve before comparing -- a direct comparison never
    matches and the dedupe silently does nothing."""
    client = FakeClient(graph)
    mutations = VerifiedMutations(client)  # type: ignore[arg-type]

    await mutations.set_property(graph.page["uuid"], MANY_PROP, "alpha")
    sent_after_first = len(client.calls)

    result = await mutations.set_property(graph.page["uuid"], MANY_PROP, "alpha")

    assert "duplicate" in (result.diagnostic or "")
    # No write was sent the second time.
    assert not any(m == "logseq.DB.upsertBlockProperty"
                   for m, _ in client.calls[sent_after_first:])


async def test_cardinality_one_still_overwrites(graph, mutations):
    """The dedupe applies only to many; a one property must stay replaceable."""
    await mutations.set_property(graph.page["uuid"], WRITABLE, 5)
    result = await mutations.set_property(graph.page["uuid"], WRITABLE, 5)
    assert result.verified is True


async def test_create_tag_refuses_an_existing_page_title(graph, mutations):
    """createPage already refuses a tag's title. Without the mirror check the
    guard is asymmetric and both entities become unresolvable by title."""
    with pytest.raises(ValueError, match="already exists"):
        await mutations.create_tag("TEST-PAGE")


# ------------------------------------------------- destructive-operation gates

async def test_delete_property_refuses_while_values_exist(graph, mutations):
    """deletePage gates on orphaning references; this destroys every value of
    the property and had no gate at all."""
    await mutations.set_property(graph.page["uuid"], WRITABLE, 5)

    result = await mutations.delete_property(WRITABLE)

    assert result.verified is False
    assert "acknowledge_value_loss" in (result.diagnostic or "")
    assert graph.entities[graph.prop["uuid"]] is not None


async def test_delete_tag_refuses_while_holders_exist(graph, mutations):
    await mutations.add_tag(graph.page["uuid"], graph.tag["uuid"])

    result = await mutations.delete_tag(graph.tag["uuid"])

    assert result.verified is False
    assert "acknowledge_detach" in (result.diagnostic or "")
    assert graph.tag["uuid"] in graph.entities


async def test_property_usage_survives_a_literal_value(graph, mutations):
    """checkbox and datetime store literals inline. Pulling the value made the
    usage query 500 and left those properties undeletable."""
    checkbox = graph.add("Flag", ident=":plugin.property._test_plugin/Flag",
                         tags=[PROPERTY_CLASS_ID],
                         extra={":logseq.property/type": "checkbox",
                                "cardinality": ":db.cardinality/one"})
    graph.entities[graph.page["uuid"]][checkbox["ident"]] = True

    users = await mutations.get_property_users(checkbox["ident"])

    assert users
    assert users[0]["value"] is True
    assert users[0]["value_entity"] is None


async def test_cardinality_many_dedupes_against_a_materialized_value(graph):
    """The realistic shape: the property holds a pointer to a value entity,
    not the literal that was written."""
    client = FakeClient(graph)
    mutations = VerifiedMutations(client)  # type: ignore[arg-type]

    value_entity = graph.add("alpha", extra={":logseq.property/value": "alpha"})
    graph.entities[graph.page["uuid"]][MANY_PROP] = [{"id": value_entity["id"]}]

    result = await mutations.set_property(graph.page["uuid"], MANY_PROP, "alpha")

    assert "duplicate" in (result.diagnostic or "")
    assert not any(m == "logseq.DB.upsertBlockProperty" for m, _ in client.calls)


async def test_delete_property_sweeps_its_value_blocks(graph):
    """Removing the definition clears the attribute but leaves the
    materialized value blocks behind as orphans on their pages."""
    client = FakeClient(graph)
    mutations = VerifiedMutations(client)  # type: ignore[arg-type]

    value_block = graph.add(
        "42", parent=graph.page["id"], page=graph.page["id"],
        extra={":logseq.property/created-from-property": {"id": graph.prop["id"]}})

    result = await mutations.delete_property(
        WRITABLE, acknowledge_value_loss=True)

    assert result.verified is True
    assert value_block["uuid"] not in graph.entities
    assert "swept 1" in (result.diagnostic or "")


# ------------------------------------------------- verification is substantive

async def test_set_property_rejects_a_value_that_did_not_land(graph):
    """Presence is not correctness. Checking only that the ident appeared
    would report success for a write that stored something else."""
    class WrongValueClient(FakeClient):
        def _upsertBlockProperty(self, target, ident, value, options=None):
            self.graph.entities[target][ident] = "something else"
            return None

    client = WrongValueClient(graph)

    with pytest.raises(MutationVerificationError, match="not 5"):
        await VerifiedMutations(client).set_property(  # type: ignore[arg-type]
            graph.page["uuid"], WRITABLE, 5)


async def test_set_property_accepts_a_materialized_value(graph):
    """The realistic shape: the write stores a pointer to a minted value
    entity, so the check has to resolve before comparing."""
    class MaterializingClient(FakeClient):
        def _upsertBlockProperty(self, target, ident, value, options=None):
            entity = self.graph.add(
                str(value), extra={":logseq.property/value": value})
            self.graph.entities[target][ident] = {"id": entity["id"]}
            return None

    client = MaterializingClient(graph)

    result = await VerifiedMutations(client).set_property(  # type: ignore[arg-type]
        graph.page["uuid"], WRITABLE, 5)

    assert result.verified is True


async def test_create_property_rejects_a_type_that_does_not_match(graph):
    """A `number` that is really `default` will not reject a string later,
    and every subsequent write is validated against the wrong type."""
    class WrongTypeClient(FakeClient):
        def _upsertProperty(self, title, schema=None, options=None):
            return self.graph.add(
                title, ident=f":plugin.property._test_plugin/{title}",
                tags=[PROPERTY_CLASS_ID],
                extra={":logseq.property/type": "default"})

    client = WrongTypeClient(graph)

    with pytest.raises(MutationVerificationError, match="not the requested"):
        await VerifiedMutations(client).create_property(  # type: ignore[arg-type]
            "Budget", {"type": "number"})


async def test_create_property_reports_storage_shape(graph, mutations):
    """Cardinality and valueType decide how later writes behave, so they are
    surfaced rather than left to be discovered."""
    result = await mutations.create_property("Budget", {"type": "number"})

    assert result.verified is True
    assert "cardinality" in (result.diagnostic or "")

"""
Page import and link repair.

The escaping is the load-bearing part. Logseq parses block content on write --
confirmed live: `[[X]]` mints a page and rewrites the text to `[[uuid]]`, `#X`
mints a tag AND tags the block. So an unescaped import of a page full of links
creates a stub for every target that does not exist, which is how a graph
fills with duplicate stubs.
"""

import itertools
from typing import Any

import pytest

from mcp_logseq_db.importer import VerifiedImport
from mcp_logseq_db.markdown import (
    escape_references,
    find_placeholders,
    parse_markdown,
    restore_reference,
)

PAGE_CLASS_ID = 4


# ------------------------------------------------------------------ parsing

def test_indentation_is_measured_before_the_bullet():
    """"\\t- x" is depth 1 holding "x". Measuring after the bullet would make
    every line depth 0."""
    parsed = parse_markdown("- A\n\t- A1\n\t\t- A1a\n")
    assert parsed.blocks[0].content == "A"
    assert parsed.blocks[0].children[0].content == "A1"
    assert parsed.blocks[0].children[0].children[0].content == "A1a"


def test_a_line_without_a_bullet_continues_the_previous_block():
    """A wrapped paragraph is more of the same block. Without this it becomes
    a child, silently changing the structure."""
    parsed = parse_markdown("- First line\n  continued here\n- Second\n")
    assert parsed.blocks[0].content == "First line\ncontinued here"
    assert len(parsed.blocks) == 2


def test_page_properties_stop_at_the_first_bullet():
    parsed = parse_markdown("alias:: City/Dawnspire\n\n- A block\n")
    assert parsed.page_properties == {"alias": "City/Dawnspire"}
    assert len(parsed.blocks) == 1


def test_spaces_and_tabs_both_work():
    tabs = parse_markdown("- A\n\t- A1\n")
    spaces = parse_markdown("- A\n    - A1\n")
    assert tabs.blocks[0].children[0].content == "A1"
    assert spaces.blocks[0].children[0].content == "A1"


def test_markdown_with_no_blocks_is_rejected():
    with pytest.raises(ValueError, match="No blocks found"):
        parse_markdown("Just a paragraph with no bullet.")


def test_call_count_is_one_per_parent_with_children():
    parsed = parse_markdown("- A\n\t- A1\n\t- A2\n- B\n\t- B1\n")
    assert parsed.block_count() == 5
    # The page, plus A, plus B.
    assert parsed.call_count() == 3


# ----------------------------------------------------------------- escaping

@pytest.mark.parametrize(
    ("source", "escaped"),
    [
        ("[[Dawnspire]] and [[City]]",
         "{{link:Dawnspire}} and {{link:City}}"),
        ("### Historical Weight #fix",
         "### Historical Weight {{tag:fix}}"),
        ("#Industrial", "{{tag:Industrial}}"),
        ("A #[[Multi Word Tag]] inline.", "A {{tag:Multi Word Tag}} inline."),
    ],
)
def test_references_are_escaped(source: str, escaped: str):
    assert escape_references(source)[0] == escaped


@pytest.mark.parametrize(
    "source",
    [
        "Bold **text** and an em dash —",
        "A {{query (todo)}} macro",          # a real Logseq macro
        "Edge cases: C# and issue#4",        # not tags
        "## Heading stays a heading",        # Logseq converts this itself
    ],
)
def test_content_without_references_is_untouched(source: str):
    assert escape_references(source)[0] == source


def test_bracketed_tags_are_consumed_before_links():
    """`#[[X]]` must not be read as a `#` beside a link."""
    escaped, links, tags = escape_references("#[[Some Page]]")
    assert escaped == "{{tag:Some Page}}"
    assert links == []
    assert tags == ["Some Page"]


def test_escaping_reports_every_reference():
    _, links, tags = escape_references(
        "[[A]] and [[B]] and [[A]] with #t")
    assert links == ["A", "B", "A"]
    assert tags == ["t"]


def test_one_reference_can_be_restored_at_a_time():
    """A block with several links is repaired partially: the resolvable ones
    are restored and the rest stay escaped, rather than skipping the block."""
    content = "{{link:Known}} and {{link:Unknown}}"
    content = restore_reference(content, "Known", is_tag=False)

    assert content == "[[Known]] and {{link:Unknown}}"
    assert find_placeholders(content) == (["Unknown"], [])


def test_restored_tags_use_the_bracketed_form():
    """`#[[x]]` survives a name containing spaces; Logseq treats the two the
    same."""
    assert restore_reference("{{tag:fix}}", "fix", is_tag=True) == "#[[fix]]"


# --------------------------------------------------------------- the fixture

SAMPLE = """alias:: City/Dawnspire

- Perched in a sunlit range, **Dawnspire** is a marvel.
- #Industrial
- ## Core Elements
\t- ### Urban Layout
\t\t- [[Dawnspire]] is centered on the [[City]] and its [[Plants]].
\t- ### Historical Weight #fix
\t\t- Founded by pioneers.
- ---
"""


class FakeGraph:
    def __init__(self) -> None:
        self._ids = itertools.count(1000)
        self.entities: dict[str, dict[str, Any]] = {}
        self.add("Page", name="page", ident=":logseq.class/Page",
                 entity_id=PAGE_CLASS_ID)
        self.page = self.add("TEST-PAGE", name="test-page",
                             tags=[PAGE_CLASS_ID])

    def add(self, title, parent=None, page=None, *, name=None, tags=None,
            ident=None, entity_id=None) -> dict[str, Any]:
        entity_id = entity_id if entity_id is not None else next(self._ids)
        uuid = "%08x-0000-4000-8000-000000000000" % entity_id
        entity: dict[str, Any] = {"id": entity_id, "uuid": uuid,
                                  "title": title}
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

    def children(self, parent_id: int) -> list[dict[str, Any]]:
        return [e for e in self.entities.values()
                if e.get("parent", {}).get("id") == parent_id]


class FakeClient:
    """Models the behaviour that matters: content is PARSED on write."""

    write_policy = None
    writable_property_prefix = "plugin.property._test/"

    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.calls: list[tuple[str, list[Any]]] = []

    async def call(self, method: str, args: list[Any]) -> Any:
        self.calls.append((method, args))
        if method == "logseq.DB.insertBatchBlock":
            return self._insert_batch(*args)
        if method == "logseq.DB.insertBlock":
            return self._insert(*args)
        if method == "logseq.DB.createPage":
            return self._create_page(*args)
        if method == "logseq.DB.getPage":
            return self._get_page(*args)
        if method == "logseq.DB.getTagsByName":
            return self._get_tags_by_name(*args)
        if method == "logseq.DB.updateBlock":
            return self._update(*args)
        if method == "logseq.DB.datascriptQuery":
            return self._query(args[0], args[1:])
        raise AssertionError(f"unexpected method {method}")

    def _parse(self, entity: dict[str, Any], content: str) -> None:
        """Logseq mints a page for any [[X]] it sees and rewrites the text."""
        import re
        refs = []
        for name in re.findall(r"\[\[([^\]]+)\]\]", content):
            target = next(
                (e for e in self.graph.entities.values()
                 if e.get("title") == name and e.get("name")), None)
            if target is None:
                target = self.graph.add(name, name=name.lower(),
                                        tags=[PAGE_CLASS_ID])
            refs.append({"id": target["id"]})
            content = content.replace(f"[[{name}]]", f"[[{target['uuid']}]]")
        entity["title"] = content
        if refs:
            entity["refs"] = (entity.get("refs") or []) + refs

    def _insert_batch(self, target_uuid, blocks, options=None):
        target = self.graph.entities[target_uuid]
        page = target["id"] if target.get("name") else target["page"]["id"]
        out = []
        for block in blocks:
            entity = self.graph.add("", target["id"], page)
            self._parse(entity, block["content"])
            out.append(dict(entity))
        return out

    def _insert(self, target_uuid, title, options=None):
        return self._insert_batch(target_uuid, [{"content": title}])[0]

    def _create_page(self, title, properties=None):
        """Idempotent on title, and seeds one empty first block -- both real
        behaviours of the route page creation now takes. upsertNodes is
        deliberately unhandled: it fails on synced graphs and nothing routes
        through it, so a call here means something regressed."""
        existing = next((e for e in self.graph.entities.values()
                         if e.get("name") == str(title).lower()), None)
        if existing is not None:
            return dict(existing)
        page = self.graph.add(title, name=str(title).lower(),
                              tags=[PAGE_CLASS_ID])
        self.graph.add("", page["id"], page["id"])
        return dict(page)

    def _get_page(self, identifier):
        """Accepts a name OR a uuid, as the real method does."""
        found = self.graph.entities.get(identifier)
        if found is None:
            found = next((e for e in self.graph.entities.values()
                          if e.get("name") == str(identifier).lower()), None)
        return dict(found) if found else None

    def _get_tags_by_name(self, title):
        """Tags, modelled here as ident-carrying entities with no :block/name.
        No fixture creates one, so a tag reference resolves to nothing -- which
        is the case repair has to handle, since Logseq would mint the tag on
        write."""
        return [dict(e) for e in self.graph.entities.values()
                if e.get("title") == title and e.get("ident")
                and not e.get("name")]

    def _update(self, block_uuid, title):
        self._parse(self.graph.entities[block_uuid], title)
        return None

    def _query(self, query: str, params):
        if ":find ?class" in query:
            ident = query.split(":db/ident ")[1].split("]")[0]
            return next((e["id"] for e in self.graph.entities.values()
                         if e.get("ident") == ident), None)
        if ":block/_parent" in query and "#uuid" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            root = self.graph.entities.get(uuid)
            if root is None:
                return None

            def build(entity):
                node = {k: v for k, v in entity.items() if k != "_parent"}
                kids = self.graph.children(entity["id"])
                if kids:
                    node["_parent"] = [build(k) for k in kids]
                return node
            return build(root)
        if "{{link:" in query or "{{tag:" in query:
            prefix = "{{link:" if "{{link:" in query else "{{tag:"
            pages = set()
            for e in self.graph.entities.values():
                if prefix in (e.get("title") or "") and e.get("page"):
                    pages.add(next(u for u, x in self.graph.entities.items()
                                   if x["id"] == e["page"]["id"]))
            return sorted(pages)
        if ":find [?title ...]" in query:
            return [e["title"] for e in self.graph.entities.values()
                    if e.get("name")]
        if "#uuid" in query and ":find (pull ?entity" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            found = self.graph.entities.get(uuid)
            return dict(found) if found else None
        if "(count ?page)" in query and ':block/title "' in query:
            # The title-uniqueness guard in get_page_uuid.
            title = query.split(':block/title "')[1].split('"')[0]
            return len([e for e in self.graph.entities.values()
                        if e.get("title") == title and e.get("name")])
        if ':block/title "' in query and ":find [(pull ?" in query:
            title = query.split(':block/title "')[1].split('"')[0]
            return [e for e in self.graph.entities.values()
                    if e.get("title") == title and e.get("name")]
        if "[?child :block/parent ?parent]" in query:
            uuid = query.split('#uuid "')[1].split('"')[0]
            entity = self.graph.entities.get(uuid)
            return self.graph.children(entity["id"]) if entity else []
        return []


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def importer(graph):
    return VerifiedImport(FakeClient(graph))  # type: ignore[arg-type]


# ------------------------------------------------------------------ import

async def test_import_builds_the_whole_tree(graph, importer):
    result = await importer.import_page(graph.page["uuid"], SAMPLE)

    assert result.verified is True
    assert result.blocks == 8
    titles = {e["title"] for e in graph.entities.values()}
    assert "Perched in a sunlit range, **Dawnspire** is a marvel." in titles
    assert "## Core Elements" in titles


async def test_import_does_not_mint_pages_for_links(graph, importer):
    """The whole reason the escaping exists. Without it this import creates
    stub pages for Dawnspire, City and Plants."""
    before = {e["title"] for e in graph.entities.values() if e.get("name")}

    await importer.import_page(graph.page["uuid"], SAMPLE)

    after = {e["title"] for e in graph.entities.values() if e.get("name")}
    assert after == before
    assert set(result_names(graph)) >= {"Dawnspire", "City", "Plants"}


def result_names(graph):
    """Names that appear as placeholders rather than as pages."""
    names = []
    for entity in graph.entities.values():
        links, _ = find_placeholders(entity.get("title") or "")
        names.extend(links)
    return names


async def test_import_reports_what_it_escaped(graph, importer):
    result = await importer.import_page(graph.page["uuid"], SAMPLE)

    assert set(result.escaped_links) == {"Dawnspire", "City", "Plants"}
    assert set(result.escaped_tags) == {"Industrial", "fix"}
    assert any("repairLinks" in w for w in result.warnings)


async def test_import_reports_unapplied_page_properties(graph, importer):
    result = await importer.import_page(graph.page["uuid"], SAMPLE)

    assert result.page_properties == {"alias": "City/Dawnspire"}
    assert any("not applied" in w for w in result.warnings)


async def test_import_by_title_creates_the_page(graph, importer):
    result = await importer.import_page("Brand New Page", "- Just one block\n")

    assert result.created_page is True
    assert result.page_title == "Brand New Page"


async def test_import_dry_run_writes_nothing(graph, importer):
    result = await importer.import_page(
        graph.page["uuid"], SAMPLE, dry_run=True)

    assert result.verified is False
    assert result.blocks == 8
    assert graph.children(graph.page["id"]) == []


async def test_import_rejects_a_block_uuid_as_target(graph, importer):
    block = graph.add("a block", graph.page["id"], graph.page["id"])

    with pytest.raises(ValueError, match="block, not a page"):
        await importer.import_page(block["uuid"], SAMPLE)


# ------------------------------------------------------------------ repair

async def test_repair_converts_a_link_whose_target_exists(graph):
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    graph.add("Dawnspire", name="dawnspire", tags=[PAGE_CLASS_ID])
    await verified.import_page(graph.page["uuid"], "- See {{x}}\n")
    block = graph.add("Refers to {{link:Dawnspire}}",
                      graph.page["id"], graph.page["id"])

    result = await verified.repair_links(graph.page["uuid"])

    assert result["verified"] is True
    assert result["blocks_updated"] == 1
    assert "{{link:" not in graph.entities[block["uuid"]]["title"]


async def test_repair_skips_a_missing_target(graph):
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    block = graph.add("Refers to {{link:Nonexistent}}",
                      graph.page["id"], graph.page["id"])

    result = await verified.repair_links(graph.page["uuid"])

    assert result["missing"] == ["Nonexistent"]
    # The placeholder is left in place rather than dropped.
    assert "{{link:Nonexistent}}" in graph.entities[block["uuid"]]["title"]


async def test_creating_missing_pages_needs_an_acknowledgement(graph):
    """Two arguments, because a typo and a genuinely new page look
    identical."""
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    graph.add("Refers to {{link:Nonexistent}}",
              graph.page["id"], graph.page["id"])

    result = await verified.repair_links(
        graph.page["uuid"], create_missing=True)

    assert result["verified"] is False
    assert result["would_create"] == ["Nonexistent"]
    assert "acknowledge_page_creation" in result["diagnostic"]


async def test_acknowledged_creation_proceeds(graph):
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    graph.add("Refers to {{link:Nonexistent}}",
              graph.page["id"], graph.page["id"])

    result = await verified.repair_links(
        graph.page["uuid"], create_missing=True,
        acknowledge_page_creation=True)

    assert "Nonexistent" in result["resolved"]


async def test_bulk_creation_is_capped(graph):
    """A large number of missing targets is more often a broken import than
    an intention."""
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    for n in range(6):
        graph.add(f"Refers to {{{{link:Missing{n}}}}}",
                  graph.page["id"], graph.page["id"])

    result = await verified.repair_links(
        graph.page["uuid"], create_missing=True,
        acknowledge_page_creation=True, max_pages_to_create=5)

    assert result["verified"] is False
    assert "above the limit" in result["diagnostic"]


async def test_near_misses_are_suggested(graph):
    """Surfaced before any acknowledgement: a typo is the common cause of a
    missing target, and creating a page for it is the accident the
    confirmation exists to prevent."""
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    graph.add("Dawnspire", name="dawnspire", tags=[PAGE_CLASS_ID])
    graph.add("Refers to {{link:Dawnspyre}}",
              graph.page["id"], graph.page["id"])

    result = await verified.repair_links(graph.page["uuid"], dry_run=True)

    assert result["did_you_mean"]["Dawnspyre"] == ["Dawnspire"]


async def test_tags_are_opt_in(graph):
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    block = graph.add("Marked {{tag:fix}}", graph.page["id"], graph.page["id"])

    await verified.repair_links(graph.page["uuid"])

    assert "{{tag:fix}}" in graph.entities[block["uuid"]]["title"]


async def test_repair_is_idempotent(graph):
    """A resolved reference is rewritten to [[uuid]] and no longer matches a
    placeholder, so re-running after further imports is safe."""
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    graph.add("Dawnspire", name="dawnspire", tags=[PAGE_CLASS_ID])
    graph.add("Refers to {{link:Dawnspire}}",
              graph.page["id"], graph.page["id"])

    first = await verified.repair_links(graph.page["uuid"])
    second = await verified.repair_links(graph.page["uuid"])

    assert first["blocks_updated"] == 1
    assert second["blocks_updated"] == 0


async def test_graph_wide_scan_reaches_tag_only_pages(graph):
    """Matching only "{{link:" meant a page whose sole placeholders were tags
    was unreachable graph-wide, so include_tags silently did nothing there."""
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]
    block = graph.add("Marked {{tag:fix}}", graph.page["id"], graph.page["id"])

    without = await verified.repair_links(dry_run=True)
    assert without["pages_scanned"] == 0

    with_tags = await verified.repair_links(include_tags=True, dry_run=True)
    assert with_tags["pages_scanned"] == 1
    # The tag does not exist, so it is reported as missing rather than
    # rewritten -- Logseq would mint it on write.
    assert with_tags["tags_missing"] == ["fix"]

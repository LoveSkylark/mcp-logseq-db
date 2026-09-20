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
    parse_blocks,
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
    """And the error names the content that would have been lost, rather than
    just saying nothing was found."""
    with pytest.raises(ValueError, match="would be DISCARDED"):
        parse_markdown("Just a paragraph with no bullet.")


def test_content_before_the_first_bullet_is_refused_not_dropped():
    """This used to append to `warnings` and carry on, so an import could
    discard most of its content and still report verified=true with a
    plausible block count. That happened: six of eight lines lost, because
    the caller's text reached this parser instead of the block-list one.
    Content that cannot be placed is an error, not a footnote."""
    with pytest.raises(ValueError, match="would be DISCARDED") as caught:
        parse_markdown(
            "1. Draw the sigil\n2. Place the lamp\n\n- A real block\n")

    message = str(caught.value)
    assert "line 1" in message
    assert "Draw the sigil" in message
    # And it points at the form that would have kept them.
    assert "block LIST" in message


def test_page_properties_are_still_allowed_before_the_first_bullet():
    """The refusal must not catch the one thing that region is FOR."""
    parsed = parse_markdown("alias:: City/Dawnspire\n\n- A block\n")

    assert parsed.page_properties == {"alias": "City/Dawnspire"}


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


# --------------------------------------- the explicit block list format
#
# The line format infers block boundaries from `- ` and depth from
# indentation, and neither survives content containing newlines. A bulletless
# line is joined to the block above -- which covers a wrapped paragraph -- but
# a blank line is dropped, leading whitespace is stripped, and a line starting
# with `- ` becomes a CHILD. So an eight-step sequence, a markdown table or a
# fenced code block could not be imported as one block, and a real import
# needed a createBlock fallback part-way through.

EIGHT_STEPS = """1. Declare intent
2. Name the stake
3. Roll
   3a. On a tie, the defender chooses
4. Compare

5. Narrate
6. Apply cost
7. Update the clock
8. Ask what changes"""

# The one shape no form can carry. Logseq truncates a block at a line
# beginning with `- ` and discards the rest -- measured 2026-09-20 on
# 2.0.1-alpha+nightly.20260826, where "DASHLINE alpha\n- DASHLINE beta"
# stored only "DASHLINE alpha" with a successful response.
DASH_INSIDE_BLOCK = "Step three\n- 3a. On a tie, the defender chooses"


async def test_an_eight_line_list_imports_as_one_block(graph, importer):
    """The acceptance case. The text carries a numbered list, an indented
    continuation line and a BLANK line -- all three survive verbatim, which
    was confirmed against a live graph rather than assumed."""
    result = await importer.import_page(
        graph.page["uuid"], ["Resolution Sequence", EIGHT_STEPS])

    assert result.verified is True
    assert result.blocks == 2
    stored = [b["title"] for b in graph.children(graph.page["id"])]
    assert EIGHT_STEPS in stored
    kept = next(t for t in stored if t.startswith("1. Declare"))
    assert kept.count("\n") == 9
    assert "\n\n" in kept                 # the blank line survived
    assert "   3a." in kept              # so did the leading whitespace


async def test_a_bullet_line_inside_a_block_is_refused(graph, importer):
    """Refused rather than sent, because Logseq would truncate the block
    there and report success. Eight lines sent, two stored, verified true --
    that happened, and it is the failure this server exists to prevent."""
    with pytest.raises(ValueError, match="truncates a block") as caught:
        await importer.import_page(
            graph.page["uuid"], [DASH_INSIDE_BLOCK])

    # The message has to say what to do instead, since the content is valid
    # and the caller has no other way to know.
    assert "Split it into separate blocks" in str(caught.value)
    assert graph.children(graph.page["id"]) == []


async def test_a_leading_bullet_on_the_first_line_is_fine(graph, importer):
    """Only a line AFTER the first matters -- the first line's bullet is
    stripped by Logseq as decoration, not a truncation point."""
    result = await importer.import_page(
        graph.page["uuid"], ["- A single bulleted line"])

    assert result.verified is True


async def test_the_same_text_in_the_string_form_loses_the_blank_line(
        graph, importer):
    """Pinned to show what the list form is FOR, and corrected: the string
    form does not fragment this text, it QUIETLY REWRITES it -- the blank
    line is dropped and every continuation line is stripped of indentation.
    """
    result = await importer.import_page(
        graph.page["uuid"], f"- Resolution Sequence\n  {EIGHT_STEPS}")

    assert result.blocks == 1
    stored = graph.children(graph.page["id"])[0]["title"]
    assert "\n\n" not in stored          # blank line gone
    assert "   3a." not in stored        # indentation stripped
    assert "3a." in stored               # the text itself survived


async def test_depth_is_explicit_in_the_list_form(graph, importer):
    await importer.import_page(graph.page["uuid"], [
        "Parent",
        {"text": "Child", "depth": 1},
        {"text": "Grandchild", "depth": 2},
        {"text": "Second parent", "depth": 0},
    ])

    roots = graph.children(graph.page["id"])
    assert {b["title"] for b in roots} == {"Parent", "Second parent"}
    parent = next(b for b in roots if b["title"] == "Parent")
    child = graph.children(parent["id"])[0]
    assert child["title"] == "Child"
    assert graph.children(child["id"])[0]["title"] == "Grandchild"


async def test_a_skipped_depth_is_an_error_not_a_warning(graph, importer):
    """The line format flattens a ragged indent and warns, because whitespace
    can be accidentally ragged. An explicit integer cannot, so a gap here is a
    mistake and is refused."""
    with pytest.raises(ValueError, match="skips a level"):
        await importer.import_page(graph.page["uuid"], [
            "Parent", {"text": "Too deep", "depth": 2}])


async def test_references_are_escaped_in_the_list_form_too(graph, importer):
    """Logseq parses content on write whatever route it arrived by, so the
    escaping is not a property of the string parser."""
    result = await importer.import_page(
        graph.page["uuid"], ["see [[Dawnspire]] and #mist"])

    assert result.escaped_links == ("Dawnspire",)
    assert result.escaped_tags == ("mist",)
    stored = graph.children(graph.page["id"])[0]["title"]
    assert "{{link:Dawnspire}}" in stored
    assert "[[Dawnspire]]" not in stored


async def test_a_code_block_survives_the_list_form(graph, importer):
    fenced = "```python\ndef f():\n    return 1\n```"

    await importer.import_page(graph.page["uuid"], [fenced])

    assert graph.children(graph.page["id"])[0]["title"] == fenced


async def test_an_empty_element_is_refused(graph, importer):
    with pytest.raises(ValueError, match="empty"):
        await importer.import_page(graph.page["uuid"], ["Real", "   "])


async def test_a_malformed_element_is_refused(graph, importer):
    with pytest.raises(ValueError, match="must be a string or an"):
        await importer.import_page(graph.page["uuid"], ["Real", 42])


async def test_an_empty_list_is_refused(graph, importer):
    with pytest.raises(ValueError, match="non-empty"):
        await importer.import_page(graph.page["uuid"], [])


async def test_a_dry_run_works_on_the_list_form(graph):
    client = FakeClient(graph)
    verified = VerifiedImport(client)  # type: ignore[arg-type]

    result = await verified.import_page(
        graph.page["uuid"],
        ["Parent", {"text": "Child", "depth": 1}],
        dry_run=True)

    assert result.verified is False
    assert result.blocks == 2
    assert not any(m == "logseq.DB.insertBatchBlock"
                   for m, _ in client.calls)


def test_the_list_form_does_not_parse_page_properties():
    """There is no "before the first bullet" region, so `key:: value` is just
    block content. Silently treating it as a property would be worse."""
    parsed = parse_blocks(["alias:: City/Dawnspire"])

    assert parsed.page_properties == {}
    assert parsed.blocks[0].content == "alias:: City/Dawnspire"


async def test_a_list_that_arrives_as_a_json_string_is_still_a_list(
        graph, importer):
    """THE DEFECT THIS PINS. A client asked for the list form and the
    argument arrived JSON-encoded. Dispatching on type alone sent it to the
    markdown parser, which read the JSON as markdown and discarded six of
    eight lines -- reporting verified=true with a plausible block count.

    The block list and the markdown string are different CONTRACTS, so the
    one branch that decides which applies must not be decided by how a client
    happened to serialise the argument.
    """
    import json as _json

    result = await importer.import_page(
        graph.page["uuid"],
        _json.dumps(["Resolution Sequence", EIGHT_STEPS]))

    assert result.verified is True
    assert result.blocks == 2
    stored = [b["title"] for b in graph.children(graph.page["id"])]
    assert EIGHT_STEPS in stored          # byte-exact, nothing dropped
    assert any("JSON string" in w for w in result.warnings)


async def test_real_markdown_is_not_mistaken_for_a_json_list(graph, importer):
    """The coercion keys on parsing as a JSON ARRAY, so markdown that merely
    starts with a bracket stays markdown."""
    result = await importer.import_page(
        graph.page["uuid"], "- [[Dawnspire]] is a city\n")

    assert result.blocks == 1
    assert result.escaped_links == ("Dawnspire",)


async def test_the_string_form_still_works(graph, importer):
    """The existing format is unchanged -- adding a second one must not
    migrate anybody."""
    result = await importer.import_page(
        graph.page["uuid"], "- Parent\n  - Child\n")

    assert result.verified is True
    assert result.blocks == 2


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

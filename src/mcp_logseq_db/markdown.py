"""
Logseq markdown parsing, and the escaping an import needs.

WHY ESCAPING IS NEEDED
----------------------
Logseq parses block content on write rather than storing it literally. Three
behaviours were confirmed against a live graph:

    "## X"      -> content "X" plus :logseq.property/heading 2   (wanted)
    "[[X]]"     -> MINTS A PAGE, rewrites the text to [[uuid]]
    "#X"        -> MINTS A TAG, tags the block, rewrites to #[[uuid]]

The last two are why an import cannot pass content through verbatim. A page
whose links point at pages that do not exist yet would create a stub for every
one of them, and every inline #word would become a tag. That is precisely how
a graph accumulates duplicate stubs from a bad import.

So references are neutralised on the way in and repaired afterwards, once the
targets exist and the caller has decided which tags they actually want:

    [[X]]      ->  {{link:X}}
    #X         ->  {{tag:X}}
    #[[X Y]]   ->  {{tag:X Y}}

The `link:` and `tag:` prefixes are deliberate rather than a bare `{{X}}`, so
the repair pass only ever touches placeholders it wrote -- Logseq has real
macros ({{query}}, {{embed}}, {{cloze}}) that a source page may contain.

Headings are NOT escaped: Logseq's conversion is what we want and it happens
on insert for free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ._shared import reject_truncating_content

BULLET = re.compile(r"^(?P<indent>[\t ]*)-\s(?P<content>.*)$")
PAGE_PROPERTY = re.compile(r"^(?P<key>[A-Za-z][\w.-]*)::\s*(?P<value>.*)$")

# Bracketed tag first: #[[Multi Word]] must not be read as # beside a link.
TAG_BRACKETED = re.compile(r"#\[\[(?P<name>[^\]]+)\]\]")
LINK = re.compile(r"\[\[(?P<name>[^\]]+)\]\]")
# Not preceded by a word character, so "C#" and "issue#4" are left alone.
TAG_BARE = re.compile(r"(?<![\w#])#(?P<name>[A-Za-z0-9_/-]+)")

LINK_PLACEHOLDER = re.compile(r"\{\{link:(?P<name>[^}]+)\}\}")
TAG_PLACEHOLDER = re.compile(r"\{\{tag:(?P<name>[^}]+)\}\}")

MAX_IMPORT_BLOCKS = 500


def escape_references(content: str) -> tuple[str, list[str], list[str]]:
    """
    Return (escaped, links, tags).

    Order matters: bracketed tags are consumed before plain links, or
    `#[[X]]` would be seen as a `#` next to a link.
    """
    links: list[str] = []
    tags: list[str] = []

    def bracketed_tag(match: re.Match) -> str:
        name = match.group("name").strip()
        tags.append(name)
        return "{{tag:" + name + "}}"

    def link(match: re.Match) -> str:
        name = match.group("name").strip()
        links.append(name)
        return "{{link:" + name + "}}"

    def bare_tag(match: re.Match) -> str:
        name = match.group("name").strip()
        tags.append(name)
        return "{{tag:" + name + "}}"

    content = TAG_BRACKETED.sub(bracketed_tag, content)
    content = LINK.sub(link, content)
    content = TAG_BARE.sub(bare_tag, content)
    return content, links, tags


def find_placeholders(content: str) -> tuple[list[str], list[str]]:
    """Return (link names, tag names) present in already-escaped content."""
    return ([m.group("name") for m in LINK_PLACEHOLDER.finditer(content)],
            [m.group("name") for m in TAG_PLACEHOLDER.finditer(content)])


def restore_reference(content: str, name: str, *, is_tag: bool) -> str:
    """
    Turn one placeholder back into live syntax.

    One name at a time, so a block holding several links can be repaired
    partially -- the ones whose targets exist are restored and the rest stay
    escaped, rather than the whole block being skipped.

    `#[[name]]` rather than `#name` for tags: the bracketed form survives a
    name containing spaces, and Logseq treats the two identically.
    """
    pattern = re.escape("{{tag:" + name + "}}" if is_tag
                        else "{{link:" + name + "}}")
    replacement = f"#[[{name}]]" if is_tag else f"[[{name}]]"
    return re.sub(pattern, replacement.replace("\\", "\\\\"), content)


@dataclass
class ParsedBlock:
    content: str
    line: int
    children: list["ParsedBlock"] = field(default_factory=list)


@dataclass
class ParsedPage:
    page_properties: dict[str, str]
    blocks: list[ParsedBlock]
    links: list[str]
    tags: list[str]
    warnings: list[str]

    def block_count(self) -> int:
        def walk(blocks: list[ParsedBlock]) -> int:
            return sum(1 + walk(b.children) for b in blocks)
        return walk(self.blocks)

    def call_count(self) -> int:
        """One insert per parent that has children, plus the page itself."""
        def walk(blocks: list[ParsedBlock]) -> int:
            return sum((1 if b.children else 0) + walk(b.children)
                       for b in blocks)
        return walk(self.blocks) + (1 if self.blocks else 0)


def parse_blocks(
    blocks: list[Any], *, escape: bool = True
) -> ParsedPage:
    """
    Parse an EXPLICIT block list, where one element is one block.

    WHY THIS EXISTS. The line format infers block boundaries from `- ` and
    depth from indentation, and neither survives content that contains
    newlines. A bulletless line is joined to the block above it, which covers
    a wrapped paragraph and a numbered list -- but a blank line inside a block
    is dropped, leading whitespace is stripped, and any line that happens to
    begin with `- ` becomes a CHILD BLOCK. So an eight-step sequence with a
    nested bullet, a markdown table, or a fenced code block cannot be
    expressed at all, and an import of real manuscript content needed a
    `createBlock` fallback mid-way.

    Here nothing is inferred. Each element is one block, its text is used
    verbatim, and DEPTH IS EXPLICIT -- required rather than read from leading
    whitespace, because with multi-line values indentation no longer
    distinguishes structure from content. That is the whole point: the two
    formats answer the same question differently, and mixing them would put
    the ambiguity straight back.

    ONE SHAPE IS STILL IMPOSSIBLE, and it is Logseq's limit rather than this
    parser's: a line beginning with `- ` inside a block is truncated away on
    write. It is refused below rather than sent. Newlines, blank lines and
    leading whitespace all survive -- measured, not assumed.

    An element is either a string (depth 0) or a mapping with `text` and
    `depth`. `content` is accepted in place of `text`, since that is what the
    underlying API calls the field.

    Page properties are not parsed: there is no "before the first bullet"
    region to hold them. References are still escaped -- Logseq parses content
    on write whatever route it arrived by.
    """
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("markdown must be a non-empty string or block list")

    warnings: list[str] = []
    all_links: list[str] = []
    all_tags: list[str] = []
    roots: list[ParsedBlock] = []
    stack: list[ParsedBlock] = []

    for index, element in enumerate(blocks):
        position = index + 1
        if isinstance(element, str):
            text, depth = element, 0
        elif isinstance(element, dict):
            raw = element.get("text", element.get("content"))
            if not isinstance(raw, str):
                raise ValueError(
                    f"block {position}: needs a 'text' string")
            depth = element.get("depth", 0)
            if isinstance(depth, bool) or not isinstance(depth, int):
                raise ValueError(
                    f"block {position}: 'depth' must be an integer")
            if depth < 0:
                raise ValueError(
                    f"block {position}: 'depth' cannot be negative")
            text = raw
        else:
            raise ValueError(
                f"block {position}: each element must be a string or an "
                "object with 'text' and 'depth'")

        if not text.strip():
            raise ValueError(
                f"block {position}: empty. An explicit list says what the "
                "blocks are, so an empty one is a mistake rather than "
                "something to skip silently.")

        # A LINE BEGINNING WITH `- ` CANNOT BE BLOCK CONTENT. The rule lives
        # in `_shared` because it is Logseq's, not this parser's, and
        # `createBlock` and `updateBlock` need it too -- they take multi-line
        # titles and had no guard at all. One copy, one measurement.
        reject_truncating_content(text, role=f"block {position}")

        # A jump is an ERROR here, unlike the line format where it is a
        # warning: indentation can be accidentally ragged, an integer cannot.
        if depth > len(stack):
            raise ValueError(
                f"block {position}: depth {depth} skips a level -- the "
                f"deepest available here is {len(stack)}. Depth is explicit "
                "in this form, so a gap is a mistake rather than something "
                "to flatten.")

        if escape:
            text, links, tags = escape_references(text)
            all_links.extend(links)
            all_tags.extend(tags)

        block = ParsedBlock(content=text, line=position)
        del stack[depth:]
        (stack[-1].children if stack else roots).append(block)
        stack.append(block)

    total = sum(1 for _ in _walk(roots))
    if total > MAX_IMPORT_BLOCKS:
        raise ValueError(
            f"{total} blocks exceeds the {MAX_IMPORT_BLOCKS}-block import "
            "limit. Split the page.")

    return ParsedPage({}, roots, all_links, all_tags, warnings)


def parse_markdown(text: str, *, escape: bool = True) -> ParsedPage:
    """
    Parse Logseq markdown into a block tree.

    Three things a naive indent reader gets wrong:

      - Indentation is measured BEFORE the bullet. "\\t- x" is depth 1.
      - A line with no bullet continues the previous block. Without that, a
        wrapped paragraph silently becomes a child block.
      - Page properties stop at the first bullet. The same `key:: value`
        syntax below it is block content.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("markdown must be a non-empty string")

    lines = text.splitlines()
    properties: dict[str, str] = {}
    warnings: list[str] = []
    all_links: list[str] = []
    all_tags: list[str] = []

    start = len(lines)
    ignored: list[str] = []
    for index, raw in enumerate(lines):
        if BULLET.match(raw):
            start = index
            break
        stripped = raw.strip()
        if not stripped:
            continue
        match = PAGE_PROPERTY.match(stripped)
        if match:
            properties[match.group("key")] = match.group("value").strip()
        else:
            ignored.append(f"line {index + 1}: {stripped[:60]!r}")

    # REFUSED rather than warned. This used to append to `warnings` and carry
    # on, so an import could drop most of its content and still return
    # verified=true with a plausible block count -- the silent content loss
    # this whole server exists to prevent, and it happened: six of eight
    # lines discarded because the caller's text reached this parser instead
    # of the block-list one. Content the caller sent and this cannot place is
    # an error, not a footnote.
    if ignored:
        raise ValueError(
            f"{len(ignored)} line(s) before the first '- ' are neither page "
            "properties nor blocks, and would be DISCARDED: "
            + "; ".join(ignored[:3])
            + (" ..." if len(ignored) > 3 else "")
            + ". Every block line must begin with '- '. If these lines belong "
            "inside one block, pass a block LIST instead -- its text is used "
            "verbatim.")

    # Indent unit comes from the first indented bullet, so tabs and spaces
    # both work provided the source is internally consistent.
    unit = 0
    for raw in lines[start:]:
        match = BULLET.match(raw)
        if match and match.group("indent"):
            unit = len(match.group("indent").replace("\t", " "))
            break
    unit = unit or 1

    roots: list[ParsedBlock] = []
    stack: list[ParsedBlock] = []
    current: ParsedBlock | None = None

    for offset, raw in enumerate(lines[start:], start=start):
        lineno = offset + 1
        if not raw.strip():
            continue

        match = BULLET.match(raw)
        if not match:
            if current is not None:
                current.content += "\n" + raw.strip()
            else:
                warnings.append(
                    f"line {lineno}: content outside any block was ignored")
            continue

        depth = len(match.group("indent").replace("\t", " ")) // unit
        content = match.group("content").strip()
        if not content:
            warnings.append(f"line {lineno}: empty block skipped")
            continue

        if escape:
            content, links, tags = escape_references(content)
            all_links.extend(links)
            all_tags.extend(tags)

        if depth > len(stack):
            warnings.append(
                f"line {lineno}: indents {depth - len(stack)} levels at once; "
                "treated as one")
            depth = len(stack)

        block = ParsedBlock(content=content, line=lineno)
        del stack[depth:]
        (stack[-1].children if stack else roots).append(block)
        stack.append(block)
        current = block

    if not roots:
        raise ValueError(
            "No blocks found. Every block line must begin with '- '; "
            "indentation alone does not create one.")

    total = sum(1 for _ in _walk(roots))
    if total > MAX_IMPORT_BLOCKS:
        raise ValueError(
            f"{total} blocks exceeds the {MAX_IMPORT_BLOCKS}-block import "
            "limit. Split the page.")

    return ParsedPage(properties, roots, all_links, all_tags, warnings)


def _walk(blocks: list[ParsedBlock]):
    for block in blocks:
        yield block
        yield from _walk(block.children)

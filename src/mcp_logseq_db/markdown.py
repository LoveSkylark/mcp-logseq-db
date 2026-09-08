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
            warnings.append(
                f"line {index + 1}: text before the first block was ignored: "
                f"{stripped[:40]!r}")

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

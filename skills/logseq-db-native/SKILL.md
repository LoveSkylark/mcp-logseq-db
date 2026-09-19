---
name: logseq-db-native
description: "Use when reading or modifying a Logseq 2.x DB graph through the mcp-logseq-db server. Covers DB queries, exact identifiers, verified writes, the property namespace sandbox, and recovery from ambiguous results. Never use file-graph or non-DB Logseq tools."
---

# Logseq DB-Native MCP

For `mcp-logseq-db` against a Logseq 2.x **DB** graph. Do not load a
file-graph or legacy Logseq skill in the same conversation.

## The governing fact

**This API returns success for calls that do nothing.**

A wrong identifier type, a name where a UUID belongs, or an unsupported
combination produces `null` or `{:block 1}` — the same responses a successful
write produces. Nothing at the transport layer distinguishes them.

Observed:

| Call | Response | What happened |
| --- | --- | --- |
| `page-id` set to a page *name* | `{:block 1}` | nothing created |
| `removeProperty` given a UUID | `null` | nothing removed |
| `upsertProperty` with an explicit ident | full entity | ident silently discarded |
| `removeBlock` given a UUID | `null` | block actually deleted |

Every rule below follows from this. In particular:

**Every write tool already performs the second call.** A tool does not just
send the mutation — it reads the target back afterwards and compares. So
`verified: true` means the change was observed in the graph, not that the API
said so.

What follows from that is a rule about the RESPONSE, not the tool: a raw
Logseq response of `null` or `{:block 1}` is returned by writes that landed and
by writes that did nothing, so it is never evidence. Read `verified`.
`verified: false` means the write did not take effect even though no error was
raised — report it as a failure, not a success with a caveat.

## Identifiers

Each entity kind has one canonical key. The wrong one fails silently.

| Kind | Key | Resolve with |
| --- | --- | --- |
| page | `:block/uuid` | `getPageUUID(title)` |
| block | `:block/uuid` | `getBlockUUID(page_uuid)` |
| tag | UUID for relations | `getTagUUID(title)` |
| property | `:db/ident` — a UUID here is a no-op | `getPropertyIndent(title)` |

`:db/id` integers appear in results. They are internal, renumbered when a graph
is rebuilt, and are never accepted as identifiers. Do not persist them.

Resolve titles to identifiers **before** any write, and never select a
destructive target from an ambiguous result. The resolver tools return
`found: false` with candidates rather than guessing; treat that as a stop.

## A page is a block

Pages, blocks, tags, and properties share one entity store. A page carries
`:block/name` and the `:logseq.class/Page` tag and has no `:block/parent` —
otherwise it is a block.

This is why there is one `addTag` and not an `addPageTag` and an
`addBlockTag`: the target is uniform. Same for `addProperty`. When reading a
result, `:block/name` present means page.

Three attributes reach different things and are not interchangeable:

- `:block/parent` — direct children, one level
- `:block/page` — every block on the page, any depth
- `{:block/_parent ...}` — recursive pull, whole tree

A page's own tags and its blocks' tags are separate queries. So are properties
that hold a value and properties that are merely *declared* by the page's
classes — the latter have no datoms and appear in no query over the page.
`inspectPage` separates these with its `detail` selector for that reason.

**`:block/page` can disagree with `:block/parent`, and that is harmless.**
On some graphs a block's `:block/page` points at an ancestor block rather than
at the page. Logseq renders the outline from `:block/parent`, so such blocks
appear normally in the UI and nothing is lost. `pageStats` and `findOrphans`
report the count because it explains why a `:block/page` query returns fewer
blocks than expected — **not** because anything needs fixing.

Do not offer to repair it, and do not move blocks to "correct" it. Moving a
block rewrites its `:block/order`, so a bulk repair reorders content for no
benefit. This was learned the expensive way: a repair tool was built on the
assumption that these blocks were invisible, and roughly 1,500 blocks were
moved before anyone opened the page and saw the text rendering fine.

## A page is never empty

`createPage` seeds every new page with one empty block, and Logseq leaves a
trailing empty block behind ordinary editing. So a block count on a page made
through this API is never 0, and every count of "how much is on this page" is
high by however many empty blocks exist.

This matters most where a count feeds a decision. The rule that emptiness
alone is never grounds to delete has a converse: a genuinely empty page still
reports a block, so an audit searching for empty pages by `own_blocks == 0`
finds nothing at all.

`pageStats` therefore reports three figures rather than one:

- `own_blocks` — every block with this `:block/page`, seeds included. The raw
  figure; do not read it as content.
- `empty_blocks` — those whose title is empty.
- `content_blocks` — the difference, and **the figure to pair with a reference
  count** when judging whether a page carries anything.

The split is reported rather than silently subtracted, because an empty block
someone typed is indistinguishable from a seeded one, and hiding the
difference would trade a known overcount for an invisible undercount.

The same reasoning applies to verifying a write: check the **delta** a call
produced, not the absolute total. `importPage` takes a block count before and
after and compares the gain against the markdown, which is immune to seed
blocks and to whatever the page already held. An absolute comparison on an
append cannot distinguish a successful import from a silent no-op.

## Cost lives at the tool boundary

What costs you is what crosses in and out — arguments and results — not the
work a tool does internally. `importPage` on a 68-block page makes 34
`insertBatchBlock` calls inside the server and returns one summary; building
the same page with `createBlock` would be 68 requests and 68 responses.

So prefer the tool that loops internally over the loop you write yourself:

- `repairLinks()` with no page over one call per page
- `importPage` over a `createBlock` per line
- `pageStats` over `inspectPage` when you only need counts
- `clearPage` over a `removeBlock` per block

And when a tool does return something proportional to the damage — a plan, a
list of orphans — read the summary rather than the list unless you intend to
act on each item.

This also means slowness is not cost. A sweep that takes a minute of internal
calls is cheap; the same work as individual tool calls is not.

## Start with capabilities

Call `capabilities` once near the start. It reports **tools**, not API methods,
each with a state and any constraints that apply:

- `available` — probed and working
- `unavailable` — probed and confirmed absent
- `unknown` — **the probe was inconclusive, not that the tool is missing.** Try
  it and check `verified` in the result.

Read `constraints` on a tool before using it. Availability alone is not enough:
`addProperty` is available and will still do nothing on a user-namespace ident.

If `graph.version_matches` is false, the connected Logseq is not the build
these tools were verified against. Say so, and rely on read-back rather than
on any claim in this skill.

`include_diagnostics=true` exposes the underlying routes. That is for
debugging the server, not for planning work — and never call a `logseq.*`
method directly.

## Reading

Prefer the narrowest tool that answers the question.

1. `getPageUUID(title)` → `inspectPage(uuid, detail)` where detail is
   `page`, `blocks`, `tags`, `properties`, `declared`, or `all`. It is called
   `inspectPage` rather than `getPage` because it returns far more than a page
   entity.
2. `getBlockUUID(page_uuid)` lists every block on a page at any depth.
   `getBlock(uuid)` reads one; `getBlockTree(uuid)` reads a subtree and reports
   `truncated` when a bound stopped it. Both walk `:block/parent`, so a block
   whose `:block/page` is wrong still appears.
3. `findBacklinks(uuid)` answers "what refers to this?" for a page, block or
   tag. It reports three mechanisms separately: `refs` (what Logseq's backlink
   panel counts), `tagged`, and `property_values`. A property value is a
   reference in the DB but does **not** appear in the UI panel, so the totals
   will not match what the user sees. Run it before deleting anything —
   nothing rewrites references on delete.
4. `findOrphans(page_uuid)` reports blocks whose `:block/page` differs from
   their nearest ancestor page. **This is not damage.** Logseq renders the
   outline from `:block/parent`, so those blocks display normally and are
   reachable in the UI — only a query written against `:block/page` misses
   them. Treat it as an explanation for a surprising query result, never as a
   repair signal.
5. `pageStats(page_uuid)` returns counts only — own blocks, subtree blocks,
   nested pages, refs, tag holders, property values, and a `true_orphans`
   count that is informational rather than a fault. Prefer it for triage:
   every other read returns payload proportional to page size, so auditing
   many pages with them is expensive and this is not.
6. `getTagUsers(tag_uuid)` and `getProperyUsers(ident)` answer "what uses
   this?" — run either before deleting, and report the count to the user.
7. The `list*` tools take no arguments and return a whole kind.

Keep `uuid` and `ident` in the working plan. Do not reduce an entity to its
display text; titles are not unique and are not identifiers.

## Writing

State this before each mutation:

```text
target:        exact title plus UUID or ident
current state: what a read shows now
operation:     the exact tool
requested:     the exact value or relationship
reversibility: the undo tool, or that there is none
```

Ask for confirmation before `deleteProperty`, `deleteTag`, `removeBlock` on a
block with children, or any change across multiple entities.

After writing, check `verified`. On `verified=false`, read `previous_state` and
`observed_state`: they distinguish "nothing happened" from "something else
happened", and the usual cause is an identifier of the wrong type.

`dry_run` checks arguments and target existence. Most tools validate locally
only — `createPage` in particular, since its route has no server-side dry run.
Either way it does not tell you a write will land, so do not report a
successful dry run as though the change were made.

### Pages

`createPage(title)` rejects a title already held by any page, tag or block —
the three share one title space. Its route is idempotent on title, so a repeat
returns the existing page rather than duplicating it.

`renamePage(page_uuid, new_title)` verifies by UUID, not by the new title —
reading back by title cannot distinguish a rename from Logseq having created a
second page and left the original alone.

`deletePage(page_uuid)` recycles. Run it knowing the page survives and its
inbound references do not move.

`clearPage(page_uuid)` empties a page but keeps it, along with its tags and
property values. It is one call per top-level block, since the API has no batch
delete, so it is slow on a large page.

### Blocks

`createBlock(parent_uuid, title)` — the parent may be a **page** UUID for a
top-level block or a **block** UUID to nest. It will not resolve a title.

Only the title can be set at creation; tags and position are follow-up calls.
The write is verified against **both** `:block/parent` and `:block/page`,
because a block can end up under the right parent while belonging to the wrong
page — a real child that no page-scoped query can see.

`createPageofBlocks` builds an indented outline at one call per parent that
has children. Duplicate titles among siblings are fine.

### Importing a whole page

`importPage(target, markdown)` is the right tool when you have page content in
Logseq markdown. `target` is a page UUID to write into, or a title to create.
One call carries the whole page, so a 68-block page costs one tool call rather
than 68.

> **Logseq parses content on write.** `## X` becomes a native heading — which
> is wanted — but `[[X]]` MINTS A PAGE and `#X` MINTS A TAG, rewriting the text
> to point at the new entity. An import whose links point at pages that do not
> exist yet would create a stub for every one.
>
> The heading conversion is structural, not cosmetic: the `##` is stripped
> from the text and re-expressed as `:logseq.property/heading 2` on the block,
> which is how the UI renders it as a header. So a heading block's `title`
> reads as plain text with no markup — that is correct and expected, not a
> sign the conversion failed. Two consequences: `inspectPage` at
> `detail=blocks` does NOT return the attribute, so use `getBlock` to see it;
> and because `:logseq.property/` is outside the writable namespace, a heading
> level cannot be set or changed with `addProperty`. The only way to create
> one is to let the parser do it by writing `- ## Text`.
>
> So `importPage` escapes them: `[[X]]` → `{{link:X}}` and `#X` → `{{tag:X}}`.
> The content is inert until repaired. **Tell the user this** — the links they
> wrote will not work until the second step.

`repairLinks()` is that second step. Omit the page UUID to scan the whole
graph, which is usually right: links resolve only once their targets have been
imported, so the natural order is import everything, then repair once.

It is safe to re-run. Names that match no target are skipped and reported —
links under `missing` with near-miss suggestions, tags under `tags_missing` —
and names matching several candidates are skipped rather than guessed.

**Nothing is created without an explicit acknowledgement, and the two kinds
are separate flags.** Pages need `create_missing` plus
`acknowledge_page_creation`; tags need `create_missing` plus
`acknowledge_tag_creation`. Both are capped. `acknowledge_page_creation` does
not cover tags — a page and a tag are different entity kinds, and approval of
one is not approval of the other. Tags are still opt-in via `include_tags`.

> **Always dry-run first and show the user what would be created.** Run
> `repairLinks(dry_run=true)` and read `missing` and `tags_missing`: those are
> the names that do not exist yet. Name them to the user and get agreement
> before setting any acknowledgement flag. A typo, a rename, and a genuinely
> new entity are indistinguishable from here, so this is the user's call and
> not yours. Offering to repair is not the same as offering to create.

The safe default needs no flags at all: with neither acknowledgement set, a
repair rewrites only the placeholders whose targets already exist and leaves
the rest in place, which is always a valid state to stop in and can be resumed
after the missing entities are created deliberately.

Historical note, in case an older build is in use: the tag branch once skipped
resolution entirely and rewrote every tag placeholder unchecked, which made
Logseq mint a tag for each name that did not exist. Those creations were
reported in no bucket of the result — `resolved` came back empty even for tags
that did resolve. If a repair reports tag activity with no `tags_resolved` or
`tags_missing` keys, that is the old behaviour and `include_tags` is unsafe on
that build.

Page properties (`key:: value` above the first block) are parsed and reported
but not applied: they are outside the writable namespace.

**Batches are not atomic.** An outline several levels deep is several calls, so
a failure partway leaves earlier levels committed. The result names the level
that failed; treat it as a partial write and audit with `findOrphans` rather
than retrying, which would duplicate what already landed.

`moveBlock(block_uuid, target_uuid, placement)` relocates a block and its
subtree — `child`, `before` or `after`. Confirmed working on all placements,
including across pages. The API returns nothing, so the tool verifies the new
parent, the owning page, and that descendants followed.

Three behaviours matter when using it directly:

- **It no-ops when the position would not change.** Moving a block to the
  parent it already has does nothing, and comes back `verified: false` with a
  silent-no-op diagnostic. That is the tool being honest, not failing.
- **`child` prepends.** Moving several siblings left to right with `child`
  reverses them. Use `after <previous sibling>` for all but the first.
- **A move carries the subtree**, and descendants' `:block/page` follows.

Those three are already encoded in `repairOrphans`; you only need them when
moving blocks by hand.

`removeBlock` deletes the subtree and verifies every descendant is gone.

### Properties

Two different things share the word:

- **definition** — `createProperty(title, schema)` / `deleteProperty(ident)`
- **value on a target** — `addProperty(uuid, ident, value)` / `removeProperty(uuid, ident)`

`createProperty` takes a plain title. A namespaced string is rejected as a page
name. The ident is assigned by Logseq; retain the one returned.

> **Namespace sandbox.** Writes reach only `plugin.property.<caller>/*`.
> Properties created in the Logseq UI live under `user.property/*` and are
> readable but **not writable**. Built-ins under `:logseq.property/` are also
> outside the sandbox. This is a Logseq restriction, not a server limitation —
> do not look for a workaround, and tell the user plainly.

Reference-typed properties (`node`, `page`, `class`, `property`) take an entity
id, not a literal — a string is accepted by Logseq and mints a value entity
named after it, which reads back as success while pointing at nothing. The
tool refuses that before the call.

Cardinality-many properties accumulate rather than replace, so writing the
same value twice would add a duplicate. The tool detects that and skips the
write.

`deleteProperty` removes the definition graph-wide and takes every value with
it. Recreating mints a new entity — the old values do not return. It requires
`acknowledge_value_loss` when anything holds a value, and sweeps the value
blocks the removal orphans.

### Tags

A tag must exist before it can be attached, and this is enforced on the repair
path too: `repairLinks` resolves every tag placeholder before rewriting it and
skips the ones that do not exist, because Logseq mints a tag for any `#name`
it parses on write. Create the tag deliberately with `creatTag` first, or pass
`acknowledge_tag_creation` once the user has approved the specific names.

`creatTag(title)` creates one. Its
ident is deterministic — `:plugin.class.<caller>/<Title>`, spaces stripped — so
it need not be read back. Tags made in the Logseq UI land under `user.class/*`
and DO carry a random suffix.

Tags and pages share one title space, so `creatTag` refuses a title an existing
page holds, and `createPage` refuses one a tag holds.

`addTag(target, tag)` and `removeTag(target, tag)` take two UUIDs, **target
first**. Removal affects that one relation only.

`deleteTag` works and cascades cleanly: `:block/tags` and `:block/refs` are
cleared on everything that carried the tag. Because that touches many entities
and cannot be undone, it requires `acknowledge_detach` when anything holds the
tag, and `acknowledge_child_reparent` when child tags would move. Run
`getTagUsers` first and report what will be affected.

## Constraints worth stating to the user

**Recycled pages survive.** Deleting a page adds
`:logseq.property/deleted-at` and keeps the UUID, tags, refs, and blocks.
`listPages` excludes them; `listRecycled` shows them. Inbound references are
not rewritten.

**Ordering is fractional.** `:block/order` is a string (`a0`, `a1`, `a0V`) that
sorts lexicographically. Sort by it — pull does not guarantee order. There is
no reindex operation and none is needed.

**Property values are blocks.** Reference-typed values are materialized as
blocks on the holder's page, so they appear in block listings. `clearPage`
identifies and preserves them; nothing else should assume every block on a page
is content.

**`[[Page]]` written through the API is NOT inert.** Logseq parses content on
write: `[[X]]` creates a page if X does not exist, rewrites the stored text to
`[[uuid]]`, and adds a `:block/refs` entry. `#X` does the same for a tag and
tags the block as well. This is why `importPage` escapes both — an import
whose links point at pages that do not exist yet would create a stub for every
one of them.

**`listClosedValues` works.** `Status` and `Priority` carry permitted values
on a mature graph — six and four respectively. A freshly created graph has
none, so an empty result means this graph has no enums rather than that the
feature is missing. Both are built-ins and outside the sandbox, so they remain
read-only.

**A dry run is not a write.** `dry_run` returns `verified: false` by design.
It validates the payload, not the transaction: a graph carrying invalid
entities passes validation and still rejects the real write.

**Deleting a page recycles it.** `deletePage` does not destroy the entity: the
page keeps its UUID, tags, refs and blocks, and stops appearing in
`listPages`. Inbound references are **not** rewritten, so anything linking to
it keeps pointing at a page the user can no longer find. `deletePage` refuses
until `acknowledge_reference_rewrite` is set when references exist — surface
that to the user rather than setting it reflexively.

**`moveBlock` is confirmed working** on all placements, including across
pages. Three behaviours to know when calling it directly: it no-ops when the
position would not change (reported as `verified: false`, which is correct),
`placement=child` prepends, and a move carries the subtree.

## Tools

**Reads** — `capabilities`, `getPageUUID`, `inspectPage`, `pageStats`,
`getBlockUUID`, `getBlock`, `getBlockTree`, `findBacklinks`, `findOrphans`,
`getTagUUID`, `getTag`, `getTagUsers`, `getPropertyIndent`, `getProperyUsers`

**Lists** (no arguments) — `listPages`, `listJournals`, `listTags`,
`listProperties`, `listClosedValues`, `listOrphanTags`,
`listOrphanProperties`, `listAssets`, `listStatus`, `listRecycled`

**Writes** — `importPage`, `repairLinks`, `createPage`, `renamePage`,
`deletePage`, `clearPage`, `createBlock`, `createPageofBlocks`, `updateBlock`,
`moveBlock`,
`removeBlock`, `creatTag`, `deleteTag`, `addTag`, `removeTag`,
`createProperty`, `deleteProperty`, `addProperty`, `removeProperty`

Creating tools require an acknowledgement when an entity would be minted:
`repairLinks` (`acknowledge_page_creation` for pages,
`acknowledge_tag_creation` for tags, each alongside `create_missing`). Preview
with `dry_run` and name the entities to the user first.

Destructive tools require an acknowledgement when anything is affected:
`deletePage` (`acknowledge_reference_rewrite`), `deleteTag`
(`acknowledge_detach`, `acknowledge_child_reparent`), `deleteProperty`
(`acknowledge_value_loss`). Report what will be affected and let the user
decide — do not set these on their behalf.

Call only these names. Never emit a raw `logseq.*` method. If tools such as
`upsert_nodes`, `insert_block`, `move_block`, `add_page_tag`, or
`get_page_data` appear, an older server is running — stop and say so rather
than adapting.

## Reference files

Read before the matching work, not otherwise:

- `reference/data-modeling.md` — page vs block vs tag vs property, schema
  types, import order. Read before designing a schema or a multi-entity import.
- `reference/write-workflows.md` — exact shapes and verification steps for each
  write. Read before an unfamiliar write.
- `reference/troubleshooting.md` — ambiguous results, timeouts,
  `writes_disabled`, and what a silent no-op looks like. Read when a write
  reports `verified=false`.

## Reporting

Name the entities and the intended change before writing; report the verified
result and any generated ident after.

Do not infer a graph-wide limitation from one tool refusing, and do not claim
capability because the Logseq UI shows a result — verify by reading the
attributes. A tool that is `unknown` in `capabilities` is untested, not broken.

When something cannot be done, say which of these it is: the tool does not
exist, the route does not exist, or Logseq forbids it. They call for different
responses from the user.

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
`getPage` separates these with its `detail` selector for that reason.

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

1. `getPageUUID(title)` → `getPage(uuid, detail)` where detail is
   `page`, `blocks`, `tags`, `properties`, `declared`, or `all`.
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
4. `findOrphans(page_uuid)` reports blocks whose owning page differs from
   their nearest ancestor page — real children that no page-scoped query can
   see. **A nested page is a page boundary, not damage**: blocks beneath a
   sub-page correctly belong to it, and are reported separately under
   `nested_pages`.
5. `pageStats(page_uuid)` returns counts only — own blocks, subtree blocks,
   nested pages, true orphans, refs, tag holders, property values. Prefer it
   for triage: every other read returns payload proportional to page size, so
   auditing many pages with them is expensive and this is not.
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

`dry_run` validates arguments and target existence **locally**. It does not
call the API, so it cannot tell you a write will land. Do not report a
successful dry run as though the change were made.

### Pages

`createPage(title)` rejects a title that already exists rather than creating a
second page, because the read-back could not then tell them apart.

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
> So `importPage` escapes them: `[[X]]` → `{{link:X}}` and `#X` → `{{tag:X}}`.
> The content is inert until repaired. **Tell the user this** — the links they
> wrote will not work until the second step.

`repairLinks()` is that second step. Omit the page UUID to scan the whole
graph, which is usually right: links resolve only once their targets have been
imported, so the natural order is import everything, then repair once.

It is safe to re-run. Names that match no page are skipped and reported with
near-miss suggestions; names matching several pages are skipped rather than
guessed. **Creating the missing pages needs both `create_missing` and
`acknowledge_page_creation`** and is capped — report what would be created and
let the user decide, because a typo and a genuinely new page look identical
from here. Tags are opt-in via `include_tags`.

Page properties (`key:: value` above the first block) are parsed and reported
but not applied: they are outside the writable namespace.

**Batches are not atomic.** An outline several levels deep is several calls, so
a failure partway leaves earlier levels committed. The result names the level
that failed; treat it as a partial write and audit with `findOrphans` rather
than retrying, which would duplicate what already landed.

`moveBlock(block_uuid, target_uuid, placement)` relocates a block and its
subtree — `child`, `before` or `after`. The API returns nothing on a move, so
the tool verifies the new parent, the owning page, and that descendants
followed. A move whose page did not follow leaves a real child that no
page-scoped query can see.

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

A tag must exist before it can be attached. `creatTag(title)` creates one. Its
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

**`listClosedValues` depends on the graph, not the build.** `Status` and
`Priority` carry `:property/closed-values` on a mature graph — six and four
permitted entities respectively — but a freshly created graph has none, so the
tool returns empty there. An empty result means this graph has no enums, not
that the feature is absent. Both are built-ins and outside the sandbox, so
they remain read-only either way.

**A dry run is not a write.** `dry_run` returns `verified: false` by design.
It validates the payload, not the transaction: a graph carrying invalid
entities passes validation and still rejects the real write.

**Deleting a page recycles it.** `deletePage` does not destroy the entity: the
page keeps its UUID, tags, refs and blocks, and stops appearing in
`listPages`. Inbound references are **not** rewritten, so anything linking to
it keeps pointing at a page the user can no longer find. `deletePage` refuses
until `acknowledge_reference_rewrite` is set when references exist — surface
that to the user rather than setting it reflexively.

**`moveBlock` is exposed but its underlying route is unproven.** The API
returns null whether it moved the block or did nothing, and no live run has yet
observed it changing anything. The tool verifies by reading back, so a silent
no-op comes back as `verified: false` with a diagnostic saying so — report that
rather than assuming the move happened.

## Tools

**Reads** — `capabilities`, `getPageUUID`, `getPage`, `pageStats`,
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

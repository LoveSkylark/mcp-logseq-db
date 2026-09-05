---
name: logseq-db-native
description: "Use when reading or modifying a Logseq 2.x DB graph through the mcp-logseq-db server. Covers DB queries, exact identifiers, verified writes, the property namespace sandbox, and recovery from ambiguous results. Never use file-graph or non-DB Logseq tools."
---

# Logseq DB-Native MCP

For `mcp-logseq-db` against a Logseq 2.x **DB** graph. Do not load any other
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

**Never report a write as done on the strength of the response.** Every tool
returns `verified` and `verified_state`. `verified=false` means the write did
not take effect even though no error was raised. Report that as a failure.

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
   `truncated` when a bound stopped it.
3. `getTagUsers(tag_uuid)` and `getProperyUsers(ident)` answer "what uses
   this?" — run either before deleting the thing.
4. The `list*` tools take no arguments and return a whole kind.

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

### Blocks

`createBlock(parent_uuid, title)` — the parent may be a **page** UUID for a
top-level block or a **block** UUID to nest. It will not resolve a title.

Only the title can be set at creation. Tags and position are follow-up calls,
and the new UUID is assigned by Logseq rather than returned.

`createManyBlocks` batches. `createPageofBlocks` builds an indented outline,
alternating create and read-back per level — titles must be unique among
siblings, not across the whole outline.

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
id, not a literal. `Status` and `Priority` are closed enums; call
`listClosedValues` and pass one of those entities.

`deleteProperty` removes the definition graph-wide and takes every value with
it. Recreating mints a new entity — the old values do not return.

### Tags

A tag must exist before it can be attached. `creatTag(title)` creates one; its
ident carries a random suffix, so it cannot be predicted from the title.

`addTag(target, tag)` and `removeTag(target, tag)` take two UUIDs, **target
first**. Removal affects that one relation only.

`deleteTag` is an **unverified route**. It has never been run successfully and
its identifier type is unconfirmed. Check `verified` and do not assume.

## Constraints worth stating to the user

**Recycled pages survive.** Deleting a page adds
`:logseq.property/deleted-at` and keeps the UUID, tags, refs, and blocks.
`listPages` excludes them; `listRecycled` shows them. Inbound references are
not rewritten.

**Ordering is fractional.** `:block/order` is a string (`a0`, `a1`, `a0V`) that
sorts lexicographically. Sort by it — pull does not guarantee order. There is
no reindex operation and none is needed.

**No tool creates, renames, or deletes a page.** Those routes exist and are not
exposed. Say so rather than improvising.

**Moving a block has no route at all.** Not unavailable in this server —
unavailable, full stop.

## Tools

**Reads** — `capabilities`, `getPageUUID`, `getPage`, `getBlockUUID`,
`getBlock`, `getBlockTree`, `getTagUUID`, `getTag`, `getTagUsers`,
`getPropertyIndent`, `getProperyUsers`

**Lists** (no arguments) — `listPages`, `listJournals`, `listTags`,
`listProperties`, `listClosedValues`, `listOrphanTags`,
`listOrphanProperties`, `listAssets`, `listStatus`, `listRecycled`

**Writes** — `createBlock`, `createManyBlocks`, `createPageofBlocks`,
`updateBlock`, `removeBlock`, `creatTag`, `deleteTag`, `addTag`, `removeTag`,
`createProperty`, `deleteProperty`, `addProperty`, `removeProperty`

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

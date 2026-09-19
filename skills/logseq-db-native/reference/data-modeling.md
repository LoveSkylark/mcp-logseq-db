# Data modeling

How to shape information for a Logseq DB graph. Read before designing a schema
or planning a multi-entity import; skip it for a single scoped read or write.

## Choosing a shape

| Use | When |
| --- | --- |
| **Page** | The thing has durable identity and will be referenced, linked, or opened on its own. |
| **Block** | Narrative or nested content that belongs to something else. |
| **Tag** | A shared type. Tags are classes: they declare property slots that every tagged entity inherits. |
| **Property** | A value you will filter, sort, or query on. |

The distinction that matters most: **a tag is a schema, a property is a
field.** Tagging a page `Task` gives it Status, Priority, Deadline and
Scheduled as available slots. Setting Status gives it a value. Reach for a tag
when several entities should share a shape; a property when you need one
queryable value.

If you find yourself creating a property whose only values are a small fixed
set of names, consider a tag instead — the entities can then carry their own
properties.

## What this server can actually build

Before designing anything, know the shape of the hole:

**Pages: create, rename, delete, clear.** All four exist. Deleting recycles
rather than destroys, and does not rewrite inbound references, so a model that
churns pages leaves dangling links behind.

Titles are shared across pages, tags and blocks, so a page title can collide
with a tag of the same name. The tools refuse such collisions, but the raw API
permits them — `createTag` will happily take a title an existing page holds.
An older graph may therefore already contain pairs the tools would now reject.

**Blocks: title only at creation.** Tags, properties, and position are all
follow-up calls. A block with a tag and two properties is four calls, not one.

**Properties: your namespace only.** You can create and set
`plugin.property.<caller>/*`, where `<caller>` is assigned by Logseq from the
API server's identity rather than chosen — on the graph this was developed
against it is `_test_plugin`. Properties the user made in the UI live under
`user.property/*` and are readable but not writable, as are the
`:logseq.property/*` built-ins.

Whether any namespace is shared between callers is **untested**, and it
matters: if there is none, two integrations cannot see each other's properties,
and a property the user can edit in the UI can never be written by an API
client. `scripts/live_reliability.py --explore` reports the caller id and the
namespaces actually present in the graph.

**Tags: creation works; the delete ROUTE has never succeeded.** Every write in
this server reads back and reports `verified`, `deleteTag` included — the
uncertainty is not in the checking but in whether `deletePage` (the route
underneath it) does anything when handed a tag. Until a live run says
otherwise, treat tag deletion as likely to come back `verified: false`, and do
not design something that depends on cleaning up tags.

**Blocks can be moved.** `moveBlock(block_uuid, target_uuid, placement)`
relocates a block and its subtree — `child`, `before` or `after` — including
across pages, and is confirmed on all placements. An earlier version of this
file said there was no route for it and told you to get structure right at
creation; that is no longer true, so a model that needs to reorganise later is
viable. Three behaviours still shape how you use it: it no-ops when the
position would not change, `child` PREPENDS while `last-child` appends (use
`last-child` when moving a sequence, or the destination ends up reversed), and
the subtree's `:block/page` follows the move.

That last point is the one that shapes import order least now that blocks can
be moved — but building outward from identity is still cheaper than fixing
structure afterwards.

## Import order

Definitions have to exist before values can point at them, so build outward
from identity:

1. **Properties and tags first.** Definitions must exist before values can be
   assigned. Retain every returned ident.
2. **Pages next.** `createPage` returns the entity; keep the UUID. Titles must
   be unique, so plan for collisions with existing pages *and* with tags,
   which are pages too.
3. **Structure before content.** Use `createPageofBlocks` for anything nested,
   or `importPage` when you have the whole page as markdown — both thread the
   UUIDs each level returns into the next, which is the part a caller-side loop
   gets wrong silently.
4. **Tags and properties last**, once targets have UUIDs.

Batch within a level rather than across levels. `insertBatchBlock` — the route
under `createPageofBlocks` and `importPage` — takes a whole level of siblings
in one call; going block by block multiplies round trips for no benefit.
(`createManyBlocks`, which this file used to name here, was removed: it batched
across arbitrary parents, so a failure could commit part of the tree.)

## Property schema

```
type:        default | number | string | datetime | checkbox
             | url | node | page | class | property | map
cardinality: one (replaces) | many (adds to a set)
```

`default` is plain text — not `text`. `string` exists but appears only on
built-ins.

**Reference types store entities, not literals.** `node`, `page`, `class`, and
`property` values are entity ids. A `node` property is the right choice for a
real relationship between entities; a `default` property holding a name is not,
because nothing links.

**Closed values** turn a property into an enum. `Status` and `Priority` work
this way. A write must pass one of the permitted value entities, so call
`listClosedValues` first — and use that tool rather than querying
`:property/closed-values`, which `getAllProperties` reports but which exists as
no datom on any graph.

Cardinality is worth deciding deliberately. `one` overwrites silently on the
next write; `many` accumulates and needs explicit removal.

## Patterns

**A typed collection.** Create a tag, attach properties to it, tag each member.
Members inherit the slots and can be queried by tag. This is the closest thing
the DB has to a table.

**A cross-reference.** A `node`-typed property pointing at another entity
creates a real reference and appears in backlinks. Prefer it to writing a page
name into text.

**A status workflow.** Use the built-in `:logseq.property/status` if the
built-in states fit — it renders natively. You cannot write it (built-ins are
outside the sandbox), so a workflow you need to drive from the API needs your
own property with your own closed values.

**Narrative with structure.** Blocks for prose, properties on the page for the
queryable facts. Avoid encoding data into block text that you will later want
to filter on.

## Anti-patterns

**Markdown property syntax.** `key:: value`, YAML frontmatter, and file
manipulation are file-graph concepts. In a DB graph they are just text.

**Titles as identifiers.** Page, block, tag, and property titles are all
non-unique. Resolve to a UUID or ident and keep it.

**`:db/id` in stored data.** Those integers are renumbered when a graph is
rebuilt. Fine inside one query, never persisted.

**Deep nesting for its own sake.** Each level of an outline costs one call per
parent that has children. Depth costs calls; width is free.

**Duplicate sibling titles.** Fine — `insertBatchBlock` returns the entities it
created, so nothing has to identify a new block by its title. Two sections can
each hold a child called `Notes`. (This was an anti-pattern when creation
returned nothing and verification had to search siblings by title.)

## Before you build

Read the current shape rather than assuming it. `listTags`, `listProperties`,
and `listClosedValues` take no arguments and show what already exists — reusing
an existing property is almost always better than creating a near-duplicate in
a namespace the user cannot edit from the UI.

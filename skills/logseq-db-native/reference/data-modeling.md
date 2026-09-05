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

**No page creation tool.** The route exists and is not exposed. An import that
needs new pages cannot be done here — say so before planning it.

**Blocks: title only at creation.** Tags, properties, and position are all
follow-up calls. A block with a tag and two properties is four calls, not one.

**Properties: your namespace only.** You can create and set
`plugin.property.<caller>/*`. Properties the user made in the UI live under
`user.property/*` and are readable but not writable. If a model depends on
writing an existing user property, it will not work.

**Tags: creatable, deletion unverified.** `creatTag` works; `deleteTag` has
never been confirmed. Do not design something that depends on cleaning up tags.

**No block movement.** Structure has to be right at creation. There is no
reordering, and no route for one.

That last point shapes import order more than anything else.

## Import order

Because nothing can be moved afterwards, build outward from identity:

1. **Properties and tags first.** Definitions must exist before values can be
   assigned. Retain every returned ident.
2. **Pages next** — except this server cannot create them, so they must already
   exist. Resolve each to a UUID and keep it.
3. **Structure before content.** Use `createPageofBlocks` for anything nested;
   it handles the create/read-back/create cycle that block creation requires.
4. **Tags and properties last**, once targets have UUIDs.

Batch within a level rather than across levels. `createManyBlocks` is one call
for a whole level; going block by block multiplies round trips for no benefit.

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
this way. A write must pass one of the entities in `:property/closed-values`,
so call `listClosedValues` first.

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

**Deep nesting for its own sake.** Each level of an outline costs a create plus
a read-back. Depth is 2d−1 calls; width is free.

**Duplicate sibling titles.** Two blocks with the same title under the same
parent cannot be told apart by read-back, so a write cannot be verified. Under
*different* parents they are fine.

## Before you build

Read the current shape rather than assuming it. `listTags`, `listProperties`,
and `listClosedValues` take no arguments and show what already exists — reusing
an existing property is almost always better than creating a near-duplicate in
a namespace the user cannot edit from the UI.

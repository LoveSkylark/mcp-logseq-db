# Built-in classes and properties

Read from a live graph on 2026-09-20, Logseq `2.0.1-alpha+nightly.20260826`,
via `listTags`, `listProperties` and `listClosedValues`. Entity ids are from
that graph and are NOT stable across a rebuild — the idents are.

Tagging is not decoration. A built-in class carries behaviour: it declares
properties, and in some cases changes how the node renders. `#Code` turns a
block into a code block because the class does that, not because of anything
in the text.

## How a tag gives a node properties

A class holds its property list in `:logseq.property.class/properties`
(display title **Tag Properties**, cardinality many). Tag a node with the
class and those properties become DECLARED on it.

Declared is not set. A declared property has **no datom** until it holds a
value, so it appears in no block read and no `datascriptQuery` over the node's
attributes. `inspectPage detail=declared` exists for exactly this: it is the
only way to see them. That is also why a property you expect to be "on" a page
can be invisible everywhere else.

`:logseq.property.class/extends` (**Extends**) is class inheritance; every
built-in below extends class id 1. So a node also gets whatever its class's
ancestors declare.

## Built-in classes

| Tag you type | Ident | Declares | Notes |
| --- | --- | --- | --- |
| `#Task` | `:logseq.class/Task` | Status, Priority, Deadline, Scheduled | The to-do machinery. All four are properties, not text markers. |
| `#Code` | `:logseq.class/Code-block` | display-type, language | Sets `:logseq.property.node/display-type: "code"` on the node when applied — observed directly. `hide-from-node`. |
| `#Quote` | `:logseq.class/Quote-block` | display-type | `hide-from-node` |
| `#Math` | `:logseq.class/Math-block` | display-type | `hide-from-node` |
| `#Query` | `:logseq.class/Query` | Query | The query string lives in `:logseq.property/query`, a hidden property |
| `#Cards` | `:logseq.class/Cards` | — | Extends `Query` rather than the root |
| `#Card` | `:logseq.class/Card` | two card-scheduling properties | Spaced repetition |
| `#Comments` | `:logseq.class/Comments` | one | `hide-from-node` |
| `#Comment` | `:logseq.class/Comment` | — | `hide-from-node` |
| `#Template` | `:logseq.class/Template` | Apply template to tags | See `repair.md` and the caveat below |

`hide-from-node: true` means the class is not offered in the node's own tag
picker — it is applied by a command or by conversion, not usually chosen by
hand. It does not stop `addTag` from applying it.

Four declared property ids (34, 36, 68, 143, 144) did not come back from
`listProperties` at all, so their idents are unconfirmed. `display-type` is
named above only because a tagged block was observed gaining
`:logseq.property.node/display-type`. **`listProperties` does not return every
property** — that is worth knowing before treating its output as an inventory.

## The properties worth knowing

| Title | Ident | Type | Notes |
| --- | --- | --- | --- |
| Status | `:logseq.property/status` | closed enum, 6 values | Default value is pre-set. Renders at `blockLeft`. |
| Priority | `:logseq.property/priority` | closed enum, 4 values | Renders at `blockLeft` |
| Deadline | `:logseq.property/deadline` | datetime | Renders `blockBelow` |
| Scheduled | `:logseq.property/scheduled` | datetime | Renders `blockBelow` |
| Description | `:logseq.property/description` | default | Its value is a BLOCK, not a string |
| Assignee | `:logseq.property/assignee` | node, many | |
| Tags | `tags` | class, many | Bare ident, no namespace — the datom is `:block/tags` |
| Alias | `alias` | page, many | Bare ident; the datom is `:block/alias` |
| Tag Properties | `:logseq.property.class/properties` | property, many | What a class declares |
| Extends | `:logseq.property.class/extends` | class, many | Class inheritance |
| Apply template to tags | `:logseq.property/template-applied-to` | class, many | On the template, naming its target tag |

**Closed enums.** `Status` and `Priority` take one of a fixed set of VALUE
ENTITIES, not a string. Call `listClosedValues` and pass the entity id. Status
has six (Backlog, Todo, Doing, In Review, Done, Canceled) and Priority four
(Low, Medium, High, Urgent). Note that `getAllProperties` reports the set as
`:property/closed-values`, but no such datom exists — the real attribute is
`:block/closed-value-property`, on each value pointing back at its property.

**`Description` holds a block.** Its value is an entity id referring to a
block, so reading it gives you a reference and needs a second read to get text.

## None of this is writable

All of the above are `:logseq.property/*` or bare built-in idents, and the
plugin sandbox permits writes only to `plugin.property.<caller>/*`.
`upsertProperty` refuses anything else outright: *"Plugins can only upsert its
own properties"*. So:

- **You cannot set Status, Priority, Deadline, Scheduled, Description or
  Assignee** through these tools. `addProperty` will refuse.
- **You cannot declare properties on a tag**, since that means writing
  `:logseq.property.class/properties`. The routes `addTagProperty` and
  `removeTagProperty` exist in the API and have no tool; whether they accept a
  built-in ident is untested.
- **You CAN tag.** `addTag` works, and a class declaring properties is
  Logseq's write rather than ours.

So the division of labour: define the class and its properties ONCE in the
Logseq UI, then `addTag` from the API. The page gets the declared properties
without anything in the sandbox being touched. Confirm with
`inspectPage detail=declared`, since declared properties are invisible
elsewhere.

## Templates: they DO apply, including through the API

Measured 2026-09-20, after an earlier version of this file said the opposite.
The correction note below is worth reading — the mistake is instructive.

The mechanism: a template is a block tagged `#Template` carrying
`:logseq.property/template-applied-to` pointing at a tag. Tag any node with
that tag and the template's CHILD BLOCKS are inserted into it. `addTag` from
this API is enough to trigger it — no template-specific route is needed, which
is just as well, since none is reachable.

**The stamp goes on the INSERTED blocks, not on the tagged node.** Each block
a template produced carries `:logseq.property/used-template` naming the
template it came from. So to check whether a template fired, look at the
children rather than at the thing you tagged:

```
getProperyUsers(":logseq.property/used-template")
```

That query is the practical way to see it. `getBlockTree`'s pull pattern does
not name the attribute, so a tree read of a node that received a template
shows the inserted blocks with no indication of where they came from.

**Only CONTENT travels. Property values do not.** A template block may itself
carry `Status`, `Assignee`, `Description` and so on; those stay on the
template. In the DB model the tag owns the properties (through
`:logseq.property.class/properties`) and the template owns the child blocks.
So a template is not a way to copy properties onto a new page, and tagging is
not a way to copy a template's own property values. To get both, the tag must
declare the properties AND a template must point at that tag.

A consequence worth expecting: if a template's body is one empty block,
tagging inserts one empty block and nothing appears to have happened. That is
the template working.

### Why this file said the opposite

An experiment tagged a fresh block with `#Code`, where a template pointed at
the Code class. The read-back showed an empty child and no `used-template`,
and that was written up here as "templates do not apply on this build".

Both halves of the reasoning were wrong. The stamp was looked for on the
TAGGED block when it lands on the INSERTED ones — and the pull pattern used
would not have returned the attribute from either. The empty child WAS the
template firing, and it carried the stamp the whole time.

The lesson is the one this server is built on, pointed the other way: an
absent attribute in a read is not evidence of absence unless the read asked
for it. A pull pattern returns only what it names.

The template APIs are unreachable regardless: `logseq.DB.createTemplate` does
not exist, and the `logseq.App.*` and `logseq.Editor.*` namespaces are not
dispatched over HTTP. See `../../../doc/to-do-fixes.txt`.

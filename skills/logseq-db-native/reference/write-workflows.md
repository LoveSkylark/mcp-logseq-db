# Write workflows

Exact shapes and verification steps. Read before an unfamiliar write.

Every write returns `verified`, `verified_state`, and on failure
`previous_state` and `observed_state`. **`verified=false` is a failure**, even
though no error was raised — this API reports success for calls that do
nothing, so the read-back is the only evidence.

## verbose

Every write tool takes `verbose`, default `true`. The envelope carries the
target entity twice, before and after, so a write to a long block returns that
block's text twice over.

`verbose: false` returns identity and position only — `verified`, `uuid`,
`parent`, `page`, `order`, `diagnostic`, plus `ident` where one was assigned.
The write is still read back and still compared; only the payload is dropped.

Use it for `moveBlock`, `addTag`, `removeTag`, `addProperty`,
`removeProperty`, `createBlock` and `createPageofBlocks`. Keep the default for
`clearPage`, `removeBlock` and `deletePage`, where the payload is the only
record of what was destroyed, and for `updateBlock`, where the before/after
pair is how you see what Logseq did to the content you sent.

---

## Pages

### Creating

```
createPage(title)
```

A title already held by any page, tag or block is rejected — the three share
one title space, and the check counts RECYCLED pages too, since recycling
keeps the entity. Call `isTitleAvailable(title)` first if the title might be
taken: it runs this same check and names the holder, including whether it is
recycled. `getPageUUID` returning `found: false` is not evidence the title is
free — it is recycle-blind by design.

Routed through `logseq.DB.createPage`, not `upsertNodes`. Two reasons:
`upsertNodes` fails outright on synced graphs, returning "The Imported EDN has
N validation error(s)" for a write its own dry run accepts; and `createPage`
is idempotent on title, so a repeat returns the existing page rather than a
duplicate.

Its second argument is a PROPERTIES map, not options. Passing
`{"dry-run": true}` creates the page anyway and mints a `dry-run` property in
your namespace — so there is no server-side dry run here, and `dry_run`
validates locally only.

### Renaming

```
renamePage(page_uuid, new_title)
retitleOverDuplicate(from_uuid, to_title, park_suffix="(parked)")
```

Verified by UUID. Reading back by the new title would not distinguish a rename
from Logseq creating a second page and leaving the original untouched, so the
original UUID is re-read and its title compared. The check also confirms
`:block/name` survived — a rename that stripped page identity would otherwise
look like success.

A title another entity already holds is rejected, for the same reason as
`createPage` — and on the same recycle-aware check, so a title whose only
holder is a recycled page is refused. `isTitleAvailable(new_title)` reports
that before the attempt.

`retitleOverDuplicate` is the way THROUGH that rejection when the holder is an
empty duplicate. It renames the holder to `"<to_title> (parked)"`, then renames
`from_uuid` onto the freed title — two renames, no block edits, and every
inbound reference on both pages intact, since references are by UUID. It works
on a recycled holder too: renaming a recycled page is what releases its title.

It refuses, returning both pages' inbound reference counts rather than raising,
when the holder has content blocks, when the holder is in an alias relation, or
when the title has more than one holder or a non-page holder.

Direction is yours: `from_uuid` keeps its identity and gains the title. Choose
the side the graph is wired into, not the correctly spelled one, and use the
reported counts to check.

Not atomic. If the second rename fails, the result carries the parked page's
UUID and original title with instructions to undo.

### Deleting

```
deletePage(page_uuid, acknowledge_reference_rewrite=false)
```

**This recycles rather than destroys.** The page keeps its UUID, tags, refs and
blocks, gains `:logseq.property/deleted-at`, and drops out of `listPages`. It
remains visible through `listRecycled`.

**Inbound references are not rewritten.** Any block linking to the page keeps
pointing at it. The tool lists the referring entities and refuses until
`acknowledge_reference_rewrite=true`, so the user can decide — do not set the
flag without telling them what it means.

`deletePage` accepts the page UUID — confirmed, and the envelope reports which
form worked. The same route underlies `deleteTag`.

### Clearing

```
clearPage(page_uuid)
```

Deletes every block, keeps the page. One call per top-level block since there
is no batch delete, each taking its subtree with it — so cost scales with the
number of top-level blocks, not total blocks.

The page entity, its tags and its property values are untouched. Use this
rather than `deletePage` followed by `createPage`: the latter changes the UUID
and breaks every reference.

---

## Blocks

### Creating

```
createBlock(parent_uuid, title)
```

`parent_uuid` may be a **page** UUID (top-level block) or a **block** UUID
(nested child). It will not resolve a page title.

Only `title` can be set; tags and position are follow-up calls.

Verification checks **both** `:block/parent` and `:block/page`. Those are
separate facts — parent is the tree link, page is ownership at any depth — and
a block can have the first right and the second wrong. Such a block is a real
child that no page-scoped query can see, which is why checking the parent alone
is not enough.

### Moving

```
moveBlock(block_uuid, target_uuid, placement="child")
```

`child` and `last-child` put the block under the target; a page target moves
it to that page's top level. `before` and `after` place it as a sibling, so
they need a block target — a page has no siblings.

**`child` PREPENDS. `last-child` APPENDS.** Moving three blocks in source
order with `child` puts each new arrival in front of the last, so the
destination ends up reversed — silently, and only visible by reading the
destination back. Use `last-child` whenever the order of what you are moving
matters, which is almost always. `child` keeps prepending because that is
Logseq's own behaviour and callers depend on it.

`last-child` costs one extra read: it looks up the target's children to find
what to move after, because nothing in this API can write `:block/order`. In
exchange it verifies the block ended up *last*, so a `verified: true` from
`last-child` is a stronger claim than one from `child`.

The API returns nothing, so three things are verified by reading back: the new
parent, the owning page, and that descendants followed. Each is a distinct
failure. A block whose parent did not change is a silent no-op. A block whose
owning page did not follow is a real child of the target that no page-scoped
query can see. Descendants left pointing at the old page are the same failure
one level down.

Moving a block to the position it already occupies is a no-op reported as
`verified: false`. The exception is `last-child` on a block already last:
nothing is written and the result is `verified: true`, with a diagnostic
saying no move was needed — the requested state was read and found to hold.

Two moves are refused before the call: a target inside the block's own subtree,
which would detach it from the graph, and sibling placement against a page.

`dry_run` on any of these validates locally and does not call the API. It
confirms the arguments are well formed and the targets exist; it cannot
confirm the write will succeed.

### Outlines

```
createPageofBlocks(page_uuid, outline)
```

Indented text in, tree out. Costs one call per parent that has children.

Creation returns the entities it made, so a parent's UUID is known before its
own children are inserted — there is no read-back cycle. Duplicate titles among
siblings are fine for the same reason: nothing has to identify a new block by
its title.

Indent width is taken from the first indented line, so 2-space and 4-space both
work if consistent. Skipping a level raises.

There is no transaction. A failure at level three leaves levels one and two in
place; the error names which level stopped.

### Editing and deleting

```
updateBlock(block_uuid, title)
removeBlock(block_uuid)
```

`removeBlock` takes the whole subtree and verifies every descendant is absent,
not just the root. Subtrees over 1000 nodes are refused rather than partially
deleted — it will not delete what it cannot verify.

---

## Properties

### Definition versus value

Four tools, two different entities:

| | Definition | Value on a target |
| --- | --- | --- |
| create | `createProperty(title, schema)` | `addProperty(uuid, ident, value)` |
| remove | `deleteProperty(ident)` | `removeProperty(uuid, ident)` |
| keyed by | `:db/ident` | target UUID + ident |

Choosing wrong yields a silent no-op, so confirm which you mean before calling.

### Creating a definition

```
createProperty("Effort", {"type": "number"})
```

A **plain title**, never a namespaced ident — Logseq treats the first argument
as a page name and rejects the `/`.

Types: `default` (text), `number`, `string`, `datetime`, `checkbox`, `url`,
`node`, `page`, `class`, `property`, `map`.

The namespace comes from caller identity and cannot be chosen. An explicit
ident in the schema is accepted and silently discarded. **Retain the ident
returned in `verified_state`** — for plugin properties it is predictable
(`:plugin.property.<caller>/<Title>`, no suffix), but read it rather than
constructing it.

### Setting a value

```
addProperty(target_uuid, ":plugin.property._test_plugin/Effort", 5)
```

Target may be a page or a block.

**Reference types take an entity, not a literal.** `node`, `page`, `class`, and
`property` values are entity ids. So are closed enums: call `listClosedValues`
and pass one of the listed entities. `Status` renders as "Doing" but is stored
as a reference.

Cardinality matters. `many` adds to a set; `one` replaces.

### The namespace sandbox

Writes reach only `plugin.property.<caller>/*`. Everything else is read-only:

| Namespace | Source | Writable |
| --- | --- | --- |
| `plugin.property.<caller>/*` | this API | yes |
| `user.property/*` | the Logseq UI | no |
| `:logseq.property/*` | built-in | no |

The server rejects out-of-namespace idents **before** the call, so the failure
is a clear error rather than a silent no-op. This is Logseq's restriction, not
the server's — there is no workaround, and the user should be told plainly
rather than watching attempts fail.

### Deleting a definition

```
deleteProperty(":plugin.property._test_plugin/Effort")
```

Graph-wide, taking every value with it, and **not reversible** — recreating
mints a new entity and the old values do not return.

Run `getProperyUsers(ident)` first. An empty result makes this safe; anything
else is data you are about to destroy.

This route is **unverified**. The UUID form is confirmed to do nothing; whether
the ident form works has not been established. Check `verified` — `false` here
means the definition is still in place, not that the values are half gone.

---

## Tags

### Creating

```
creatTag(title)
```

The ident is assigned by Logseq rather than derived from the title — ones made
in the UI carry a random suffix (`:user.class/xzy-bc0auNqC`) — so take it from
`verified_state` rather than constructing it.

### Attaching and detaching

```
addTag(target_uuid, tag_uuid)
removeTag(target_uuid, tag_uuid)
```

**Target first.** Both arguments are UUIDs; the tag's ident will not work.
Target may be a page or a block.

`removeTag` removes one relation. Other tags survive and so does the tag
entity.

There is no `upsertNodes` route for removal: `operation` offers only `add` and
`edit`, with no retraction verb. Expressing removal as an upsert would mean
overwriting the entire tag set — and a page that loses `:logseq.class/Page`
stops being a page. The dedicated route cannot make that mistake; the server
also checks for it after every tag change.

### Deleting

```
deleteTag(tag_uuid)
```

**Unverified route.** It goes through `deletePage`, which has never been run
against a tag, and whose identifier type is unconfirmed. Check `verified`.

Run `getTagUsers(uuid)` first. Deleting a tag with child tags requires
`acknowledge_child_reparent=true`.

---

## When a write reports verified=false

Read `previous_state` and `observed_state`. Identical means nothing happened —
almost always an identifier of the wrong type. Different means something
happened, but not what was asked, which is more serious.

Do not retry. A repeat with the same arguments produces the same silent
no-op, and if the first call *did* land, a second may duplicate it.

Resolve the identifier and re-read the target before trying anything else. See
`troubleshooting.md` for ambiguous timeouts and `writes_disabled`.

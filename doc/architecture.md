# Logseq DB MCP — Architecture

Replaces the earlier `upsertNodes`-first design. That document assumed
`upsertNodes` was a general mutation primitive with dedicated APIs as
fallbacks. Live testing showed it is a narrow special case, and that the real
hazard is something the old design never accounted for.

---

## 1. The governing fact

**This API returns success for calls that do nothing.**

A wrong identifier type, an unresolvable name, or an unsupported combination
produces `null` or `{:block 1}` — the same responses a successful call
produces. Nothing distinguishes them at the transport layer.

Observed cases:

| Call | Response | What happened |
|---|---|---|
| `upsertNodes` with `page-id` set to a page *name* | `{:block 1}` | nothing created |
| `removeProperty` with a UUID | `null` | nothing removed |
| `upsertProperty` with an explicit `ident` | full entity | ident silently discarded |
| `removeBlock` with a UUID | `null` | block actually deleted |

The last row is the important one: `null` means both "worked" and "did
nothing". **A response is not evidence.**

Everything below follows from this.

---

## 2. Core principle

> **Every write is a write plus a read-back. An operation that cannot be
> verified is not implemented.**

Not "should verify". The verification *is* the operation. A tool that writes
and returns without reading back is a tool that reports success it has not
observed.

```
tool call
    ↓
resolve identifiers to canonical form   ← fail loudly here, not later
    ↓
snapshot the affected state
    ↓
write
    ↓
read back and compare
    ↓
VERIFIED → return          UNVERIFIED → raise, with both states attached
```

The snapshot is what makes the comparison meaningful. "The tag is present
afterwards" proves nothing if it was present before.

---

## 3. Routes are a lookup table, not a hierarchy

The old design ranked routes: try `upsertNodes`, fall back to a dedicated
API, fall back to the CLI. That framing was wrong. In practice **each tool has
exactly one route that works**, and which one is not predictable from the
tool's shape.

`upsertNodes` accepts precisely three combinations:

```
add  + page
add  + block
edit + block
```

`edit + page` returns "Editing a page, tag or property isn't supported yet".
`operation` has no retraction verb — only `add` and `edit` — so no removal of
anything can be expressed through it.

So there is no ladder to descend. There is a table, and every row is an
empirical finding rather than a preference.

**Status** — `verified`: run against a live graph, effect confirmed by
read-back. `probable`: the route is right but this exact call was not run.
`untested`: no evidence.

### Tags

| Tool | Route | Status |
|---|---|---|
| `getTagUUID` | `getTagsByName` | probable |
| `getTag` | `datascriptQuery` | verified |
| `getTagUsers` | `datascriptQuery` | verified |
| `creatTag` | `createTag` | probable — creation observed by read-back; the assigned ident shape is not pinned |
| `deleteTag` | `deletePage` | untested — identifier type unknown |
| `addTag` | `addBlockTag` | probable |
| `removeTag` | `removeBlockTag` | **verified** |

### Properties

| Tool | Route | Status |
|---|---|---|
| `getPropertyIndent` | `datascriptQuery` | verified |
| `getProperyUsers` | `datascriptQuery` | verified |
| `createProperty` | `upsertProperty` | **verified** |
| `deleteProperty` | `removeProperty` | untested — UUID form confirmed to do nothing |
| `addProperty` | `upsertBlockProperty` | probable — blocked for `user.property/*` |
| `removeProperty` | `removeBlockProperty` | untested |

### Blocks

| Tool | Route | Status |
|---|---|---|
| `getBlockUUID` | `datascriptQuery` | verified |
| `getBlock` | `getBlock` | **verified** |
| `getBlockTree` | `datascriptQuery` | verified |
| `createBlock` | `insertBlock` | **verified**, nested included |
| `createPageofBlocks` | `insertBatchBlock`, one call per parent | **verified** |
| `updateBlock` | `updateBlock` | **verified** |
| `moveBlock` | `moveBlock` | **verified**, all placements, across pages |
| `removeBlock` | `removeBlock` | **verified** |

### Pages

| Tool | Route | Status |
|---|---|---|
| `getPageUUID` | `getPage`, then `datascriptQuery` | verified |
| `inspectPage` | `datascriptQuery` (per detail selector) | verified |
| `pageStats` | `datascriptQuery` | verified |
| `findBacklinks` | `datascriptQuery` | verified |
| `findOrphans` | `datascriptQuery` | verified |
| `createPage` | `createPage` | **verified** |
| `renamePage` | `renamePage` | **verified** |
| `deletePage` | `deletePage` | **verified** — recycles; UUID tried first, name second |
| `clearPage` | `removeBlock`, looped | **verified** |
| `importPage` | `insertBatchBlock` + `datascriptQuery` | **verified** |
| `repairLinks` | `updateBlock` + `datascriptQuery` | **verified** |

### Lists

| Tool | Route | Status |
|---|---|---|
| `listTags` | `getAllTags` | verified |
| `listProperties` | `getAllProperties` | **verified** |
| all others | `datascriptQuery` | verified except `listAssets` |

`listAssets` is untested — asset modelling was never established, and its
current query is a discovery probe rather than a working list.

### Notes on the table

**Reads are not uniformly `datascriptQuery`.** Four tools use dedicated
methods. The rest use queries because the dedicated equivalents either do not
exist or return everything unfiltered.

**`upsertNodes` is no longer a route at all.** It fails on SYNCED graphs —
"The Imported EDN has N validation error(s)" for a write its own dry run
accepts — while `createPage`, `insertBlock`, `insertBatchBlock`, `updateBlock`
and `createTag` all succeed against the same graph. Block creation had already
moved off it for a second reason: it writes its single `page-id` into both
`:block/parent` and `:block/page`, so a block parent produced a child whose
owning page was the parent block — a real child that no page-scoped query could
see. It is out of the client allowlist, which is what keeps it from creeping
back. The three-combination limit described above is therefore history, kept
because it explains why the old design's fallback ordering described a
structure that did not exist.

**`getBlock`, `updateBlock` and `removeBlock` were all listed as rejected** by
the previous capability implementation. All three work. See §8.

### Routes with no tool

Working routes that nothing currently exposes:

| Route | Would support |
|---|---|
| `addTagExtends` / `removeTagExtends` | a tag's parent — tag inheritance |
| `addTagProperty` / `removeTagProperty` | the property slots a tag declares |
| `setBlockIcon` / `removeBlockIcon` | an icon on a block or page |

Tag inheritance and tag-level property declaration are the notable gaps: both
have working routes, and a caller can read a tag's parent and its declared
slots but cannot write either.

Everything else that was listed here — creating, renaming, deleting and
clearing a page — now has a tool. **Moving a block, which this section once
recorded as having "no identified route at all", goes through `moveBlock` and
is verified on every placement including across pages.** `insertBatchBlock`,
also listed as untested, now backs both `importPage` and `createPageofBlocks`.
That pair is the reason §8 exists: both were written off on the strength of a
capability list that was wrong.

## 4. Identifier discipline

Each entity kind has one canonical key. Passing the wrong one fails silently.

| Kind | Key | Notes |
|---|---|---|
| block | `:block/uuid` | |
| page | `:block/uuid` | a page **is** a block; same methods apply |
| tag | UUID for relations, `:db/ident` for lookups | the ident is assigned by Logseq, not derived from the title; read it back |
| property | `:db/ident` | UUID fails silently |
| `:db/id` | queries only | integers are not stable across rebuilds; never persist |

**Names are never identifiers.** No argument resolves page names —
`upsertNodes`'s `page-id` notably looked as if it might. `removeProperty` does
not accept a title. Name lookup is a separate, explicit resolution step that
must return exactly one match or fail.

The MCP surface accepts UUIDs uniformly and resolves internally to whatever
each route requires. Callers should never need to know that properties are
keyed differently from blocks.

### Validation at the boundary

Datascript queries are built by string interpolation, and the query travels as
a string inside the JSON envelope — `json.dumps` escapes the envelope but
cannot stop a value from breaking out of a query literal inside it. So each
kind of value is handled according to whether escaping is even available:

- **UUIDs** must match the canonical 8-4-4-4-12 form. Cosmetic variations
  (braces, uppercase, a `urn:uuid:` prefix, missing separators) are normalised
  rather than rejected; a value of the wrong KIND is rejected with a diagnosis
  naming what it looks like instead.
- **Idents** must match keyword shape, and this is the strict one. An ident
  goes into an ATTRIBUTE position — `[?holder :plugin.property.x/Effort
  ?value]` — which takes no `:in` binding and is not a string literal, so
  there is nothing to escape it with. Whitespace, quotes, brackets and
  backslashes are refused outright, including on idents that came back from
  Logseq itself.
- **String literals** — titles, mostly — are emitted with `json.dumps`, whose
  output is a valid EDN string literal, so a title containing a quote or a
  backslash is escaped correctly rather than refused. An earlier version of
  this document claimed such titles were rejected; they were not, and refusing
  them would have been wrong anyway. A page can legitimately be called `The
  "Good" Place`.

This is not a security boundary — the caller already holds the token. It
converts silent nulls into loud errors, which given §1 is the point.

---

## 5. The parent argument takes either kind

The single most useful discovery, and it is invisible from the argument name.

```json
{"method": "logseq.DB.insertBlock",
 "args": ["<page uuid>",  "...", {"sibling": false}]}   → top-level block
{"method": "logseq.DB.insertBlock",
 "args": ["<block uuid>", "...", {"sibling": false}]}   → nested child
```

One argument, both behaviours, so nested block creation needs no separate
route and no CLI. It was first found on `upsertNodes`, whose `page-id` field
behaved the same way — and that field is also where the discovery turned into
a bug, because `upsertNodes` wrote its single `page-id` into both
`:block/parent` and `:block/page`. `insertBlock` sets the two independently,
which is the other reason block creation moved to it.

Only the title can be set at creation. There is no way to set tags, order or
position on the way in — each is a follow-up call.

The general lesson: where a method or argument names one entity type, try the
other before believing the restriction. `removeBlockTag` works on pages for
the same reason, and so does `deletePage` on a tag — though that one is still
unverified.

---

## 6. Reads

All reads go through `datascriptQuery`. `getAllTags` and `getAllProperties`
exist and work, but return everything unfiltered; the query form is preferred
because it selects fields and filters by class.

Class markers, used constantly:

```
:db/id 2  → :logseq.class/Tag
:db/id 3  → :logseq.class/Property
:db/id 4  → :logseq.class/Page
```

Three read patterns worth naming explicitly, because they are not
interchangeable and confusing them produces wrong answers:

- **`:block/parent`** — direct children only, one level
- **`:block/page`** — every block on a page at any depth
- **`{:block/_parent ...}`** — recursive pull, full tree in one call

A page's own tags and the tags on its blocks are different queries. The UI
merges them; the DB does not.

Similarly, a property with a value and a property *declared but unset* are
different queries. Unset properties have no datom — they come from the class
via `:logseq.property.class/properties`, and no query over the page will
surface them.

---

## 7. Constraints that shape the tool surface

**Property namespace sandbox.** API-created properties land in
`plugin.property.<caller-id>/*`. The namespace is assigned from caller
identity and cannot be overridden — passing an explicit `ident` is silently
discarded. Properties created in the UI live in `user.property/*` and can be
read but not written. Tools must surface this as a constraint, not fail
mysteriously.

Plugin idents are deterministic (`:plugin.property._test_plugin/<Title>`, no
suffix) and so are predictable, but read the one returned in `verified_state`
rather than assembling it — Logseq normalizes titles. Tag and user-property
idents get random suffixes and must be read back.

**Closed values.** `Status` and `Priority` are enums. `getAllProperties`
reports the permitted values as `:property/closed-values`, but no such datom
exists — it is synthesised from the reverse of `:block/closed-value-property`,
which lives on each value pointing back at its property. Query the real
attribute; setting one means passing an entity id, not a string.

**Recycling preserves entities.** A recycled page keeps its UUID, tags and
refs, gaining `:logseq.property/deleted-at`,
`:logseq.property/deleted-by-ref` and
`:logseq.property.recycle/original-page`. Recycled pages still carry
`:block/tags 4`, so **every page listing must exclude them** or they appear
as live pages. Backlinks to a recycled page are not rewritten.

**Batching works, and it no longer needs a read-back cycle.**
`insertBatchBlock` takes a whole level of siblings in one call and **returns
the entities it created**, so a parent's UUID is known before its own children
are inserted. Building an outline is therefore one call per parent that has
children — not the 2d−1 an earlier version of this section described, which
assumed creation returned nothing and names did not resolve. The second half of
that assumption still holds: names do not resolve, which is why the returned
entities matter.

---

## 8. Capability reporting

The previous `capabilities` implementation probed three read methods and
reported everything else from hardcoded tuples. It listed `getBlock`,
`removeBlock` and `updateBlock` as rejected. All three work. Downstream code
routed block deletion through the CLI because of that literal.

Rules for the replacement:

1. **Self-description and backend claims are different kinds of fact.** What
   tools this server exposes is certain. What Logseq supports is a claim
   about software we do not control. Never merge them into one list.

2. **Three states, not two.** `supported` / `absent` / `unknown`. A `null`
   response yields `unknown` — never `absent`. The old binary had no way to
   express uncertainty, so uncertainty was recorded as fact.

3. **Every claim carries provenance** — probed, inferred, or declared — and a
   timestamp. A reader must be able to tell a test result from a typed-in
   assumption.

4. **Writes are probed without writing.** Call each write method once with a
   deliberately invalid argument. A validation error proves the method exists
   and touched nothing. Only an explicit not-supported message proves
   absence.

5. **Report tools, not methods.** The default response describes what the
   caller can invoke, with constraints where they apply. Raw `logseq.DB.*`
   findings stay available behind a maintainer flag — they are how
   availability is determined and they caught the `removeBlock` error — but
   they are implementation detail and do not belong in the caller-facing
   response.

---

## 9. Tool surface

Semantic operations, named so a caller never has to choose between two tools
that do the same thing. There is no `addBlockTag` / `addPageTag` split: a page
is a block, the target is uniform, so there is one `addTag`.

### Tags

```
getTagUUID(title)                  -> uuid
getTag(uuid)
getTagUsers(uuid)                  -> everything carrying the tag
creatTag(title)
deleteTag(uuid)
addTag(targetUuid, tagUuid)        target may be a page or a block
removeTag(targetUuid, tagUuid)     target may be a page or a block
```

### Properties

```
getPropertyIndent(title)           -> ident
getProperyUsers(ident)             -> everything holding a value
createProperty(title, schema)      title only; namespace is caller-assigned
deleteProperty(ident)              removes the definition graph-wide
addProperty(uuid, ident, value)    sets a value on one target
removeProperty(uuid, ident)        clears a value from one target
```

`createProperty` / `deleteProperty` act on the **definition**; `addProperty` /
`removeProperty` act on a **value**. Different entities, different identifier
types — definitions are keyed by ident, targets by UUID.

### Blocks

```
getBlockUUID(pageUuid)             -> every block on the page, any depth
getBlock(blockUuid)
getBlockTree(blockUuid)            -> one subtree, with a truncation flag
createBlock(parentUuid, title)     parent may be a page or a block (nests)
createPageofBlocks(pageUuid, md)   one insertBatchBlock per parent
updateBlock(blockUuid, title)
moveBlock(blockUuid, targetUuid, placement)   child | last-child | before | after
removeBlock(blockUuid)             takes the whole subtree
```

`createPageofBlocks` is a tool rather than a caller-side loop because
structure has to be right on the way in: `insertBatchBlock` returns the
entities it created, and threading those UUIDs into the next level is exactly
the sequence a caller gets wrong silently (§2).

`moveBlock` no-ops when the position would not change, which the tool reports
as `verified: false` rather than as a false success. `placement=child`
PREPENDS -- the route's behaviour, left alone because callers depend on it --
so `last-child` exists to append. It has no route of its own: nothing here can
write `:block/order`, so it reads the target's children and moves the block
after the current last one, then verifies the block ended up LAST rather than
merely under the right parent. Without it, relocating a sequence reversed it,
silently and with no safe alternative. A move carries the subtree, and the
tool checks all three of the new parent, the owning page, and whether
descendants followed.

### Pages

```
getPageUUID(title)                 -> uuid, or candidates when ambiguous
inspectPage(pageUuid, detail)      page | blocks | tags | properties | declared | all
pageStats(pageUuid)                -> counts only
findBacklinks(uuid)                -> refs, tag holders, property values
findOrphans(pageUuid)              -> informational, not a repair signal
createPage(title)
renamePage(pageUuid, newTitle)
deletePage(pageUuid)               recycles; references are not rewritten
clearPage(pageUuid)                empties a page, keeps the page
importPage(target, markdown)       a whole page in one call
repairLinks(...)                   converts the placeholders importPage left
```

The detail selector matters because a page's own tags and its blocks' tags are
different queries, and declared-but-unset properties appear in neither (§6).

It is `inspectPage` rather than `getPage` because it returns far more than a
page entity, and because `logseq.DB.getPage` is a different and much narrower
thing.

### Lists

No arguments; each returns the whole of one kind.

```
listPages         listJournals      listTags           listProperties
listClosedValues  listOrphanTags    listOrphanProperties
listAssets        listStatus        listRecycled
```

### Naming

Four names are worth revisiting before they harden:

- **`getBlockUUID` returns a list**, not a UUID. It behaves like
  `listPageBlocks`; the name invites a caller to expect one value.
- **`getPropertyIndent` returns a `:db/ident`.** "Indent" is whitespace — a
  model may reasonably infer it deals with nesting.
- **`removeProperty` vs `deleteProperty`** carry the value/definition
  distinction only in convention, and take different identifier types. Choosing
  wrong yields a silent null — the failure mode §1 is about.
- **`creatTag`, `getProperyUsers`, `listTags`** — first two appear to be
  typos; the third was corrected from `listATgs`.

Missing tools are listed in §3 under "Routes with no tool", alongside the
routes that would serve them.

## 10. Guiding rule

> The MCP exposes semantic operations over pages, blocks, tags and
> properties. Each tool has one route, recorded in a table built from live
> testing rather than inferred from a preference order, and carrying the
> status of the evidence behind it. Every write is followed by a read-back,
> and a write whose read-back fails is an error, not a success. Identifiers
> are validated at the boundary, because this API's characteristic failure is
> success that did nothing.

§2 states the standard; §3 records how far the current surface meets it. Two
write tools — `deleteTag` and `deleteProperty` — are still shipping on
unverified routes, and both are destructive: `deleteTag` strips the tag from
everything carrying it, and `deleteProperty` takes every value of the property
with it. Until they are run against a live graph they are assumptions, which is
why both gate on an explicit acknowledgement and why their read-backs are the
only thing standing between a silent no-op and a reported success.

---

## Open questions

- Whether built-in properties (`:logseq.property/status`, `priority`,
  `deadline`, `scheduled`) are writable, or blocked like `user.property/*`
- Whether `removeProperty` (the route behind `deleteProperty`) works at all,
  by any identifier — the UUID form was confirmed to do nothing
- Whether `deleteTag` routing to `deletePage` is correct at all
- Whether recycling is reversible by clearing `:logseq.property/deleted-at`
- Whether any property namespace is shared between callers — if none is, two
  integrations cannot see each other's properties, and a property the user can
  edit in the UI can never be written through the API
- What ident shape `createTag` actually assigns — the tool reads it back, so
  nothing depends on the answer, but two documents used to guess differently
- How assets are modelled

Answered since this list was written: `deletePage` accepts the page UUID (the
name is still tried as a fallback, since a silent no-op is indistinguishable
from success); moving a block goes through `moveBlock` and is verified on every
placement; batch order does determine `:block/order`, which is why
`insertBatchBlock` preserves the order its items were written in.
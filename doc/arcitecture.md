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
| `creatTag` | `createTag` | untested |
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
| `createBlock` | `upsertNodes` add+block | **verified**, nested included |
| `updateBlock` | `updateBlock` | **verified** |
| `removeBlock` | `removeBlock` | **verified** |
| `createManyBlocks` | `upsertNodes`, batched | **verified** |
| `createPageofBlocks` | `upsertNodes` + `datascriptQuery`, per level | probable |

### Pages

| Tool | Route | Status |
|---|---|---|
| `getPageUUID` | `datascriptQuery` | verified |
| `getPage` | `datascriptQuery` (per detail selector) | verified |

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

**`updateBlock` and `upsertNodes` edit+block both work.** The old design would
have called that redundancy to be eliminated. Keep both: `upsertNodes` batches
and `updateBlock` does not, so they differ in throughput, not semantics.

**`getBlock`, `updateBlock` and `removeBlock` were all listed as rejected** by
the previous capability implementation. All three work. See §8.

### Routes with no tool

Working routes that nothing currently exposes:

| Route | Would support |
|---|---|
| `upsertNodes` add+page | create a page |
| `renamePage` | rename a page |
| `deletePage` | delete or recycle a page |
| `removeBlock`, looped | clear a page without deleting it |

**`move block` has no identified route at all.** `insertBatchBlock` and
`prependBlockInPage` remain untested, and the CLI fallback previously assumed
for it rested on the capability list that turned out to be wrong.

## 4. Identifier discipline

Each entity kind has one canonical key. Passing the wrong one fails silently.

| Kind | Key | Notes |
|---|---|---|
| block | `:block/uuid` | |
| page | `:block/uuid` | a page **is** a block; same methods apply |
| tag | UUID for relations, `:db/ident` for lookups | ident carries a random suffix; must be read back |
| property | `:db/ident` | UUID fails silently |
| `:db/id` | queries only | integers are not stable across rebuilds; never persist |

**Names are never identifiers.** `page-id` does not resolve page names.
`removeProperty` does not accept a title. Name lookup is a separate,
explicit resolution step that must return exactly one match or fail.

The MCP surface accepts UUIDs uniformly and resolves internally to whatever
each route requires. Callers should never need to know that properties are
keyed differently from blocks.

### Validation at the boundary

Datascript queries are built by string interpolation, and the query travels
as a string inside the JSON envelope — `json.dumps` escapes the envelope but
cannot stop a value from breaking out of a query literal. Every value that
reaches query text is validated:

- UUIDs must match the canonical 8-4-4-4-12 form
- idents must match keyword shape
- string literals containing quotes or backslashes are **rejected**, not
  escaped — a quoted title is far more likely to be a bad paste than a real
  title, and guessing wrong means operating on the wrong entity

This is not a security boundary — the caller already holds the token. It
converts silent nulls into loud errors, which given §1 is the point.

---

## 5. `page-id` is a parent pointer

The single most useful discovery, and it is invisible from the field name.

```json
{"operation": "add", "entityType": "block",
 "data": {"page-id": "<page uuid>",  "title": "..."}}   → top-level block
{"operation": "add", "entityType": "block",
 "data": {"page-id": "<block uuid>", "title": "..."}}   → nested child
```

One field, both behaviours. Nested block creation is available over HTTP and
does not need the CLI.

`data` is a **closed allowlist**: only `page-id` and `title`. `parent-id` is
rejected as a disallowed key. There is no way to set tags, order, or position
at creation — each is a follow-up call.

The general lesson: where a method or field names one entity type, try the
other before believing the restriction. `removeBlockTag` works on pages for
the same reason.

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
suffix) and can be constructed client-side. Tag and user-property idents get
random suffixes and must be read back.

**Closed values.** `Status` and `Priority` are enums —
`:property/closed-values` lists the permitted value entities. Setting them
means passing an entity id, not a string.

**Recycling preserves entities.** A recycled page keeps its UUID, tags and
refs, gaining `:logseq.property/deleted-at`,
`:logseq.property/deleted-by-ref` and
`:logseq.property.recycle/original-page`. Recycled pages still carry
`:block/tags 4`, so **every page listing must exclude them** or they appear
as live pages. Backlinks to a recycled page are not rewritten.

**Batching works.** `upsertNodes` takes an array of operations. Building an
outline is 2d−1 calls for depth d, independent of width: create a level, read
back the server-assigned UUIDs, create the next. The read-back is
unavoidable — creation does not return UUIDs and names do not resolve.

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
createBlock(parentUuid, title)     parent may be a page or a block (nests)
getBlock(blockUuid)
updateBlock(blockUuid, title)
removeBlock(blockUuid)
createManyBlocks(op, op, ...)      one batched upsertNodes
createPageofBlocks(indented_md)    create / read-back / create, per level
```

`createPageofBlocks` is a tool rather than a caller-side loop because the
create/read-back/create cycle is the most silent-failure-prone sequence in
this API (§2). UUIDs are not returned by creation and names do not resolve, so
the read-back between levels is structural, not defensive.

### Pages

```
getPageUUID(title)                 -> uuid
getPage(pageUuid, detail)          detail: page | block | tags | properties | all
```

The detail selector matters because a page's own tags and its blocks' tags are
different queries, and declared-but-unset properties appear in neither (§6).

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

§2 states the standard; §3 records how far the current surface meets it. Four
write tools — `creatTag`, `deleteTag`, `deleteProperty`, `removeProperty` —
are shipping on untested routes. Until those are run against a live graph they
are assumptions, and `deleteTag` is a destructive one.

---

## Open questions

- Moving a block — no route identified and no tool exposed;
  `insertBatchBlock` and `prependBlockInPage` untested
- Whether built-in properties (`:logseq.property/status`, `priority`,
  `deadline`, `scheduled`) are writable, or blocked like `user.property/*`
- Whether `deletePage` keys on UUID or name
- Whether `removeProperty` (the route behind `deleteProperty`) works at all,
  by any identifier — the UUID form was confirmed to do nothing
- Whether batch order determines `:block/order`
- Whether recycling is reversible by clearing `:logseq.property/deleted-at`
- How assets are modelled
- `createTag` argument shape — never run
- Whether `deleteTag` routing to `deletePage` is correct at all
# API reference

Every tool, the raw HTTP call behind it, and the constraints that are not
obvious from the signature.

All calls are `POST {baseUrl}/api` with a JSON body:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["..."]}
```

**Status** — `verified` means run against a live graph with the effect
confirmed by read-back. `probable` means the route is right but this exact call
was not run. `untested` means no evidence; treat the result as a hypothesis.

**Identifiers.** Each entity kind has one canonical key, and passing the wrong
one returns success while doing nothing:

| Kind | Key |
| --- | --- |
| block, page | `:block/uuid` |
| tag | UUID for relations, `:db/ident` for lookups |
| property | `:db/ident` — a UUID here is a silent no-op |
| `:db/id` | queries only; integers are renumbered on rebuild |

---

## Verified writes

Every write tool takes `verbose`, default `true`. Terse mode
(`verbose: false`) shapes the RESPONSE only: the write still issues its
mutation, still reads the target back, still compares, and still reports the
same `verified`. What it drops is the entity payload, which a write envelope
carries twice — before and after — and which for a block write is the block's
own text. Terse returns `verified`, `uuid`, `parent`, `page`, `order`,
`diagnostic`, and `ident` where one was assigned; on a failure it keeps the
observed entities as the same digests, because a write cannot safely be
repeated to get detail.

---

## Tags

A tag must exist before it can be attached. Attaching to a page and to a block
are the same operation — a page **is** a block in the DB — so there is one
`addTag`, not two.

Tag idents are assigned by Logseq rather than derived from the title — ones
created in the UI carry a random suffix (`:user.class/xzy-bc0auNqC`) — so read
the ident back after creation rather than constructing it.

Removing a tag removes that one relation. The target's other tags and the tag
entity itself are untouched. There is no single-call "set the tag list" route,
and that is just as well: overwriting the whole set risks dropping
`:logseq.class/Page` and with it the target's page identity.

Tags declare property slots via `:logseq.property.class/properties`. A page
tagged with a class inherits those properties as *available* — declared, but
with no value until one is assigned.

| Tool | Route | Status |
| --- | --- | --- |
| `getTagUUID(title)` | `getTagsByName` | probable |
| `getTag(uuid)` | `datascriptQuery` | verified |
| `getTagUsers(uuid)` | `datascriptQuery` | verified |
| `creatTag(title)` | `createTag` | probable — creation observed by read-back; the assigned ident shape is not pinned |
| `deleteTag(uuid)` | `deletePage` | **untested — identifier type unconfirmed** |
| `addTag(target, tag)` | `addBlockTag` | probable |
| `removeTag(target, tag)` | `removeBlockTag` | **verified** |

```json
{"method": "logseq.DB.getTagsByName", "args": ["$TITLE"]}
```
```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?t [*]) . :where [?t :block/uuid #uuid \"$UUID\"]]"]}
```
```json
{"method": "logseq.DB.createTag", "args": ["$TITLE"]}
```
```json
{"method": "logseq.DB.addBlockTag", "args": ["$TARGET_UUID", "$TAG_UUID"]}
```
```json
{"method": "logseq.DB.removeBlockTag", "args": ["$TARGET_UUID", "$TAG_UUID"]}
```

Everything carrying a tag — pages and blocks together. Holders with
`block/name` are pages. This is the work list for removing a tag everywhere,
and the check to run before deleting one.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?e [:db/id :block/uuid :block/title :block/name {:block/page [:db/id :block/uuid :block/title]}]) ...] :where [?t :block/uuid #uuid \"$TAG_UUID\"] [?e :block/tags ?t]]"]}
```

---

## Properties

Two different things share the word. A **definition** is an entity with a
`:db/ident` and a type. A **value** is that ident set on a page or block. They
take different identifiers and different tools.

> **Namespace sandbox.** Property writes reach only
> `plugin.property.<caller-id>/*`. The namespace is assigned from caller
> identity and cannot be chosen — passing an explicit ident is silently
> discarded. Properties created in the Logseq UI live under `user.property/*`
> and are readable but **not writable** over HTTP. Built-ins under
> `:logseq.property/` are also outside the sandbox.

Plugin idents are deterministic (`:plugin.property._test_plugin/<Title>`, no
suffix), so they are predictable — but read the one returned in
`verified_state` rather than assembling it, since Logseq normalizes titles.
User and tag idents get random suffixes and must be looked up.

Types: `default` (text), `number`, `string`, `datetime`, `checkbox`, `url`,
`node`, `page`, `class`, `property`, `map`. Reference types take an entity id,
not a literal. Properties also carry a cardinality — `one` replaces on write,
`many` adds to a set.

`Status` and `Priority` are closed enums. Their permitted values are reported
by `getAllProperties` as `:property/closed-values`, but **no such datom
exists** — the list is synthesised from the reverse of
`:block/closed-value-property`, which lives on each value pointing back at its
property. Query the real attribute, as `listClosedValues` does, and pass one of
those value entities on a write.

| Tool | Route | Status |
| --- | --- | --- |
| `getPropertyIndent(title)` | `datascriptQuery` | verified |
| `getProperyUsers(ident)` | `datascriptQuery` | verified |
| `createProperty(title, schema)` | `upsertProperty` | **verified** |
| `deleteProperty(ident)` | `removeProperty` | **untested — UUID form confirmed to do nothing** |
| `addProperty(target, ident, value)` | `upsertBlockProperty` | probable |
| `removeProperty(target, ident)` | `removeBlockProperty` | untested |

Resolve a title to an ident. The `:block/tags ?class` clause is what restricts
this to properties — tags carry idents too and would otherwise match.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?p [:db/id :db/ident :block/uuid :block/title :logseq.property/type]) ...] :in $ ?class :where [?p :block/tags ?class] [?p :block/title \"$TITLE\"]]", 3]}
```

Everything holding a value, with the value in raw and resolved form —
reference types store an entity id, scalars store a literal, and one query has
to serve both. The value is deliberately **not** pulled: `pull` needs an entity
id, and checkbox and datetime properties store literals inline, so pulling made
the query 500 with *"Expected number or lookup ref for entity id, got true"* —
which left those properties undeletable. Entity ids in the result are resolved
by a second query instead.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?holder [:db/id :block/uuid :block/title :block/name {:block/page [:db/id :block/uuid :block/title]}]) ?value :where [?holder $IDENT ?value]]"]}
```

The ident is interpolated into the query text rather than passed as a
parameter, because an attribute position takes no `:in` binding. That is why
every ident is shape-checked at the boundary before it gets here.

Create a definition. The first argument is a plain **title** — a namespaced
string is rejected as a page name (`Page name can't include "/"`).

```json
{"method": "logseq.DB.upsertProperty", "args": ["$TITLE", {"type": "number"}]}
```

Set and clear a value on a page or block:

```json
{"method": "logseq.DB.upsertBlockProperty", "args": ["$TARGET_UUID", "$IDENT", "$VALUE"]}
```
```json
{"method": "logseq.DB.removeBlockProperty", "args": ["$TARGET_UUID", "$IDENT"]}
```

Delete a definition graph-wide, taking every value with it. Not reversible —
recreating mints a new entity.

```json
{"method": "logseq.DB.removeProperty", "args": ["$IDENT"]}
```

---

## Blocks

Block writes go through `insertBlock` and `insertBatchBlock`, **not**
`upsertNodes`. `upsertNodes` fails outright on synced graphs — "The Imported
EDN has N validation error(s)" for a write its own dry run accepts — and it
writes its single `page-id` into both `:block/parent` and `:block/page`, so a
block parent produced a child whose owning page was the parent block. It is out
of the client allowlist entirely; see `archive/` for what it used to say here.

> **The parent may be a page or a block.** `insertBlock` takes the target's
> UUID: a page UUID makes a top-level block, a block UUID nests. One argument,
> both behaviours — which is why nested creation needs no separate route.
> `{"sibling": false}` is what makes it a child rather than a neighbour.

Only the title can be set at creation. Tags, properties and position are
follow-up calls.

Unlike `upsertNodes`, `insertBlock` and `insertBatchBlock` **return the
entities they created**, so a new UUID is known without a follow-up read —
which is what removed the read-back cycle from outline building.

| Tool | Route | Status |
| --- | --- | --- |
| `getBlockUUID(page_uuid)` | `datascriptQuery` | verified |
| `getBlock(uuid)` | `getBlock` | **verified** |
| `getBlockTree(uuid)` | `datascriptQuery` | verified |
| `createBlock(parent, title)` | `insertBlock` | **verified, nesting included** |
| `createPageofBlocks(page, outline)` | `insertBatchBlock` per parent | **verified** |
| `updateBlock(uuid, title)` | `updateBlock` | **verified** |
| `moveBlock(uuid, target, placement)` | `moveBlock` | **verified, all placements**; `last-child` is built on `before:false` |
| `removeBlock(uuid)` | `removeBlock` | **verified** |

Every block on a page, at any depth. This walks `:block/parent` recursively
rather than scoping to `:block/page`: on some graphs a block's `:block/page`
points at an ancestor block, and a page-scoped query misses it even though
Logseq displays it normally.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?root [:db/id :block/uuid :block/title :block/name :block/order {:block/parent [:db/id :block/uuid]} {:block/page [:db/id :block/uuid]} {:block/_parent ...}]) . :where [?root :block/uuid #uuid \"$PAGE_UUID\"]]"]}
```

The pull pattern must name the attributes it wants: `...` recurses the *whole*
pattern, so a pattern of only `{:block/_parent ...}` returns nodes carrying no
id, uuid or title — which reads as an empty page rather than as an error.

```json
{"method": "logseq.DB.getBlock", "args": ["$BLOCK_UUID"]}
```
```json
{"method": "logseq.DB.removeBlock", "args": ["$BLOCK_UUID"]}
```
```json
{"method": "logseq.DB.updateBlock", "args": ["$BLOCK_UUID", "$TITLE"]}
```

Create one, or a whole level of siblings in a single call:

```json
{"method": "logseq.DB.insertBlock", "args": ["$PARENT_UUID", "$TITLE", {"sibling": false}]}
```
```json
{"method": "logseq.DB.insertBatchBlock", "args": ["$PARENT_UUID", [{"content": "$TITLE"}, {"content": "$TITLE"}], {"sibling": false}]}
```

Move a block and its subtree. `{"children": true}` places it under the target
and **prepends**; `{"before": true|false}` places it as a sibling. The method
returns nothing whether it moved the block or did nothing, so the outcome comes
from reading the block back — its parent, its owning page, and whether
descendants followed.

```json
{"method": "logseq.DB.moveBlock", "args": ["$BLOCK_UUID", "$TARGET_UUID", {"children": true}]}
```

The `moveBlock` TOOL exposes a fourth placement, `last-child`, which appends.
It has no route of its own: nothing in this API can write `:block/order`, so it
reads the target's children and moves the block after the current last one.
That extra read is also what makes it checkable — appending is verified by the
block ending up last, not merely under the right parent. `child` is left
prepending because that is the route's behaviour and callers depend on it.

`createPageofBlocks` costs **one call per parent that has children** — not
2d−1. Because each batch response carries the entities it created, a parent's
UUID is known before its own children are inserted, so there is no
create/read-back/create cycle. Duplicate titles among siblings are fine for the
same reason: nothing has to identify a new block by its title.

It is not atomic. A failure at the third level leaves the first two committed;
the error names the level that stopped.

---

## Pages

| Tool | Route | Status |
| --- | --- | --- |
| `getPageUUID(title)` | `getPage`, then `datascriptQuery` | verified |
| `isTitleAvailable(title)` | `datascriptQuery` | verified |
| `inspectPage(uuid, detail)` | `datascriptQuery` (per selector) | verified |
| `pageStats(uuid)` | `datascriptQuery` | verified |
| `findBacklinks(uuid)` | `datascriptQuery` | verified |
| `findOrphans(uuid)` | `datascriptQuery` | verified |
| `createPage(title)` | `createPage` | **verified** |
| `renamePage(uuid, title)` | `renamePage` | **verified** |
| `retitleOverDuplicate(uuid, title)` | `renamePage` × 2 | **verified**, not atomic |
| `deletePage(uuid)` | `deletePage` | **verified — recycles; UUID tried first, name second** |
| `clearPage(uuid)` | `removeBlock`, looped | **verified** |
| `importPage(target, markdown)` | `insertBatchBlock` + `datascriptQuery` | **verified** |
| `repairLinks(...)` | `updateBlock` + `datascriptQuery` | **verified** |

`createPage` is idempotent on title: calling it twice returns the same entity
rather than a duplicate. Its second argument is a **properties map, not
options** — passing `{"dry-run": true}` creates the page anyway *and* mints a
`dry-run` property in the caller's namespace, so there is no server-side dry run
here.

It also seeds every new page with one empty block, which is why a block count
on a page made through this API is never 0.

```json
{"method": "logseq.DB.createPage", "args": ["$TITLE"]}
```
```json
{"method": "logseq.DB.renamePage", "args": ["$PAGE_UUID", "$NEW_TITLE"]}
```
```json
{"method": "logseq.DB.deletePage", "args": ["$PAGE_UUID"]}
```

`deletePage` **recycles**: the page keeps its UUID, tags, refs and blocks,
gains `:logseq.property/deleted-at`, and drops out of `listPages`. Inbound
references are not rewritten.

An **alias relation** is checked separately and requires its own
acknowledgement. `alias` is a built-in property, outside the namespace this
server may write, so a relation broken by a delete cannot be rebuilt here.
Both spellings are queried, because DB graphs carry the built-in as
`:logseq.property/alias` while older ones use `:block/alias`, and a guard
matching only one silently never fires:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?holder [:db/id :block/uuid :block/title :block/name]) ...] :in $ ?target :where (or-join [?holder ?target] [?holder :logseq.property/alias ?target] [?holder :block/alias ?target])]", 846]}
```

The reverse direction — the aliases a page declares — is the same query with
the roles swapped. `pageStats` reports both as `is_alias_of` and `aliases`,
because an alias relation is invisible to every block and reference count, so
an empty page that is a functioning alias reads as a dead stub.

Resolve a title. `getPage` accepts a name or a UUID and is tried first, but
its result is checked three times before being trusted — it returns recycled
pages, a title held only by a tag must resolve to nothing rather than to the
tag, and because it returns a single entity it cannot tell a unique title from
a duplicated one. That last check is a count, which is cheap; anything other
than exactly one page-classed holder falls through. The query below is that
fallback, and the only path that can see every match and so report ambiguity
rather than guessing.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?page [:db/id :block/uuid :block/name :block/title :logseq.property/deleted-at]) ...] :in $ ?class :where [?page :block/tags ?class] [?page :block/title \"$TITLE\"]]", 4]}
```

If that returns nothing, the same query against `:block/name` with the title
lowercased is tried — `:block/name` is the normalized form, so the exact string
Logseq stores will not match `:block/title`.

### Resolving is not the same as checking availability

`getPageUUID` filters recycled pages out; `createPage` and `renamePage` do not,
because a recycled entity still exists and still holds its title. So the same
title can be unresolvable and unwritable at once. Both behaviours are
deliberate — a resolver that returned recycled pages would let a link point at
a deleted page — and `isTitleAvailable` exists so the difference is visible
rather than discovered by a failed write.

It runs the writers' query, which is broader than the resolver's in two ways:
no recycled filter, and no Page-class filter, so a block or a tag holding the
title counts.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?entity [*]) ...] :where [?entity :block/title \"$TITLE\"]]"]}
```

Holders are classified by what they carry: `:block/name` means page, otherwise
the `:block/tags` entry for `:logseq.class/Tag` or `:logseq.class/Property`
decides, and anything else is a block. Those two class ids are resolved only
when a holder is not a page, so the common cases cost one query.

`detail` selects what comes back, and the options are **not** interchangeable:

- `page` — the page entity alone
- `blocks` — every block at any depth
- `tags` — the page's own tags *and* its blocks' tags, which live in different places
- `properties` — properties that have a **value**
- `declared` — property slots inherited from the page's classes, which have no datoms and appear in no other query
- `all` — the above combined

The page entity, with tags resolved:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?p [* {:block/tags [:db/id :db/ident :block/title]}]) . :where [?p :block/uuid #uuid \"$PAGE_UUID\"]]"]}
```

The full tree. `*` returns property values as raw `:db/id` refs — there is no
way to wildcard-resolve unknown attributes in a pull spec, which is why
`properties` is a separate query.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?p [* {:block/tags [:db/ident :block/title]} {:block/_parent [* {:block/tags [:db/ident :block/title]} {:block/_parent ...}]}]) . :where [?p :block/uuid #uuid \"$PAGE_UUID\"]]"]}
```

Declared-but-unset properties, via the page's classes:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?c [:db/ident :block/title]) (pull ?prop [:db/id :db/ident :block/title :logseq.property/type]) :in $ ?page :where [?page :block/tags ?c] [?c :logseq.property.class/properties ?prop]]", 846]}
```

---

### Taking a title back from an empty duplicate

A rename cannot land on a title another entity holds, and recycling the holder
does not release it. But a rename can MOVE the holder out of the way first, and
a recycled page can be renamed — which is what finally releases its title. So
`retitleOverDuplicate` is two `renamePage` calls:

```json
{"method": "logseq.DB.renamePage", "args": ["$HOLDER_UUID", "$TITLE (parked)"]}
```
```json
{"method": "logseq.DB.renamePage", "args": ["$FROM_UUID", "$TITLE"]}
```

No block is touched. References are stored as entity ids, so every inbound
link, tag and property value on both pages is unaffected by either rename —
which is why this is cheaper and less lossy than de-resolving references,
deleting a page and re-resolving.

The guards are reads: block counts for the holder, and an alias check in both
directions, since an alias is live resolution wiring rather than an abandoned
duplicate.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [?holder ...] :in $ ?target :where [?holder :block/alias ?target]]", 846]}
```

Not atomic: the two renames are separate transactions, so a failure on the
second leaves the holder parked.

---

## Lists

Each returns the whole of one kind. Most take no arguments; the two page
listings take `with_counts` and `limit`.

Class markers appear throughout: `:logseq.class/Tag` is `:db/id` 2,
`Property` 3, `Page` 4 on a typical graph. **Resolve them by ident rather than
hardcoding** — integers are renumbered when a graph is rebuilt:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find ?class . :where [?class :db/ident :logseq.class/Page]]"]}
```

**`listPages`** — recycled pages keep the Page class, so they must be excluded
explicitly or they appear live.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?p [:db/id :block/uuid :block/name :block/title]) ...] :in $ ?class :where [?p :block/name] [?p :block/tags ?class] [(missing? $ ?p :logseq.property/deleted-at)]]", 4]}
```

**`listJournals`** — `:block/journal-day` is an integer date that sorts
chronologically. The tool returns them newest first.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?p [:db/id :block/uuid :block/name :block/title :block/journal-day]) ...] :where [?p :block/journal-day _]]"]}
```

Both take `with_counts`, which attaches `own_blocks`, `content_blocks` and
`refs` to each entry. It is three aggregate queries on top of the listing —
four calls in total, whatever the number of pages — rather than one
`pageStats` per page. Each aggregate binds the listed ids with
`:in $ [?page ...]`, so the response is one row per listed page rather than
one per page in the graph:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find ?page (count ?block) :in $ [?page ...] :where [?block :block/page ?page]]", [846, 847]]}
```
```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find ?page (count ?block) :in $ [?page ...] :where [?block :block/page ?page] [?block :block/title \"\"]]", [846, 847]]}
```
```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find ?target (count ?holder) :in $ [?target ...] :where [?holder :block/refs ?target]]", [846, 847]]}
```

A page with no blocks does not appear in the join at all, so the absence is
read as zero rather than merged positionally. The attributes are the same ones
`pageStats` counts, so the two cannot disagree.

With `with_counts` the result is an envelope — the list under `pages` or
`journals`, plus `total`, `counted` and `truncated` — because the counted form
is capped at 500 rows and a capped result has to be able to say so. Without
it, both return the bare list they always returned.

**`listTags`** and **`listProperties`** use dedicated methods:

```json
{"method": "logseq.DB.getAllTags", "args": []}
```
```json
{"method": "logseq.DB.getAllProperties", "args": []}
```

**`listClosedValues`** — required before setting `Status`, `Priority`, or any
enum property. Note the attribute: `:block/closed-value-property` lives on each
VALUE, pointing back at its property. `:property/closed-values` is what
`getAllProperties` reports and matches nothing on any graph, and
`:closed-value-property` is what a probe sees because responses strip the
`:block/` prefix. Three attempts, each reading a response as if it described
the schema.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?prop [:db/id :db/ident :block/title]) (pull ?value [:db/id :db/ident :block/title :logseq.property/value :block/order]) :where [?value :block/closed-value-property ?prop]]"]}
```

**`listOrphanTags`** — tags nothing carries.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?t [:db/id :db/ident :block/uuid :block/title]) ...] :in $ ?class :where [?t :block/tags ?class] [(missing? $ ?t :block/_tags)]]", 2]}
```

**`listOrphanProperties`** — no single-query form exists. Each property is its
own DB attribute, so this lists all properties and checks each one:

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [?holder ...] :where [?holder $IDENT _]]"]}
```

**`listStatus`** — everything with a Status value, paired with the status.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?e [:db/id :block/uuid :block/title :block/name {:block/page [:block/title]}]) (pull ?v [:db/ident :block/title]) :where [?e :logseq.property/status ?v]]"]}
```

**`listRecycled`** — recycling preserves the entity: same UUID, tags and refs,
plus `:logseq.property/deleted-at`, `:logseq.property/deleted-by-ref` and
`:logseq.property.recycle/original-page`. Inbound references are **not**
rewritten.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?p [:db/id :block/uuid :block/name :block/title :logseq.property/deleted-at]) ...] :where [?p :logseq.property/deleted-at _]]"]}
```

**`listAssets`** — untested. Asset modelling was never established; the current
query is a discovery probe rather than a working list.

---

## No tool covers these

Tag inheritance and tag-level property declaration. `addTagExtends`,
`removeTagExtends`, `addTagProperty` and `removeTagProperty` have working
routes and no tool, so a tag's parent and its declared property slots can be
read but not written. `setBlockIcon` and `removeBlockIcon` are the same case
with less at stake.

Everything else that was listed here — creating, renaming, deleting and
clearing a page, and moving a block — now has a tool. The moving entry in
particular said "no identified route at all"; `moveBlock` is verified on every
placement, including across pages.

`prependBlockInPage`, `addPropertyValueChoices` and `newBlockUUID` remain
untested and unexposed. See `logseq-api-surface.md` for the rest of the API and
why it is out of reach.

---

## Escaping

Datascript queries travel as a **string inside** the JSON body, so `#uuid "..."`
becomes `#uuid \"...\"`. Getting this wrong produces a Fastify
`FST_ERR_CTP_INVALID_JSON_BODY` before Logseq sees the request.

Keyword idents go in bare — `[?p :db/ident :logseq.property/status]`, no quotes
and no reader tag. UUIDs need both.

Do not edit these queries inside Logseq. `#uuid` autocompletes into a tag
reference (`#[[...]]`), and `[[`, `((` and `{{` transform too. Use a code block
if you must paste one into a note.

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

## Tags

A tag must exist before it can be attached. Attaching to a page and to a block
are the same operation — a page **is** a block in the DB — so there is one
`addTag`, not two.

Tag idents carry a random suffix (`:user.class/xzy-bc0auNqC`), so they cannot
be constructed from the title and must be read back after creation.

Removing a tag removes that one relation. The target's other tags and the tag
entity itself are untouched. There is no `upsertNodes` route for removal:
`operation` offers only `add` and `edit`, with no retraction verb, so a removal
expressed as an upsert would mean overwriting the whole tag set — and risking
the loss of `:logseq.class/Page`.

Tags declare property slots via `:logseq.property.class/properties`. A page
tagged with a class inherits those properties as *available* — declared, but
with no value until one is assigned.

| Tool | Route | Status |
| --- | --- | --- |
| `getTagUUID(title)` | `getTagsByName` | probable |
| `getTag(uuid)` | `datascriptQuery` | verified |
| `getTagUsers(uuid)` | `datascriptQuery` | verified |
| `creatTag(title)` | `createTag` | untested |
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
suffix) so they can be constructed client-side. User and tag idents get random
suffixes and must be looked up.

Types: `default` (text), `number`, `string`, `datetime`, `checkbox`, `url`,
`node`, `page`, `class`, `property`, `map`. Reference types take an entity id,
not a literal. Properties also carry a cardinality — `one` replaces on write,
`many` adds to a set.

`Status` and `Priority` are closed enums; their permitted values are listed in
`:property/closed-values` and a write must use one of those entities.

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
to serve both.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?e [:db/id :block/uuid :block/title :block/name]) ?v (pull ?v [:db/id :db/ident :block/title]) :where [?e $IDENT ?v]]"]}
```

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

`upsertNodes` accepts exactly three combinations: `add`+`page`, `add`+`block`,
`edit`+`block`. `edit`+`page` returns *"Editing a page, tag or property isn't
supported yet"*. There is no removal operation at all.

> **`page-id` is a parent pointer, not a page pointer.** A page UUID makes a
> top-level block; a **block** UUID nests. One field, both behaviours — which
> is why nested creation needs no separate route.

`data` is a **closed allowlist**: only `page-id` and `title`. `parent-id` is
rejected as a disallowed key, and so are `tags` and `order`. Tagging and
positioning are follow-up calls.

The new block's UUID is assigned by Logseq and **not returned**. Read it back
if you need it.

| Tool | Route | Status |
| --- | --- | --- |
| `getBlockUUID(page_uuid)` | `datascriptQuery` | verified |
| `getBlock(uuid)` | `getBlock` | **verified** |
| `createBlock(parent, title)` | `upsertNodes` add+block | **verified, nesting included** |
| `updateBlock(uuid, title)` | `updateBlock` | **verified** |
| `removeBlock(uuid)` | `removeBlock` | **verified** |
| `createManyBlocks([...])` | `upsertNodes`, batched | **verified** |
| `createPageofBlocks(outline)` | `upsertNodes` + `datascriptQuery` per level | probable |

Every block on a page, at any depth. `:block/page` rather than
`:block/parent` — parent reaches one level, page reaches all of them.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?b [:db/id :block/uuid :block/title :block/order {:block/parent [:block/uuid]}]) ...] :in $ ?page :where [?b :block/page ?page]]", 846]}
```

```json
{"method": "logseq.DB.getBlock", "args": ["$BLOCK_UUID"]}
```
```json
{"method": "logseq.DB.removeBlock", "args": ["$BLOCK_UUID"]}
```
```json
{"method": "logseq.DB.updateBlock", "args": ["$BLOCK_UUID", "$TITLE"]}
```

Create one, or many in a single call:

```json
{"method": "logseq.DB.upsertNodes", "args": [[{"operation": "add", "entityType": "block", "data": {"page-id": "$PARENT_UUID", "title": "$TITLE"}}]]}
```

`createPageofBlocks` is three calls per two levels — create a level, read back
the UUIDs Logseq assigned, create the next. The read-back is structural:
creation returns no UUIDs and `page-id` will not resolve a title, so children
cannot name their parents until the level above exists. Cost is 2d−1 calls for
depth *d*, independent of width.

Titles must be unique among **siblings**, because that is the scope
verification searches. Two sections may each have a child called `Notes`.

---

## Pages

| Tool | Route | Status |
| --- | --- | --- |
| `getPageUUID(title)` | `datascriptQuery` | verified |
| `getPage(uuid, detail)` | `datascriptQuery` | verified |

Resolve a title. Returns `found: false` with candidates when ambiguous rather
than guessing — page titles are not unique, and picking a write target from a
fuzzy match is how the wrong entity gets modified.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?p [:db/id :block/uuid :block/name :block/title]) ...] :where [?p :block/name] [?p :block/title \"$TITLE\"]]"]}
```

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

## Lists

Each takes no arguments and returns the whole of one kind.

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
chronologically.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find [(pull ?p [:db/id :block/uuid :block/name :block/title :block/journal-day]) ...] :where [?p :block/journal-day _]]"]}
```

**`listTags`** and **`listProperties`** use dedicated methods:

```json
{"method": "logseq.DB.getAllTags", "args": []}
```
```json
{"method": "logseq.DB.getAllProperties", "args": []}
```

**`listClosedValues`** — required before setting `Status`, `Priority`, or any
enum property.

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find (pull ?p [:db/ident :block/title]) (pull ?v [:db/id :db/ident :block/title]) :where [?p :property/closed-values ?v]]"]}
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

Working routes with nothing exposing them: creating a page (`upsertNodes`
add+page), renaming one (`renamePage`), deleting or recycling one
(`deletePage`), and clearing a page's blocks without deleting the page (a loop
over `removeBlock`, since no batch delete exists).

**Moving a block has no identified route at all.** `insertBatchBlock` and
`prependBlockInPage` remain untested.

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
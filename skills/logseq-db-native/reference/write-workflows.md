# Write workflows

Exact shapes and verification steps. Read before an unfamiliar write.

Every write returns `verified`, `verified_state`, and on failure
`previous_state` and `observed_state`. **`verified=false` is a failure**, even
though no error was raised — this API reports success for calls that do
nothing, so the read-back is the only evidence.

---

## Pages

### Creating

```
createPage(title)
```

A title already used by any page or block is rejected rather than duplicated:
verification identifies a new entity by title, so a duplicate could not be
told from the original.

### Renaming

```
renamePage(page_uuid, new_title)
```

Verified by UUID. Reading back by the new title would not distinguish a rename
from Logseq creating a second page and leaving the original untouched, so the
original UUID is re-read and its title compared. The check also confirms
`:block/name` survived — a rename that stripped page identity would otherwise
look like success.

A title another entity already holds is rejected, for the same reason as
`createPage`.

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

The identifier this route accepts is unconfirmed. The UUID is tried first and
the page name second, and the result reports which worked. If the envelope says
`via its name`, record that: it also settles which form `deleteTag` needs.

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

```
createManyBlocks([{parent_uuid, title}, ...])
```

Items are grouped by parent and one call is made per distinct parent. Duplicate
titles are fine, including among siblings: the response carries each created
entity, so nothing has to identify them by title afterwards.

Whether a batch applies atomically is untested. On failure, read back rather
than assuming all-or-nothing.

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

There is **no move**. No route exists for it.

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

This route is **untested**. The UUID form is confirmed to do nothing; whether
the ident form works has not been established. Check `verified`.

---

## Tags

### Creating

```
creatTag(title)
```

The ident carries a random suffix (`:user.class/xzy-bc0auNqC`), so it cannot be
derived from the title and must be read back.

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

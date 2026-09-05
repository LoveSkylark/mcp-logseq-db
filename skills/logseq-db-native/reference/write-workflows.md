# Write workflows

Exact shapes and verification steps. Read before an unfamiliar write.

Every write returns `verified`, `verified_state`, and on failure
`previous_state` and `observed_state`. **`verified=false` is a failure**, even
though no error was raised — this API reports success for calls that do
nothing, so the read-back is the only evidence.

---

## Blocks

### Creating

```
createBlock(parent_uuid, title)
```

`parent_uuid` may be a **page** UUID (top-level block) or a **block** UUID
(nested child). The underlying field is called `page-id` but behaves as a
parent pointer. It will not resolve a page title — passing one returns success
and creates nothing.

Only `title` can be set. Tags, order, and position are rejected at creation and
must be follow-up calls. The new UUID is assigned by Logseq and **not
returned**; read it back if you need it.

```
createManyBlocks([{parent_uuid, title}, ...])
```

One batched call. Each item may target a different parent. Titles must be
unique **among siblings** — two blocks may share a title under different
parents, but not under the same one, because verification finds a new block
among its parent's children.

Whether a batch applies atomically is untested. On failure, read back rather
than assuming all-or-nothing.

### Outlines

```
createPageofBlocks(page_uuid, outline)
```

Indented text in, tree out. Costs 2d−1 calls for depth *d*, independent of
width: create a level, read back the UUIDs Logseq assigned, create the next.

The read-back between levels is structural, not defensive. Creation returns no
UUIDs and no field resolves a title, so children have no way to name their
parents until the level above exists.

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

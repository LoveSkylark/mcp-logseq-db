# Logseq API surface

What the plugin API offers, and why this server reaches so little of it.

Twenty-three methods are in the client allowlist. The rest are unreachable,
and each falls into one of a few categories — none of which is "we ran out of
time".

## Reachable

Every entry is used by at least one tool. A method no tool needs is not in the
allowlist, so the list doubles as a dependency inventory — which is why
`getCurrentGraph` was dropped: it sat here reading as a dependency while
nothing called it.

| Method | Used by |
| --- | --- |
| `datascriptQuery` | every read, every list, every verification |
| `getBlock` | `getBlock` |
| `getTagsByName` | `getTagUUID` |
| `getAllTags` | `listTags` |
| `getAllProperties` | `listProperties` |
| `createPage` | `createPage` |
| `insertBlock` | `createBlock` |
| `insertBatchBlock` | `createPageofBlocks`, `importPage` |
| `moveBlock` | `moveBlock` |
| `updateBlock` | `updateBlock`, `repairLinks` |
| `removeBlock` | `removeBlock`, `clearPage` |
| `renamePage` | `renamePage` |
| `createTag` | `creatTag` |
| `deletePage` | `deleteTag`, `deletePage` |
| `addBlockTag` / `removeBlockTag` | `addTag` / `removeTag` |
| `upsertProperty` / `removeProperty` | `createProperty` / `deleteProperty` |
| `upsertBlockProperty` / `removeBlockProperty` | `addProperty` / `removeProperty` |
| `getAppInfo`, `checkCurrentIsDbGraph` | `capabilities` |

## Not reachable, and why

**Superseded by a query.** `listPages`, `listTags`, `listProperties`,
`getPageData`, `getTagObjects`, `getProperty`, `getTag` all work, but return
everything unfiltered or in a shape that needs post-processing anyway.
`datascriptQuery` selects fields and filters by class in one call, so the
dedicated methods bought nothing.

**Blocked upstream.** `getFavorites` returns HTTP 500. `setPropertyNodeTags`
times out. `onChanged` and `onBlockChanged` are event callbacks and cannot be
carried over request/response HTTP at all.

**Deliberately withheld.** `q` and `customQuery` return result shapes too
limited to build a safe contract on. `exportEdn` returns the whole graph
unbounded; `importEdn` replaces it. `setFileContent` writes raw files, which
sidesteps every guarantee this server makes.

**`upsertNodes` is out of the allowlist.** It fails on SYNCED graphs — "The
Imported EDN has N validation error(s)" for a write its own dry run accepts —
while `createPage`, `insertBlock`, `insertBatchBlock`, `updateBlock` and
`createTag` all succeed against the same graph. Local graphs are unaffected,
which is why it went unnoticed for so long. Block creation had already moved
off it for a second reason: it writes its single `page-id` into both
`:block/parent` and `:block/page`, so a block parent produced a child whose
owning page was the parent block. It is listed here rather than deleted because
an earlier design treated it as the general mutation primitive, and someone
reading that design needs to find out why it is gone.

**Untested, so unexposed.** `prependBlockInPage`, `addPropertyValueChoices`,
`newBlockUUID`.

`insertBlock`, `insertBatchBlock` and `moveBlock` were once in this section —
all three now back the tool surface. `insertBlock` in particular was removed
from the allowlist on the strength of the wrong capability list, which is how
nested block creation ended up broken: `upsertNodes` writes its single
`page-id` into both `:block/parent` and `:block/page`, while `insertBlock`
sets them independently.

**No tool needs them.** `setBlockIcon`, `removeBlockIcon`, `addTagProperty`,
`removeTagProperty`, `addTagExtends`, `removeTagExtends`. All verified working
at some point; none has a tool. Tag inheritance and tag-level property
declaration are the notable gaps — both have working routes and no way to call
them.

## Content is parsed on write

Not a method list, but the most consequential thing learned about this API and
the reason `importPage` exists.

Block content is interpreted when written, by `insertBlock`, `insertBatchBlock`
and `updateBlock` alike:

| Written | Stored as | Side effect |
| --- | --- | --- |
| `## X` | `X` | `:logseq.property/heading 2` set |
| `[[X]]` | `[[uuid]]` | **a page is created** if X does not exist |
| `#X` | `#[[uuid]]` | **a tag is created**, and the block is tagged |
| `**bold**`, `—` | unchanged | none |
| `{{query …}}` | unchanged | none |

The heading conversion is why `importPage` passes markdown through rather than
stripping markers. The other two are why it escapes references instead.

Note the tag ident from an inline `#X` carries a random suffix
(`:user.class/tag-rymz5vkR`) while `createTag` produces a deterministic
`:plugin.class.<caller>/X`. Both are true; they are different creation paths.

`updateBlock` parses identically, which is what makes `repairLinks` possible —
and it does NOT guard against a page UUID, so a page's title can be rewritten
through it. The tool layer refuses that; the raw method does not.

## Responses do not name queryable attributes

The most expensive lesson in this project, because it looks like the opposite
of a problem: a response shows you an attribute, you query it, nothing
matches.

Two transformations happen on the way out.

**Namespaces are stripped.** `:block/` and `:db/` prefixes are dropped when
attribute names are serialised. A raw datom dump of `:logseq.property/status`
returns nineteen attributes, of which `tags`, `ident`, `uuid`, `title`,
`name`, `order`, `created-at` and `tx-id` are all bare. They are really
`:block/tags`, `:db/ident`, `:block/uuid` and so on. Prefixes that are neither
`:block/` nor `:db/` survive intact, which is why `:logseq.property/type` sits
alongside bare `title` in the same result.

**Some fields are synthesised.** `getAllProperties` reports
`:property/closed-values` on `Status` with a list of six entity ids. **No such
datom exists.** It is derived from the reverse of
`:block/closed-value-property`, which lives on each value pointing back at its
property. A query for `:property/closed-values` matches nothing, on any graph.

Both caught me on the same tool. `listClosedValues` was written against the
synthesised name, then changed to the stripped name, then finally to
`:block/closed-value-property` — three attempts, each reading a response as if
it described the schema.

The same stripping caused a second bug elsewhere. `pageStats` filters
node-structure attributes out of its property-value count by checking for a
`block/` prefix; since those arrive bare, the filter never fired and a page
with no property values reported 29 — one per block's `page`, plus one per
top-level block's `parent`.

**So: confirm against a raw datom dump before building a query on an attribute
name.**

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find ?a ?v :where [?e :db/ident :logseq.property/status] [?e ?a ?v]]"]}
```

That shows what is actually stored. Anything visible only through
`getAllProperties`, `getAllTags` or a pull spec may be a view rather than a
fact. And note the filtering corollary: a bare ident is usually structure, but
`tags` and `alias` are bare AND are real user-settable properties, so "has no
namespace" is not a safe test for "is not a property".

## A caution about lists like this

An earlier version of this file recorded `getBlock`, `removeBlock`, and
`updateBlock` as rejected. All three work. The claim came from a probe that
passed wrong arguments, read the silent no-op as unavailability, and wrote the
result into a constant that nothing rechecked. Block deletion was routed
through a subprocess CLI for months because of it.

So: **treat every "unavailable" here as untested rather than settled.** The
`supported` entries have been exercised; the negative ones are much weaker
claims. `scripts/live_reliability.py` re-checks the load-bearing ones on every
run, and anything not covered there is worth probing directly before you build
a workaround around it.

## Namespaces

`logseq.DB.*` is the DB-graph namespace and the only one this server touches.

`logseq.Editor.*` mirrors much of it for file graphs and carries UI-coupled
operations — cursor position, selection, editing mode. `logseq.App.*`,
`logseq.UI.*`, `logseq.Assets.*`, `logseq.Git.*`, and `logseq.Commands.*` are
application state, chrome, and registration hooks. None describes a DB
mutation, so none belongs behind a tool that claims to verify one.

The client rejects any method outside `logseq.DB.*` before it reaches the
network.

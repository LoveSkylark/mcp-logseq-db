# Logseq API surface

What the plugin API offers, and why this server reaches so little of it.

Nineteen methods are in the client allowlist. The rest are unreachable, and
each falls into one of a few categories — none of which is "we ran out of
time".

## Reachable

Every entry is used by at least one tool. A method no tool needs is not in the
allowlist, so the list doubles as a dependency inventory.

| Method | Used by |
| --- | --- |
| `datascriptQuery` | every read, every list, every verification |
| `getBlock` | `getBlock` |
| `getTagsByName` | `getTagUUID` |
| `getAllTags` | `listTags` |
| `getAllProperties` | `listProperties` |
| `upsertNodes` | `createPage` |
| `insertBlock` | `createBlock` |
| `insertBatchBlock` | `createPageofBlocks`, `importPage` |
| `moveBlock` | `moveBlock` |
| `updateBlock` | `updateBlock`, `repairLinks` |
| `removeBlock` | `removeBlock`, `clearPage` |
| `renamePage` | `renamePage` |
| `createTag` | `creatTag` |
| `deletePage` | `deleteTag` |
| `addBlockTag` / `removeBlockTag` | `addTag` / `removeTag` |
| `upsertProperty` / `removeProperty` | `createProperty` / `deleteProperty` |
| `upsertBlockProperty` / `removeBlockProperty` | `addProperty` / `removeProperty` |
| `getAppInfo`, `checkCurrentIsDbGraph`, `getCurrentGraph` | `capabilities` |

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

**Untested, so unexposed.** `prependBlockInPage`, `addPropertyValueChoices`,
`newBlockUUID`.

`insertBlock`, `insertBatchBlock` and `moveBlock` were once in this section —
all three now back the tool surface. `insertBlock` in particular was removed
from the allowlist on the strength of the wrong capability list, which is how
nested block creation ended up broken: `upsertNodes` writes its single
`page-id` into both `:block/parent` and `:block/page`, while `insertBlock`
sets them independently.

**No tool needs them.** `setBlockIcon`, `removeBlockIcon`, `addTagProperty`,
`removeTagProperty`, `addTagExtends`, `removeTagExtends`, `renamePage`. All
verified working at some point; none has a tool. `renamePage` in particular is
a gap rather than a decision — page rename has a working route and no way to
call it.

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

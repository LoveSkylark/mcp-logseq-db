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
| `upsertNodes` | `createBlock`, `createManyBlocks`, `createPageofBlocks` |
| `updateBlock` | `updateBlock` |
| `removeBlock` | `removeBlock` |
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

**Untested, so unexposed.** `insertBatchBlock`, `prependBlockInPage`,
`addPropertyValueChoices`, `newBlockUUID`. These may work. `insertBatchBlock`
and `prependBlockInPage` are the two most worth probing, since between them
they might give block movement a route — the one operation with no route at
all.

**No tool needs them.** `setBlockIcon`, `removeBlockIcon`, `addTagProperty`,
`removeTagProperty`, `addTagExtends`, `removeTagExtends`, `renamePage`. All
verified working at some point; none has a tool. `renamePage` in particular is
a gap rather than a decision — page rename has a working route and no way to
call it.

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

# Live test specification

A reusable plan for exercising `mcp-logseq-db` against a real Logseq DB graph.
Tests only — record outcomes in a separate run log rather than editing results
into this file.

This is the counterpart to `pytest`. The unit suite checks the code does what
we intended; it cannot check whether our model of Logseq is still correct,
because the fakes encode the same beliefs the code does. Everything below asks
the second question.

---

## Protocol

1. Run `capabilities` first. Record the graph version, whether
   `version_matches` is true, and any tool reported `unavailable` or
   `unknown`. **`unknown` means the probe was inconclusive, not that the tool
   is missing** — test it anyway and record what happened.
2. Before any write, state the target UUID or ident, the operation, and how it
   is reversed.
3. Create one fixture page per run: `MCP T <date> <run>`. Never test against
   real content.
4. Verify every write with an independent read. **An envelope reporting
   `verified: true` is necessary but not sufficient** — the server's read-back
   and your verification can share a wrong assumption.
5. **Confirm a symptom before treating something as damage.** A count is not
   a symptom. If a metric is computed from the same attribute you suspect is
   wrong, it cannot be evidence about that attribute — open the page in the
   Logseq UI and look. A repair tool was once built and thousands of blocks
   moved on a premise nobody checked this way.
6. **On any error, do not retry.** A failed response does not imply a failed
   write, and a successful one does not imply a completed write. Record
   whether the mutation committed.
7. Prefer attribute patterns and `#uuid` literals in verification queries.
   Predicate functions are used by the server in one place (`listAssets`) but
   have hung the DB worker before — see T-706.
8. Destructive steps require explicit confirmation: `deleteTag`,
   `deleteProperty`, `deletePage`, `clearPage`, and `removeBlock` on anything
   with children.
9. Use `pageStats` for triage rather than `inspectPage` or `findOrphans`. Its
   response is a fixed size; theirs scale with page content, and a container
   page can exhaust the context in one call. Across MANY pages, use
   `listPages(with_counts=true)` or `listJournals(with_counts=true)` instead
   of a `pageStats` per page — four queries either way.
10. **Pass `verbose: false` on any write whose payload you do not need.** The
    default is `true` for compatibility, and the envelope carries the target
    entity twice. Keep `verbose: true` on `clearPage`, `removeBlock`,
    `deletePage` and `updateBlock`, where the payload is the only record of
    what was destroyed or of what Logseq did to what you sent. Record which
    mode each test used — shaping must not change `verified`.
11. **Before recycling anything, check `is_alias_of` and `aliases` in
    `pageStats`.** An alias relation appears in no count, and it is the one
    relation these tools cannot rebuild.
12. Tear down fixtures at the end of the run.

### Standard verification query

```json
{"method": "logseq.DB.datascriptQuery", "args": ["[:find ?a ?v :where [?e :block/uuid #uuid \"UUID\"] [?e ?a ?v]]"]}
```

### Verdicts

| Verdict | Meaning |
|---|---|
| PASS | Behaved as expected, verified independently |
| FAIL | Did not behave as expected |
| FALSE-ERROR | Tool reported an error but the mutation committed |
| SILENT-FAIL | Tool reported success but nothing committed |
| CAUGHT | Tool reported `verified: false` and nothing committed — the intended behaviour on failure |
| BLOCKED | Could not run (unavailable, or a prerequisite failed) |

`SILENT-FAIL` and `CAUGHT` are the two outcomes that matter most. The whole
verification layer exists to convert the first into the second.

### Run log format

```
run: <date> | logseq <version> | version_matches <true|false>
ID | verdict | observed | notes
```

---

## Suite 0 — Regressions

Run first. Each was broken and fixed; a failure here means the fix did not hold
or the build changed underneath it.

| ID | Test | Expected |
|---|---|---|
| T-001 | `createBlock` on a page | Succeeds. Block creation now routes through `insertBlock`, not `upsertNodes` — the latter wrote its single `page-id` into both `:block/parent` and `:block/page`. |
| T-002 | `createBlock` with a **block** parent | `:block/parent` is the block, `:block/page` is the **page**. These are separate facts and the old route conflated them. |
| T-003 | `inspectPage` with `detail=properties` | Returns rows. Previously 500'd: `(pull ?value ...)` received scalars such as `:block/order` strings. |
| T-004 | `inspectPage` with `detail=all` | Returns; recovers with T-003. |
| T-005 | `getPageUUID` with the lowercase form of a mixed-case title | Resolves via the normalized name fallback. |
| T-006 | `getPageUUID` for a title also used by a tag | Resolves to the page. Tags carry `:block/name` too; the Page-class filter separates them. |
| T-007 | `getBlockUUID` on a page with nested blocks | Every block at any depth, and none duplicated. Reads walk `:block/parent`, and the raw `_parent` key is stripped once the tree is built. |
| T-008 | `findOrphans` on a page containing a **nested page** | `orphans: []`, `nested_pages` populated, diagnostic says "No damage". A nested page is a page boundary — flagging its blocks invites repair of correct structure. |
| T-009 | `deleteProperty` on a property with a `checkbox` or `datetime` value | Succeeds. The usage query pulled the value, which 500'd on inline literals and left such properties undeletable. |
| T-010 | `clearPage` on a page holding property values | Content blocks gone, property-value blocks preserved and counted in the diagnostic. |
| T-011 | `capabilities` tool list | Includes `getBlockTree`, `isTitleAvailable` and `retitleOverDuplicate`. `getBlockTree` was missing from the route map entirely, so it never appeared. |
| T-012 | `capabilities` constraints for `inspectPage` | Present. They were keyed under the old name `getPage` and so were never surfaced. |
| T-013 | Any property tool with an ident containing a space or a quote | Rejected at the boundary. An ident reaches Datascript query TEXT, which an attribute position cannot parameterise, so the shape is checked rather than escaped. |
| T-014 | `listOrphanProperties` | Returns. Built-ins with no namespace are skipped rather than interpolated into a query. |
| T-015 | `moveBlock` with `placement=last-child` | Available and appends — see the Moving section. |
| T-016 | Any write with `verbose: false` | `verified` is identical to the same write with `verbose: true`; only the payload differs. Shaping must never change the verdict. |
| T-017 | `insertBlock` with a page NAME as the parent, called DIRECTLY (not through `createBlock`) | A block is created at the top of that page. Recorded, not a fault — the old belief that names are inert came from `upsertNodes`, which nothing uses now. It matters because `createBlock`'s UUID validation is then the only thing preventing a mistyped argument from landing a real write. See T-704. |

---

## Suite 1 — Pages

| ID | Test | Expected |
|---|---|---|
| T-101 | `createPage` | Page returned with a UUID; readable by it; carries `:logseq.class/Page` |
| T-102 | `createPage` with an existing title | Rejected **before** writing, not duplicated |
| T-103 | `renamePage` | Title changes, **UUID stable**, `:block/name` updated, still a page |
| T-104 | `renamePage` onto an existing title | Rejected |
| T-105 | `deletePage` with no inbound refs and no alias relation | Recycled: `:logseq.property/deleted-at` set, UUID and tags retained |
| T-106 | `deletePage` **with** inbound refs, no acknowledgement | Refused, referring entities listed |
| T-107 | Same with `acknowledge_reference_rewrite: true` | Proceeds; **confirm the inbound refs still point at it** |
| T-108 | `listPages` after T-105 | Recycled page absent |
| T-109 | `listRecycled` after T-105 | Recycled page present |
| T-110 | `clearPage` on a page with nested blocks | All content blocks gone; page, tags and property values intact |
| T-111 | `inspectPage` at each `detail` value | `page`, `blocks`, `tags`, `properties`, `declared`, `all` each return their own shape |
| T-112 | `pageStats` on a leaf page | Counts only, no block payload. Compare `own_blocks` against `getBlockUUID` length. |
| T-113 | `pageStats` on a container page with sub-pages | `nested_pages > 0`, `true_orphans: 0`, `own_blocks` counts only the container's own. Response stays small. |

### Titles, availability and the recycle asymmetry

`getPageUUID` is recycle-BLIND and the write path is recycle-AWARE, both
deliberately. This is the sequence that cost a repair: a page recycled on the
belief its title would be released, found mid-repair to be still holding it.

| ID | Test | Expected |
|---|---|---|
| T-114 | `isTitleAvailable` on an unused title | `available: true`, `held_by: []` |
| T-115 | `isTitleAvailable` on a live page's title | `available: false`; one holder, `kind: page`, `recycled: false` |
| T-116 | Recycle a fixture page, then `isTitleAvailable` on its title | `available: false`, `recycled: true`. **Then `getPageUUID` on the same title: `found: false`.** Both are correct. Record both in the log — this pair is the whole point of the tool. |
| T-117 | `createPage` with that recycled title | Refused, agreeing with T-116 rather than with `getPageUUID` |
| T-118 | `isTitleAvailable` on a title held by a plain BLOCK | `available: false`, `kind: block`. Surprising and correct: `createPage` refuses it. |
| T-119 | `isTitleAvailable` on a title held by a tag | `kind: tag` |

### Taking a title back

| ID | Test | Expected |
|---|---|---|
| T-120 | `retitleOverDuplicate` onto a title held by an EMPTY page | Two renames, `verified: true`, `parked` names the holder's new title. **Confirm with an independent read that no block was edited and that inbound refs on both pages are unchanged.** |
| T-121 | Same, where the holder is RECYCLED | Succeeds. Renaming a recycled page is what releases its title — the fact the whole tool rests on. Record it explicitly. |
| T-122 | `retitleOverDuplicate` onto a title held by a page WITH content | Refused, both reference counts reported, nothing renamed |
| T-123 | `retitleOverDuplicate` onto a title held by an ALIAS page | Refused with the alias diagnostic. Construct this deliberately: it is the near-miss that nearly recycled a working alias. |
| T-124 | `retitleOverDuplicate` onto a free title | Plain rename, `parked: null` |
| T-125 | `retitleOverDuplicate` where the parking title is itself taken | Refused **before** any write; confirm the holder still has its original title |

### Alias visibility

An alias relation shows up in no count. `alias` is a built-in property outside
the writable namespace, so anything broken here cannot be repaired with these
tools — which makes this the most consequential suite in the spec.

| ID | Test | Expected |
|---|---|---|
| T-126 | `pageStats` on a page that is an alias of another | `is_alias_of` carries the owner's UUID; diagnostic says "NOT a dead stub" |
| T-127 | `pageStats` on the page that declares it | `aliases` lists the alias page's UUID |
| T-128 | Record which attribute this graph uses | Dump every attribute of the declaring page and note whether the alias is under `:logseq.property/alias` or `:block/alias`. Both are queried through an or-join. **Record which one holds data — it has differed between builds, and the or-join is what makes the guard version-independent.** Finding either form does NOT mean narrowing the query to it: a single-attribute guard silently never fires on a graph using the other. `getProperyUsers` answers this in two calls, one per ident, without touching a page. |
| T-129 | `deletePage` on an alias page, no acknowledgement | Refused, alias-related entities listed |
| T-130 | `deletePage` on the page that DECLARES aliases, no acknowledgement | Also refused — the guard runs in both directions |
| T-131 | Same with `acknowledge_alias_loss: true` | Proceeds; diagnostic states how many relations were broken. **Then check in the Logseq UI whether the alias still resolves.** Needs a human. |
| T-132 | `deletePage` on a page with no alias relation | Proceeds without the flag — the guard must not tax every delete |

### Counted listings

| ID | Test | Expected |
|---|---|---|
| T-133 | `listJournals` with no arguments | A bare list, unchanged shape, no counts |
| T-134 | `listJournals(with_counts=true)` | Envelope with `total`, `counted`, `truncated`; each entry carries `own_blocks`, `content_blocks`, `refs`. Newest first. |
| T-135 | Compare T-134 against `pageStats` on three of those journals | The counts agree exactly. They query the same attributes; a disagreement means one of them is wrong. |
| T-136 | A journal with NO blocks in T-134 | `own_blocks: 0` rather than a missing key. A page absent from the join must read as zero. |
| T-137 | `listPages(with_counts=true)` on a graph over 500 pages | `truncated: true`, `counted: 500`, diagnostic names the way to get the rest |
| T-138 | `listPages(with_counts=true, limit=10)` | Ten rows, `truncated: true`, `total` is the real total |

---

## Suite 2 — Blocks

| ID | Test | Expected |
|---|---|---|
| T-201 | `createBlock` with a page parent | Top-level; `:block/parent` and `:block/page` both the page |
| T-202 | `createBlock` with a **block** parent | Nested; parent is the block, page is the page |
| T-203 | Depth 3 via T-202 twice | `:block/parent` chains; `:block/page` unchanged at every level |
| T-204 | `updateBlock` | Title changes; UUID stable; `previous_entities` carries the **old title** |
| T-205 | `removeBlock` on a childless block | Gone; verified absent |
| T-206 | `removeBlock` on a block with descendants | Whole subtree gone; no orphan with a dangling `:block/parent` |
| T-207 | `getBlockUUID` on an empty page | `[]`, not an error |
| T-208 | `getBlock` on a deleted UUID | `found: false`, distinguishable from a transport failure |
| T-209 | `getBlockTree` with `max_nodes: 1` | `truncated: true`, accurate `node_count` |
| T-210 | `getBlockTree` with `max_depth: 1` | Root plus one generation — depth counts generations **below** the root |
| T-211 | `getBlockTree` payload | Each node carries `children` only. The raw `_parent` key must not also be present — it duplicated whole subtrees. |
| T-212 | `createPageofBlocks`, 3 levels | Tree correct. `calls` equals **one per parent that has children** — the read-back cycle is gone. |
| T-213 | `createPageofBlocks` with a repeated title in different branches | Both created under the right parents |
| T-214 | `createPageofBlocks` where one level fails | **Batches are not atomic.** Earlier levels stay committed. Confirm the result names the failing level and does not imply a clean rollback. |
| T-215 | `findOrphans` after T-214 | Reports what was left, so the partial write is auditable |

### Searching

The riskiest suite in this spec: `searchBlocks` is the only tool that runs a
predicate in front of every title in the graph. **If a trivial read times out
after any of these, stop, restart Logseq, and record which search provoked
it** — that finding is worth more than the rest of the suite.

| ID | Test | Expected |
|---|---|---|
| T-230 | `searchBlocks` for a string you know exists in one block | One match, with its uuid and page. Confirm the page is right with `getBlock`. |
| T-231 | `searchBlocks` for a string that exists nowhere | `matches: 0`, `truncated: false`, diagnostic calls it a definite zero |
| T-232 | The same string with different capitalisation | `matches: 0`. Matching is case-sensitive by design. |
| T-233 | A common word such as "the" | Either a count with rows, or a count with `results: []` and `truncated: true` above the ceiling. **Record the count and the elapsed time** — this is the worst case for the predicate. |
| T-234 | The same search with `page_uuid` set | Far fewer matches, and noticeably faster: scoping binds the page before the predicate runs. |
| T-235 | A string containing a quote, e.g. `"Session Zero"` | Matches, or a clean zero. **It must not fail with `FST_ERR_CTP_INVALID_JSON_BODY`** — the needle is escaped, and this is the test for that. |
| T-236 | A non-Latin string | Matches. Records whether EDN `\uXXXX` escapes resolve on this build. |
| T-237 | A string that appears in a PAGE title | Matched, with `kind: page` and `page: null`. Pages carry `:block/title` too. |
| T-238 | `regex` alongside `text` | Refines the substring rows; `matches` still reports the substring count and `returned` the smaller number |
| T-239 | `limit` below the match count | `truncated: true`, and the diagnostic says how many were withheld |
| T-240 | A one-character search | Refused before any query |
| T-241 | End-to-end: find a real typo, then `updateBlock` it from the returned uuid | Fixed in two calls, with no page re-read. This is the workflow the tool exists for. |

### Moving

`moveBlock` is **confirmed working on every placement**, including across
pages. An earlier version of this spec said its route had never been observed
changing anything; that is no longer true, and a `verified: false` here is now
a finding rather than the expected result.

It still returns null whether it moved the block or did nothing, so the tool
establishes the outcome by reading back. A genuine no-op — moving a block to
the position it already occupies — comes back `verified: false` with a
silent-no-op diagnostic. **That is the tool working correctly**; record it as
CAUGHT, not FAIL.

| ID | Test | Expected |
|---|---|---|
| T-216 | `moveBlock` to another block on the same page, `placement=child` | Parent changes, page unchanged |
| T-217 | `moveBlock` to a block on a **different page** | Parent changes **and** `:block/page` follows. A page that does not follow leaves an invisible child. |
| T-218 | `moveBlock` on a block with descendants | Descendants' `:block/page` follows too |
| T-219 | `moveBlock` with a page target, `placement=child` | Moves to the page's top level |
| T-220 | `moveBlock` with a page target, `placement=after` | Refused — a page has no siblings |
| T-221 | `moveBlock` into the block's own subtree | Refused before the call |
| T-222 | `moveBlock` to the parent it already has | `verified: false`, silent-no-op diagnostic. CAUGHT. |

#### Append vs prepend

`placement=child` PREPENDS. That is Logseq's own behaviour and is left alone,
which means moving a sequence with it reverses the sequence — silently, and
only visible by reading the destination back. Three pages of content were
reversed this way before anyone looked.

| ID | Test | Expected |
|---|---|---|
| T-223 | Two blocks under a parent, move a third with `placement=child` | The arrival is FIRST. Confirm `child` still prepends: if it ever starts appending, `last-child` is redundant and the docs are wrong. |
| T-224 | Same with `placement=last-child` | The arrival is LAST |
| T-225 | **Acceptance.** Move three blocks in source order to an empty page with `last-child` | They arrive in source order. Verify by reading `:block/order` on all three, not by trusting the envelopes. |
| T-226 | The same three with `placement=child` | They arrive REVERSED. Record it — this is the footgun the placement exists to remove, and it should still be reproducible. |
| T-227 | `last-child` into a parent with no children | Succeeds; falls through to a plain child call, where prepend and append coincide |
| T-228 | `last-child` on a block that is already the last child | `verified: true` with "no move was needed", and **no `moveBlock` call is issued** — the state was read, not written. Distinguish this from T-222. |
| T-229 | `last-child` where the block lands under the right parent but not last | `verified: false`, diagnostic gives its position. Hard to provoke deliberately; if it happens, it is a real finding about the route. |

#### Bulk moving

`moveBlocks` is the bulk write for a flat run of siblings. It is **not
atomic** and it **stops** at the first block that does not verify, so what
these tests ask is whether a partial result is legible — not just whether the
happy path works.

Build a fixture page with ten numbered sibling blocks (`Para 01` … `Para 10`)
and a second empty destination page. Verify every outcome by reading
`:block/order` at the destination, never from the envelopes.

| ID | Test | Expected |
|---|---|---|
| T-242 | **Acceptance.** Move all ten to the empty destination, `placement=last-child` | All ten arrive in source order. `summary` reads `requested: 10, attempted: 10, landed: 10, failed: 0`, and `order_preserved: true`. |
| T-243 | Count the API calls T-242 made | Roughly two per block plus a fixed handful. If it is nearer eight per block the hoisting has regressed and a large chapter will time out. |
| T-244 | Move five more into the same destination with `last-child` | They append AFTER the ten already there; the first ten keep their order |
| T-245 | The same five with `placement=child` | The run lands at the TOP of the destination, still internally in order |
| T-246 | Move a run to a block target rather than a page | Every block's `:block/parent` is the target and `:block/page` is the target's page |
| T-247 | Move a run where one block has descendants | Descendants follow; `stranded_descendants` is empty. A stranded descendant is a real child no page-scoped query can see. |
| T-248 | Pass the same UUID twice | Refused before any write |
| T-249 | Pass a parent and one of its own children in the same list | Refused before any write. A move carries the subtree, so the second move would pull the child back out. |
| T-250 | Pass a target that sits inside one of the blocks' subtrees | Refused before any write |
| T-251 | Pass 55 blocks | `attempted: 50`, `not_attempted: 5`, `verified: false`, and the five come back **in order**. Then pass those five in a second call with `last-child` and confirm they append correctly — that is the paging contract. |
| T-252 | `all_or_nothing: true` on a run you can make fail part-way | `rolled_back` lists the landed blocks, and the diagnostic says **POSITION WAS NOT RESTORED**. Confirm in the UI that they are back under the original parent but grouped at its top. Hard to provoke; if you cannot, record it as unconstructable rather than as a pass. |

### Importing

| ID | Test | Expected |
|---|---|---|
| T-260 | `importPage` with a markdown STRING, nested | Tree correct, `blocks` matches, references escaped to `{{link:X}}` |
| T-261 | **Acceptance.** `importPage` with a LIST whose second element is an eight-line numbered sequence containing a blank line and an INDENTED continuation line (`   3a. …`), and **no** line beginning with `- ` | ONE block. Read it back and confirm byte-exact: the newlines, the blank line and the leading whitespace are all content. This is the contract the docs claim, and the case the first run never actually tested — its fixture mixed the safe shape with the destructive one below. |
| T-261b | The same list, but with a line beginning with `- ` inside the multi-line element | **REFUSED** before any write, with a diagnostic naming the offending line. Logseq truncates a block at such a line and discards the rest — measured 2026-09-20, where eight lines sent stored two with `verified: true`. Confirm nothing was written. |
| T-261c | `createBlock` with a multi-line title containing a `- ` line | Also refused. The guard is shared, because this path had none and fails identically. |
| T-262 | The T-261 text as a markdown STRING | ONE block, but **rewritten**: the blank line is dropped and the indentation stripped. It does not fragment — that earlier expectation was wrong, and depended on the `- ` line that T-261 no longer contains. |
| T-263 | A list with explicit depths `0, 1, 2, 0` | Two roots, one child, one grandchild. Confirm by reading `:block/parent`, not from the count. |
| T-264 | A list element with `depth: 2` following a `depth: 0` element | Refused. A skipped level is an error here, unlike the string form which flattens and warns. |
| T-265 | A list containing a fenced code block with indented lines | Stored verbatim, indentation intact |
| T-266 | A list containing a markdown table | Stored as one block |
| T-267 | A list element containing `[[Link]]` and `#tag` | Escaped exactly as in the string form. **Confirm no page or tag was minted** — the escaping is not a property of the string parser. |
| T-268 | A list element that is `"alias:: X"` | Stored as block CONTENT. The list form has no page-property region, so `page_properties` must be empty. |
| T-269 | An empty or whitespace-only list element | Refused |
| T-270 | `dry_run: true` on a list | `verified: false`, correct `blocks`, and no `insertBatchBlock` call |

---

## Suite 3 — Tags

| ID | Test | Expected |
|---|---|---|
| T-301 | `creatTag` | Created. **Record the assigned ident verbatim.** It comes from Logseq rather than being derived from the title, so it is read back rather than constructed — note whether it carries a random suffix and under which namespace it landed. Two documents used to guess differently. |
| T-302 | `creatTag` with a title an existing page holds | Refused — tags and pages share one title space |
| T-303 | `addTag` to a block | `:block/tags` updated |
| T-304 | `addTag` to a page | Same tool, same result — the target is uniform |
| T-305 | `addTag` twice with the same tag | Record whether it is idempotent |
| T-306 | `removeTag` with two tags present | Only the named relation removed; the other survives |
| T-307 | `removeTag` from a page | Page keeps `:logseq.class/Page` and is still a page |
| T-308 | `getTagUsers` on a tag applied to a page and a block | Both returned; pages distinguishable by `:block/name` |
| T-309 | `deleteTag` on an unused tag | **Unverified route** — it goes through `deletePage`, never confirmed against a tag. Record which identifier the route accepted, or that it silently did nothing. A `verified: false` here is the expected outcome until proven otherwise. |
| T-310 | `deleteTag` on a tag in use, no acknowledgement | Refused, holders listed |
| T-311 | Same with `acknowledge_detach: true` | Proceeds; every `:block/tags` and `:block/refs` entry cleared |
| T-312 | `listOrphanTags` after T-306 | The now-unused tag appears |

---

## Suite 4 — Properties

The sandbox limits writes to `plugin.property.<caller>/*`. Tests expecting a
refusal are testing the guard, not looking for a workaround.

| ID | Test | Expected |
|---|---|---|
| T-401 | `createProperty` with type `default` | Created; **record the assigned ident** |
| T-402 | `createProperty` with a namespaced title | Rejected — Logseq treats it as a page name and refuses the `/` |
| T-403 | `createProperty` for `number`, `checkbox`, `url`, `datetime`, `node` | Each accepted, and the **stored type matches the requested type**. A mismatch now fails rather than passing. |
| T-404 | Value storage audit | Set a value of each type; dump every attribute of the target and of the value entity. **Record which attribute each type writes.** |
| T-405 | `addProperty` on a page | Set, and the **stored value matches** what was requested |
| T-406 | `addProperty` on a block | Same tool, same result |
| T-407 | `addProperty` with a `user.property/*` ident | Refused **before** the API call |
| T-408 | `addProperty` with a `:logseq.property/*` built-in | Refused the same way |
| T-409 | `addProperty` on a `node` property, passing a literal | Refused before the call. Logseq would mint a value entity named after the string and read back as success. |
| T-410 | `addProperty` twice with the same value on a cardinality-many property | Second write skipped, diagnostic says duplicate |
| T-411 | Two distinct values on a cardinality-many property | Both retained |
| T-412 | `removeProperty` (value) | Attribute, `:block/refs` entry, and value entity all cleared |
| T-413 | `deleteProperty` with values, no acknowledgement | Refused, holder count reported |
| T-414 | Same with `acknowledge_value_loss: true` | Definition gone, values gone, and **orphaned value blocks swept** — the diagnostic reports how many |
| T-415 | `getProperyUsers` on a property set on both a page and a block | Both returned, with raw and resolved values |
| T-416 | `getProperyUsers` on a `checkbox` property | Returns rows. The value is a literal, not a ref — pulling it used to 500. |

---

## Suite 5 — Declared properties and classes

| ID | Test | Expected |
|---|---|---|
| T-501 | `inspectPage detail=declared` on a page tagged `Task` | Status, Priority, Deadline, Scheduled listed as declared |
| T-502 | Same page, `detail=properties` | Only properties **with values**; declared-but-unset ones absent |
| T-503 | A page's own tags vs its blocks' tags | `inspectPage detail=tags` covers both; confirm each holder is identified |
| T-504 | `listClosedValues` | **Returns values — corrected.** This table previously said "expected empty on current builds, no closed-value relationship exists". That was checked live on 2026-09-20 and is wrong: `Status` carries six values (Backlog, Todo, Doing, In Review, Done, Canceled) and `Priority` four (Low, Medium, High, Urgent), exactly as `SKILL.md` claims. Three more enum properties also appear: `:logseq.property.repeat/recur-unit` (6), `:logseq.property.repeat/repeat-type` (3), `:logseq.property.pdf/hl-color` (5) and `:logseq.property.view/type` (3). The attribute is `:block/closed-value-property`, on each VALUE pointing back at its property — `:property/closed-values` is what `getAllProperties` reports and matches nothing. Confirm the counts on this graph and record any difference. |
| T-505 | `addProperty` on `:logseq.property/status` with one of T-504's value entities | Refused — `:logseq.property/*` is outside the sandbox. This is the guard working, not a closed-value failure. |
| T-506 | `createProperty` with a closed-value schema of your own | Record whether the schema is accepted and whether `listClosedValues` then reports it. If it is, closed-value ENFORCEMENT becomes testable for the first time: set an allowed value, then a disallowed one, and record both. |

---

## Suite 6 — References

| ID | Test | Expected |
|---|---|---|
| T-601 | Write `[[Target]]` into a block title via `createBlock`, where `Target` EXISTS | **This spec and the code disagree, and the answer matters.** This table has said links written through the API stay inert text with no `:block/refs` entry. `importPage` escapes every reference on the opposite premise — that Logseq mints a page or tag for anything it parses — and `updateBlock` verifies the title CHANGED rather than matching, because `[[X]]` is expected to come back as `[[uuid]]`. Record exactly what happens: whether `:block/refs` gained the target, and whether the stored title still reads `[[Target]]`. |
| T-602 | Same with a target that does NOT exist | Record whether a stub page is minted. If it is, the escaping in `importPage` is load-bearing and T-601's old expectation was wrong. If nothing is created and no ref appears, the escaping is unnecessary and `repairLinks` exists for nothing — either finding is worth the run. |
| T-603 | Edit a block containing `[[Target]]` in the Logseq UI | Record whether a user edit materializes the ref where an API write did not. Needs a human. |
| T-604 | Point a `node` property at a page, then `findBacklinks` on the page | Appears under `property_values`, **not** under `refs` |
| T-605 | `findBacklinks` on a page with refs, tag holders and property values | All three reported separately. The total exceeds Logseq's backlink panel, which counts only `refs`. |
| T-606 | `deletePage` on a referenced page, then `findBacklinks` | **References are not rewritten** — they still point at the recycled page |
| T-607 | `pageStats` reference counts vs `findBacklinks` lengths | The integers agree with the lists |

---

## Suite 7 — Failure handling

| ID | Test | Expected |
|---|---|---|
| T-701 | Any tool with a malformed UUID | Clean `validation` failure naming the argument; no mutation |
| T-702 | A tag tool given the tag's **ident** instead of its UUID | Rejected at the boundary, diagnosed as an ident |
| T-703 | A property tool given a **UUID** instead of an ident | Rejected; this is the silent no-op the guard exists for |
| T-704 | `createBlock` with a page **name** as the parent | Rejected at the boundary. **This guard is now load-bearing:** the underlying `insertBlock` RESOLVES a page title and creates a top-level block on that page (confirmed 2026-09-20). It used to write nothing, so a mistyped argument was harmless; it no longer is. Confirm the rejection happens before any API call, and that no block appeared on the named page. |
| T-705 | A normal call immediately after a failed one | Succeeds — no session poisoning |
| T-706 | A query with a `clojure.string/*` predicate, isolated session | Record whether it errors cleanly or wedges the worker. **Run last; may need a Logseq restart.** |
| T-707 | After any timeout: re-probe, re-read the target | Establish committed state; check `recovered_after_timeout` and whether `capabilities` reports `writes_disabled` |
| T-708 | Recovery from an open write circuit | Reads still work; writes refused until Logseq is restarted and the MCP reconnected |
| T-709 | Every write failing with "The Imported EDN has N validation error(s)" | **A graph-level fault, not a payload fault.** A dry run still succeeds. Re-index the graph or test on a fresh one; nothing in the request will fix it. |

---

## Suite 8 — Envelope consistency

Fill in from envelopes captured during earlier suites.

| ID | Test | Expected |
|---|---|---|
| T-801 | `deleteTag` envelope | `verified_state` null; `previous_state` carries the tag and its holders |
| T-802 | `deleteProperty` envelope | Compare with T-801; flag inconsistencies |
| T-803 | `removeBlock` envelope | `verified_entities` empty; `previous_entities` carries the whole subtree |
| T-804 | `deletePage` envelope | `previous_entities` carries page and blocks; `observed_entities` carries inbound refs |
| T-805 | `updateBlock` envelope | `previous_entities` carries the prior title — an edit is otherwise unrecoverable from its own result |
| T-806 | A cascade delete diagnostic | Does it cover descendants or only the target UUID? |
| T-807 | Any `verified: false` result | `previous_state` and `observed_state` both present, and distinguishable |
| T-808 | Any `dry_run: true` call | `verified: false`, empty `verified_entities`. A dry run is not a write. |
| T-809 | A partial batch failure (T-214) | The committed blocks appear in `verified_entities` and the diagnostic says batches are not atomic |
| T-810 | `deletePage` refused for an alias relation | `observed_entities` carries the alias-related pages, so the refusal is actionable without a second read |
| T-811 | `retitleOverDuplicate` after a second-rename failure | `PARTIALLY APPLIED`, the parked UUID, its original title, and an undo instruction. Rare; if it happens, capture the whole envelope. |

---

## Suite 9 — Response shaping and cost

The point of this suite is that `verbose` changes the PAYLOAD and nothing
else. If a verdict ever differs between the two modes, that is a serious
finding — shaping would be altering behaviour.

| ID | Test | Expected |
|---|---|---|
| T-901 | `moveBlock` on a block holding ~400 words, `verbose: true` | The block's text appears **twice** — once in `previous_entities`, once in `verified_entities`. Record the response size. |
| T-902 | The same move back, `verbose: false` | `verified`, `uuid`, `parent`, `page`, `order`, `diagnostic` and nothing proportional to content. Compare sizes with T-901. |
| T-903 | A failing write with `verbose: false` | Still `verified: false`, still the same diagnostic, and `observed` carries entity DIGESTS — a failure must stay actionable, since a write cannot safely be re-run to get detail. |
| T-904 | `creatTag` and `createProperty` with `verbose: false` | The assigned `ident` is still present. It is the only way to address the entity afterwards, so terse keeps it. |
| T-905 | `clearPage` with `verbose: true` on a page of real prose | `previous_entities` carries the full text. **This is the only surviving record of what was destroyed** — confirm it is complete enough to diff an import against. |
| T-906 | `clearPage` with `verbose: false` | Reduced to `previous_count`. Verify the count matches T-905's list length. |
| T-907 | `createPageofBlocks` with `verbose: false` | `created_count` plus one digest per block; the titles you sent are not echoed back |
| T-908 | `importPage` | No `verbose` flag, and none needed — its result is already counts and names. Confirm nothing proportional to the page appears. |

---

## Unconstructable with the current toolset

Record as such rather than leaving them perpetually BLOCKED.

**Ambiguous-title tests.** `createPage` refuses a title a tag holds, and
`creatTag` refuses a title a page holds, so an ambiguous title cannot be
created through the tools. `getPageUUID`, `getTagUUID` and `getPropertyIndent`
all have ambiguity guards that can only be exercised against pre-existing
damage.

The **recycled-title** case is the exception and IS constructable — see T-116.
Recycle a fixture page and the title is held by an entity that `getPageUUID`
will not resolve, which is exactly the state that misled a repair. Run it.

**Setting a declared built-in.** Every `Task`-declared property is a
`:logseq.property/*` built-in, outside the sandbox. There is no way to set one
and watch it move from `declared` to `properties`.

**Closed-value enforcement.** Values themselves DO exist — see the corrected
T-504 — but every property carrying them is a `:logseq.property/*` built-in,
outside the writable sandbox, so there is nothing writable to enforce against.
T-506 asks whether a caller-owned closed property can be created at all; if it
can, this entry is obsolete.

---

## No tool exists

Tag inheritance (`extends`), tag-level property declaration, and block icons.
All have working API methods and no tool. If any becomes one, this spec needs
a suite.

**Page aliases are readable but not writable.** `pageStats` reports
`is_alias_of` and `aliases`, and `deletePage` and `retitleOverDuplicate` both
refuse on an alias relation — but nothing here can create, move or restore
one, because `alias` is a built-in property outside the writable namespace.
That asymmetry is why the guards exist: breaking a relation is possible and
repairing it is not.

Assigning or freeing a UUID. `:block/uuid` cannot be written, and recycling
does not release an identity. This is what makes split-identity damage
unrepairable by reassignment — though a TITLE can now be taken back from an
empty duplicate without touching a UUID, which is what
`retitleOverDuplicate` does.

---

## Run order

Suite 0 first — a regression there makes everything after it uninterpretable.
Then Suite 1, since fixtures depend on page creation, and Suite 2, since most
later suites need blocks to target. Then 3, 4, 5, 6. Suites 8 and 9 are
assembled from envelopes already captured. Suite 7 last, and T-706 last of
all.

Within Suite 1, run the alias tests (T-126 to T-132) before anything that
deletes a page, and construct the alias fixture by hand in the Logseq UI —
there is no tool that can create one.

Within Suite 2, run Searching (T-230 to T-241) LAST of the three block
sections, after Moving and Importing. It is the only predicate query in the
surface and the most likely thing to wedge the worker, so a wedge there costs
you nothing that has not already run. Bulk moving (T-242 to T-252) needs the
ten-block fixture built first, and Importing (T-260 to T-270) is the cheapest
way to build it.

After the run, update `scripts/live_reliability.py` with any assumption that
turned out to be wrong. It now checks the ones these tools depend on,
including that `children: true` still prepends; if T-223 contradicts it, the
script is the thing to fix first. A finding recorded only in a run log gets
rediscovered the expensive way — several in this spec were rediscovered twice
before being written down.

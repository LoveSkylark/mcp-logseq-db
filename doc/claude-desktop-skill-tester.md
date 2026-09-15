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
9. Use `pageStats` for triage rather than `getPage` or `findOrphans`. Its
   response is a fixed size; theirs scale with page content, and a container
   page can exhaust the context in one call.
10. Tear down fixtures at the end of the run.

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
| T-003 | `getPage` with `detail=properties` | Returns rows. Previously 500'd: `(pull ?value ...)` received scalars such as `:block/order` strings. |
| T-004 | `getPage` with `detail=all` | Returns; recovers with T-003. |
| T-005 | `getPageUUID` with the lowercase form of a mixed-case title | Resolves via the normalized name fallback. |
| T-006 | `getPageUUID` for a title also used by a tag | Resolves to the page. Tags carry `:block/name` too; the Page-class filter separates them. |
| T-007 | `getBlockUUID` on a page with nested blocks | Every block at any depth, and none duplicated. Reads walk `:block/parent`, and the raw `_parent` key is stripped once the tree is built. |
| T-008 | `findOrphans` on a page containing a **nested page** | `orphans: []`, `nested_pages` populated, diagnostic says "No damage". A nested page is a page boundary — flagging its blocks invites repair of correct structure. |
| T-009 | `deleteProperty` on a property with a `checkbox` or `datetime` value | Succeeds. The usage query pulled the value, which 500'd on inline literals and left such properties undeletable. |
| T-010 | `clearPage` on a page holding property values | Content blocks gone, property-value blocks preserved and counted in the diagnostic. |

---

## Suite 1 — Pages

| ID | Test | Expected |
|---|---|---|
| T-101 | `createPage` | Page returned with a UUID; readable by it; carries `:logseq.class/Page` |
| T-102 | `createPage` with an existing title | Rejected **before** writing, not duplicated |
| T-103 | `renamePage` | Title changes, **UUID stable**, `:block/name` updated, still a page |
| T-104 | `renamePage` onto an existing title | Rejected |
| T-105 | `deletePage` with no inbound refs | Recycled: `:logseq.property/deleted-at` set, UUID and tags retained |
| T-106 | `deletePage` **with** inbound refs, no acknowledgement | Refused, referring entities listed |
| T-107 | Same with `acknowledge_reference_rewrite: true` | Proceeds; **confirm the inbound refs still point at it** |
| T-108 | `listPages` after T-105 | Recycled page absent |
| T-109 | `listRecycled` after T-105 | Recycled page present |
| T-110 | `clearPage` on a page with nested blocks | All content blocks gone; page, tags and property values intact |
| T-111 | `getPage` at each `detail` value | `page`, `blocks`, `tags`, `properties`, `declared`, `all` each return their own shape |
| T-112 | `pageStats` on a leaf page | Counts only, no block payload. Compare `own_blocks` against `getBlockUUID` length. |
| T-113 | `pageStats` on a container page with sub-pages | `nested_pages > 0`, `true_orphans: 0`, `own_blocks` counts only the container's own. Response stays small. |

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

### Moving

`moveBlock`'s underlying route has never been observed changing anything. The
tool verifies by reading back, so a no-op returns `verified: false` with a
diagnostic saying so. **Treat that as the tool working correctly.**

| ID | Test | Expected |
|---|---|---|
| T-216 | `moveBlock` to another block on the same page, `placement=child` | Parent changes, page unchanged. Or `verified: false` with "silent no-op". |
| T-217 | `moveBlock` to a block on a **different page** | Parent changes **and** `:block/page` follows. A page that does not follow leaves an invisible child. |
| T-218 | `moveBlock` on a block with descendants | Descendants' `:block/page` follows too |
| T-219 | `moveBlock` with a page target, `placement=child` | Moves to the page's top level |
| T-220 | `moveBlock` with a page target, `placement=after` | Refused — a page has no siblings |
| T-221 | `moveBlock` into the block's own subtree | Refused before the call |

---

## Suite 3 — Tags

| ID | Test | Expected |
|---|---|---|
| T-301 | `creatTag` | Created. The ident is **deterministic**: `:plugin.class.<caller>/<Title>`, spaces stripped. Record it and confirm no random suffix. |
| T-302 | `creatTag` with a title an existing page holds | Refused — tags and pages share one title space |
| T-303 | `addTag` to a block | `:block/tags` updated |
| T-304 | `addTag` to a page | Same tool, same result — the target is uniform |
| T-305 | `addTag` twice with the same tag | Record whether it is idempotent |
| T-306 | `removeTag` with two tags present | Only the named relation removed; the other survives |
| T-307 | `removeTag` from a page | Page keeps `:logseq.class/Page` and is still a page |
| T-308 | `getTagUsers` on a tag applied to a page and a block | Both returned; pages distinguishable by `:block/name` |
| T-309 | `deleteTag` on an unused tag | Succeeds. Record which identifier the route accepted. |
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
| T-501 | `getPage detail=declared` on a page tagged `Task` | Status, Priority, Deadline, Scheduled listed as declared |
| T-502 | Same page, `detail=properties` | Only properties **with values**; declared-but-unset ones absent |
| T-503 | A page's own tags vs its blocks' tags | `getPage detail=tags` covers both; confirm each holder is identified |
| T-504 | `listClosedValues` | **Expected empty on current builds.** No closed-value relationship exists: `Status` reports type `default` with a `:logseq.property/default-value` and no permitted set. Record if that changes. |

---

## Suite 6 — References

| ID | Test | Expected |
|---|---|---|
| T-601 | Write `[[Target]]` into a block title via `createBlock` | **No `:block/refs` entry.** Links written through the API stay inert text. |
| T-602 | Edit that block in the Logseq UI | Record whether a user edit materializes the ref. Needs a human. |
| T-603 | Point a `node` property at a page, then `findBacklinks` on the page | Appears under `property_values`, **not** under `refs` |
| T-604 | `findBacklinks` on a page with refs, tag holders and property values | All three reported separately. The total exceeds Logseq's backlink panel, which counts only `refs`. |
| T-605 | `deletePage` on a referenced page, then `findBacklinks` | **References are not rewritten** — they still point at the recycled page |
| T-606 | `pageStats` reference counts vs `findBacklinks` lengths | The integers agree with the lists |

---

## Suite 7 — Failure handling

| ID | Test | Expected |
|---|---|---|
| T-701 | Any tool with a malformed UUID | Clean `validation` failure naming the argument; no mutation |
| T-702 | A tag tool given the tag's **ident** instead of its UUID | Rejected at the boundary, diagnosed as an ident |
| T-703 | A property tool given a **UUID** instead of an ident | Rejected; this is the silent no-op the guard exists for |
| T-704 | `createBlock` with a page **name** as the parent | Rejected at the boundary |
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

---

## Unconstructable with the current toolset

Record as such rather than leaving them perpetually BLOCKED.

**Ambiguous-title tests.** `createPage` refuses a title a tag holds, and
`creatTag` refuses a title a page holds, so an ambiguous title cannot be
created through the tools. `getPageUUID`, `getTagUUID` and `getPropertyIndent`
all have ambiguity guards that can only be exercised against pre-existing
damage.

**Setting a declared built-in.** Every `Task`-declared property is a
`:logseq.property/*` built-in, outside the sandbox. There is no way to set one
and watch it move from `declared` to `properties`.

**Closed-value enforcement.** No closed-value relationship exists on current
builds, so "set an allowed value" and "reject a disallowed one" have nothing to
enforce against.

---

## No tool exists

Tag inheritance (`extends`), tag-level property declaration, block icons, and
page aliases. All have working API methods and no tool. If any becomes one,
this spec needs a suite.

Assigning or freeing a UUID. `:block/uuid` cannot be written, and recycling
does not release an identity. This is what makes split-identity damage
unrepairable by reassignment.

---

## Run order

Suite 0 first — a regression there makes everything after it uninterpretable.
Then Suite 1, since fixtures depend on page creation, and Suite 2, since most
later suites need blocks to target. Then 3, 4, 5, 6. Suite 8 is assembled from
envelopes already captured. Suite 7 last, and T-706 last of all.

After the run, update `scripts/live_reliability.py` with any assumption that
turned out to be wrong. A finding recorded only in a run log gets rediscovered
the expensive way — several in this spec were rediscovered twice before being
written down.

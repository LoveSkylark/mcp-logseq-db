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
5. **On any error, do not retry.** Read actual state first: a failed response
   does not imply a failed write, and a successful one does not imply a
   completed write. Record whether the mutation committed.
6. Prefer attribute patterns and `#uuid` literals in verification queries.
   Predicate functions are used by the server in one place (`listAssets`) but
   have hung the DB worker before — see T-705.
7. Destructive steps require explicit confirmation: `deleteTag`,
   `deleteProperty`, `deletePage`, `clearPage`, and `removeBlock` on anything
   with children.
8. Tear down fixtures at the end of the run.

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

Run first. Each of these was broken and fixed; a failure here means the fix did
not hold or the build changed underneath it.

| ID | Test | Expected |
|---|---|---|
| T-001 | `createBlock` on a page | Succeeds. Previously failed with "The Imported EDN has 4 validation error(s)" because `upsertNodes` was sent a second options argument. |
| T-002 | `createManyBlocks`, `createPageofBlocks`, `updateBlock` | All succeed. Same single route as T-001 — one failure means all four. |
| T-003 | `getPage` with `detail=properties` | Returns rows. Previously 500'd: `(pull ?value ...)` received scalars such as `:block/order` strings. |
| T-004 | `getPage` with `detail=all` | Returns; recovers with T-003. |
| T-005 | `getPageUUID` with the lowercase form of a mixed-case title | Resolves via the normalized name fallback. |
| T-006 | `getPageUUID` for a title also used by a tag | Resolves to the page. Tags carry `:block/name` too; the Page-class filter should separate them. |
| T-007 | `createBlock` with `dry_run: true`, then the same call for real | Dry run states it is local-only and is **not** evidence. Compare the two outcomes. |

---

## Suite 1 — Pages

| ID | Test | Expected |
|---|---|---|
| T-101 | `createPage` | Page returned with a UUID; readable by that UUID; carries `:logseq.class/Page` |
| T-102 | `createPage` with an existing title | Rejected before writing, not duplicated |
| T-103 | `getPageUUID` on an ambiguous title | `found: false` with candidates; no guess |
| T-104 | `renamePage` | Title changes, **UUID stable**, `:block/name` updated, still a page |
| T-105 | `renamePage` onto an existing title | Rejected |
| T-106 | `deletePage` on a page with no inbound refs | Recycled: `:logseq.property/deleted-at` set, UUID and tags retained |
| T-107 | `deletePage` on a page **with** inbound refs, no acknowledgement | Refused, referring entities listed |
| T-108 | Same with `acknowledge_reference_rewrite: true` | Proceeds; **check whether the inbound refs still point at it** |
| T-109 | `listPages` after T-106 | Recycled page absent |
| T-110 | `listRecycled` after T-106 | Recycled page present |
| T-111 | Which identifier `deletePage` accepts | The envelope reports `via its uuid` or `via its name`. **Record which** — this also settles `deleteTag`. |
| T-112 | `clearPage` on a page with nested blocks | All blocks gone; page, its tags and its property values intact |
| T-113 | `getPage` at each `detail` value | `page`, `blocks`, `tags`, `properties`, `declared`, `all` each return their own shape |

---

## Suite 2 — Blocks

| ID | Test | Expected |
|---|---|---|
| T-201 | `createBlock` with a page parent | Top-level block; `:block/parent` and `:block/page` both the page |
| T-202 | `createBlock` with a **block** parent | Nested; `:block/parent` the block, `:block/page` still the page |
| T-203 | Depth 3 via T-202 twice | `:block/parent` chains; `:block/page` unchanged at every level |
| T-204 | `createManyBlocks`, 3 blocks | All three created; `:block/order` ascending |
| T-205 | `createManyBlocks` with two identical titles under one parent | Rejected before writing |
| T-206 | Same title under two different parents | Both created |
| T-207 | `createPageofBlocks`, 3 levels | Tree correct; count the calls — expect 2d−1 |
| T-208 | `createPageofBlocks` with a repeated title in different branches | Both created under the right parents |
| T-209 | `updateBlock` | Title changes; UUID stable |
| T-210 | `removeBlock` on a childless block | Gone; verified absent |
| T-211 | `removeBlock` on a block with descendants | Whole subtree gone; no orphan with a dangling `:block/parent` |
| T-212 | `getBlockUUID` on a page with nested blocks | Returns **every** block at any depth, ordered |
| T-213 | `getBlockUUID` on an empty page | `[]`, not an error |
| T-214 | `getBlock` on a deleted UUID | `found: false`, distinguishable from a transport failure |
| T-215 | `getBlockTree` with `max_nodes: 1` | `truncated: true`, accurate `node_count` |
| T-216 | `getBlockTree` with `max_depth: 1` | Root plus one generation — depth counts generations **below** the root |
| T-217 | A batch where one operation is invalid | Record whether the valid ones landed. **Batch atomicity is untested.** |

---

## Suite 3 — Tags

| ID | Test | Expected |
|---|---|---|
| T-301 | `creatTag` | Created; ident carries a random suffix; **record the ident** |
| T-302 | `addTag` to a block | `:block/tags` updated |
| T-303 | `addTag` to a page | Same tool, same result — the target is uniform |
| T-304 | `addTag` twice with the same tag | Record whether it is idempotent |
| T-305 | `removeTag` with two tags present | Only the named relation removed; the other survives |
| T-306 | `removeTag` from a page | Page still has `:logseq.class/Page` and is still a page |
| T-307 | `getTagUsers` on a tag applied to a page and a block | Both returned; pages distinguishable by `:block/name` |
| T-308 | `getTagUUID` on an ambiguous title | `found: false` with candidates |
| T-309 | `deleteTag` on an unused tag | **Unverified route.** Record the verdict and which identifier worked. |
| T-310 | `deleteTag` on a tag in use | `getTagUsers` first; check every `:block/tags` and `:block/refs` entry is cleared |
| T-311 | `listOrphanTags` after T-305 | The now-unused tag appears |

---

## Suite 4 — Properties

The sandbox limits writes to `plugin.property.<caller>/*`. Tests that expect a
refusal are testing the guard, not looking for a workaround.

| ID | Test | Expected |
|---|---|---|
| T-401 | `createProperty` with type `default` | Created; **record the assigned ident** |
| T-402 | `createProperty` with a namespaced title | Rejected — Logseq treats it as a page name and refuses the `/` |
| T-403 | `createProperty` for each type: `number`, `checkbox`, `url`, `datetime`, `node` | Each accepted; record the stored `:logseq.property/type` |
| T-404 | Value storage audit | Set a value of each type; dump every attribute of the target and of the value entity. **Record which attribute each type writes.** |
| T-405 | `addProperty` on a page | Set and verified |
| T-406 | `addProperty` on a block | Same tool, same result |
| T-407 | `addProperty` with a `user.property/*` ident | Refused **before** the API call |
| T-408 | `addProperty` with a `:logseq.property/*` built-in | Refused the same way |
| T-409 | `addProperty` on a `node` property, passing a literal | Record whether it is rejected or silently stored wrong |
| T-410 | `addProperty` twice with the same value on a cardinality-many property | Exactly one value retained |
| T-411 | Two distinct values on a cardinality-many property | Both retained |
| T-412 | `listClosedValues`, then set `Status` to one of them | Record whether the built-in is writable at all |
| T-413 | `removeProperty` (value) | Attribute, `:block/refs` entry, and value entity all cleared |
| T-414 | `deleteProperty` (definition) with no users | **Unverified route.** Record the verdict. |
| T-415 | `deleteProperty` on a definition still in use | `getProperyUsers` first; check the value is cleared everywhere |
| T-416 | `getPropertyIndent` on a title shared with a tag | Resolves to the property, not the tag |
| T-417 | `getProperyUsers` on a property set on both a page and a block | Both returned, with raw and resolved values |

---

## Suite 5 — Declared properties and classes

| ID | Test | Expected |
|---|---|---|
| T-501 | `getPage` `detail=declared` on a page tagged `Task` | Status, Priority, Deadline, Scheduled listed as declared |
| T-502 | Same page, `detail=properties` | Only properties **with values**; the declared-but-unset ones absent |
| T-503 | Set one declared property, re-run both | It moves from declared-only into properties |
| T-504 | `listClosedValues` | `Status` and `Priority` return their permitted entities |
| T-505 | A page's own tags vs its blocks' tags | `getPage detail=tags` covers both; confirm each holder is identified |

---

## Suite 6 — References

| ID | Test | Expected |
|---|---|---|
| T-601 | Write `[[Target]]` into a block title via `createBlock` | Record whether `:block/refs` gains an entry |
| T-602 | Edit that block in the Logseq UI | Record whether a user edit materializes the ref |
| T-603 | Point a `node` property at a page | Target appears in `:block/refs`; check the UI backlink count |
| T-604 | Query `:block/refs` against a target UUID | Referring blocks returned with their pages |
| T-605 | `deletePage` on a referenced page, then T-604 | **References are not rewritten** — confirm they still point at the recycled page |

---

## Suite 7 — Failure handling

| ID | Test | Expected |
|---|---|---|
| T-701 | Any tool with a malformed UUID | Clean `validation` failure naming the argument; no mutation |
| T-702 | A tag tool given the tag's **ident** instead of its UUID | Rejected at the boundary, diagnosed as an ident |
| T-703 | A property tool given a **UUID** instead of an ident | Rejected; this is the silent no-op the guard exists for |
| T-704 | `createBlock` with a page **name** as the parent | Rejected at the boundary. Bypassing the tool, the raw API reports success and writes nothing. |
| T-705 | A normal call immediately after a failed one | Succeeds — no session poisoning |
| T-706 | A query with a `clojure.string/*` predicate, in an isolated session | Record whether it errors cleanly or wedges the worker. **Run last; may require restarting Logseq.** |
| T-707 | After any timeout: re-probe, re-read the target | Establish committed state; check `recovered_after_timeout` and whether `capabilities` reports `writes_disabled` |
| T-708 | Recovery from an open write circuit | Reads still work; writes refused until Logseq is restarted and the MCP reconnected |

---

## Suite 8 — Envelope consistency

Fill in from envelopes captured during earlier suites.

| ID | Test | Expected |
|---|---|---|
| T-801 | `deleteTag` envelope | `verified_state` null; `previous_state` populated with the tag and its holders |
| T-802 | `deleteProperty` envelope | Compare with T-801; flag inconsistencies |
| T-803 | `removeBlock` envelope | `verified_entities` empty; `previous_entities` carries the whole subtree |
| T-804 | `deletePage` envelope | `previous_entities` carries page and blocks; `observed_entities` carries inbound refs |
| T-805 | Create and edit envelopes | `previous_entities` behaviour on non-deletes |
| T-806 | A cascade delete diagnostic | Does it cover descendants or only the target UUID? |
| T-807 | Any `verified: false` result | `previous_state` and `observed_state` both present, and distinguishable |

---

## Not covered, because no tool exists

Moving a block. No route has been found — `insertBatchBlock` and
`prependBlockInPage` are untested and would be the place to look.

Tag inheritance (`extends`), tag-level property declaration, block icons, and
page aliases. All have working API methods and no tool. If any becomes a tool,
this spec needs a suite.

---

## Run order

Suite 0 first — a regression there makes everything after it uninterpretable.
Then Suite 1, since fixtures depend on page creation, and Suite 2, since most
later suites need blocks to target. Then 3, 4, 5, 6. Suite 8 is assembled from
envelopes already captured. Suite 7 last, and T-706 last of all.

After the run, update `scripts/live_reliability.py` with any assumption that
turned out to be wrong. A finding recorded only in a run log gets rediscovered
the expensive way.
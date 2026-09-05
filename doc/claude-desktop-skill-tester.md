built for the old code needs to be updated!!!


# Logseq DB graph over MCP — test specification

A reusable test plan for `mcp-logseq-db` against a Logseq DB graph. Contains
tests to perform only. Record outcomes in a separate run log — do not edit
results into this file.

Coverage is derived from the DB graph content-structure capabilities documented
upstream (logseq/logseq Property System; logseq/docs DB Graph System).

---

## Protocol

1. Run `db_capabilities` first. Record Logseq version, manifest, and whether
   experimental writes are enabled. Skip suites the manifest does not support.
2. Before any write, state the target UUIDs, the operations, and how each is
   reversed.
3. Create one fixture page per run: `MCP T <date> <run>`. Do not test against
   real content.
4. Verify every write with an independent Datascript read. An envelope
   reporting `verified: true` is necessary but not sufficient.
5. **On any tool error, do not retry.** Read actual state first — a failed
   response does not imply a failed write. Record whether the mutation
   committed.
6. Queries must use attribute patterns and `#uuid` literals only. Do not use
   `clojure.string/*` or other predicate functions inside a query.
7. Destructive steps (tag deletion, property-definition removal, block
   deletion) require explicit confirmation before running.
8. Tear down fixtures at the end of the run.

### Standard verification query

Replace `ID` with the entity's numeric `:db/id`:

    [:find ?e ?a ?v :where [?e ?a ?v] [(= ?e ID)]]

### Verdicts

| Verdict | Meaning |
|---|---|
| PASS | Behaved as expected, verified independently |
| FAIL | Did not behave as expected |
| FALSE-ERROR | Tool reported an error but the mutation committed |
| SILENT-FAIL | Tool reported success but nothing committed |
| BLOCKED | Could not run (unsupported, or prerequisite failed) |

### Run log format

    run: <date> | logseq <version> | manifest <hash/summary>
    ID | verdict | observed | notes

---

## Suite 1 — Node structure

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-101 | Create page | `db_create_page` | Page returned with UUID; readable by UUID |
| T-102 | Batch block creation | `db_upsert_nodes`, 3 blocks on the page | 3 blocks created; `:block/order` ascending |
| T-103 | Nested creation depth 3 | Insert child of a block, then child of that child | `:block/parent` chains; `:block/page` stays the owning page on both |
| T-104 | Subtree move | Move a parent with 2 descendants under a sibling | Parent reparented; all descendants follow; `:block/page` unchanged |
| T-105 | Unsupported placement | Move with `placement: after` | Clean rejection; target unmodified; next call succeeds |
| T-106 | Cascade delete | Delete a block with a child | Both removed; no orphan with dangling `:block/parent` |
| T-107 | Rename page | `db_rename_page` | Title and `:block/name` change; UUID stable |
| T-108 | Recycle page | `db_recycle_page` | `:logseq.property/deleted-at` set; page no longer listed |
| T-109 | Page-level read | `db_get_page_data` on a page with nested blocks | Confirm which depths the reader returns |
| T-110 | Alias | Set `:block/alias` between two pages | Alias resolves; check reverse direction |

## Suite 2 — Property types and value storage

Upstream storage per type: `default` → `:logseq.property/value` or
`:block/title`; `number` → `:logseq.property/value`; `date` →
`:block/journal-day`; `datetime` → ms timestamp; `checkbox` → Boolean;
`url` → `:logseq.property/value`; `node` → entity reference.

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-201 | Storage shape audit | Create `default`, `url`, `number` props; set a value on each; dump every attribute of each value entity | Identify which attribute each type writes, and which attribute the server's verifier keys on |
| T-202 | Number write | Set a `number` value | Value committed as requested; note whether the tool reports success |
| T-203 | Checkbox write | Set a `checkbox` value | Boolean committed; note reported result |
| T-204 | Date write | Create `date` prop; set a value | Confirm `:block/journal-day`; check whether a journal page is created |
| T-205 | Datetime write | Create `datetime` prop; set ms timestamp | Stored as a number |
| T-206 | Type validation | Set a non-numeric string on a `number` prop | Rejected by validation; distinguish a genuine validation rejection from a transport error |
| T-207 | Internal types | Attempt to create a property typed `map`, `class`, `page`, `coll`, or `keyword` | Rejected as not user-creatable |
| T-208 | Retry safety | On a cardinality-many prop, issue the same value twice | Exactly one value retained; no duplicate accumulation |
| T-209 | Page as target | Set a property on a page UUID | Property set on the page node |
| T-210 | Property removal | Remove a property from a block | Attribute, `:block/refs` entry, and value entity all cleared |

## Suite 3 — Property configuration

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-301 | Cardinality many | Create prop with many cardinality; set 2 distinct values | Both retained |
| T-302 | Unsupported cardinality | Many-cardinality on `checkbox` and on `datetime` | Rejected (unsupported upstream) |
| T-303 | Closed values, valid | Define a choice set; set an allowed value | Accepted |
| T-304 | Closed values, invalid | Set a value outside the set | Rejected |
| T-305 | Default value | Define a default; add the prop without a value | Default applied |
| T-306 | Node class restriction | Set `:logseq.property/classes` on a `node` prop; point it at a wrong-class target | Rejected |
| T-307 | Visibility flags | Set `public?`, `hide?`, `hide-empty-value` | Flags persist on the definition |
| T-308 | UI position | Set `ui-position` | Value persists |
| T-309 | Definition removal in use | Remove a property definition still applied to a block | Block attribute, ref, and orphaned value entity all cleared |

## Suite 4 — Tags, classes, inheritance

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-401 | Create and apply | Create tag; apply to a block; apply to a page | `:block/tags` updated on both node types |
| T-402 | Tag at creation | Create a block passing `tag_uuids` | Tag applied in one call |
| T-403 | Extends | Create parent and child tags; `db_add_tag_extends` | `:logseq.property.class/extends` set |
| T-404 | Class schema | Add a property to the parent tag | `:logseq.property.class/properties` set |
| T-405 | Property inheritance | Tag a block with the child tag | Determine whether the parent's schema property appears on the instance |
| T-406 | Tag objects | `db_get_tag_objects` on the parent | Determine whether child-tagged objects are included |
| T-407 | Remove extends | `db_remove_tag_extends` | Inheritance relation dropped |
| T-408 | Rename tag | Rename a tag; then look it up by old title, new title, and ident | Establish which identifier survives a rename |
| T-409 | Namespaced tag | Create a tag named `Parent/Child` | Determine whether a hierarchy is created or the name is literal |
| T-410 | Delete tag in use | Delete a tag applied to several nodes | All `:block/tags` and `:block/refs` cleared; no dangling refs |

## Suite 5 — References and backlinks

Both pages and blocks use `[[]]` referencing in DB graphs; `(())` is retired.

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-501 | Bracket text on write | Write `[[Target]]` into a block title via MCP | Determine whether `:block/refs` gains an entry |
| T-502 | UI re-parse | Open that block in the Logseq editor and edit it | Determine whether a user edit materializes the ref |
| T-503 | Node property to page | Point a `node` prop at a page | Target id appears in `:block/refs`; check the UI Backlinks count |
| T-504 | Node property to block | Point a `node` prop at a block | Block-level reference behavior |
| T-505 | Tag ref vs backlink | Tag a page; inspect the UI Backlinks column | Determine whether tag refs count as backlinks |
| T-506 | Backlink query | Query `:block/refs` against a target UUID | Returns referring blocks with their pages |
| T-507 | Unlinked references | Create a block containing the target's title as plain text | Determine whether unlinked references are exposed |

## Suite 6 — Built-in properties and tasks

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-601 | Task status | Set the built-in status property on a block | Accepted; closed-value set enforced |
| T-602 | Priority | Set the built-in priority property | Accepted |
| T-603 | Deadline / scheduled | Set these `datetime` built-ins | Stored correctly |
| T-604 | Icon | Set an icon (`map` type) and remove it | Set and absence both verified |
| T-605 | Built-in immutability | Attempt to modify a built-in property definition | Rejected |

## Suite 7 — Failure handling and recovery

| ID | Test | Steps | Expected |
|---|---|---|---|
| T-701 | Malformed UUID | Write with an invalid UUID | Clean error; no mutation |
| T-702 | Nonexistent ident | Write with an unknown property ident | Clean error |
| T-703 | Session survival | Issue a normal call immediately after a failed one | Succeeds |
| T-704 | Deleted-entity read | `db_get_block` on a deleted UUID | Determine whether absence is distinguishable from transport failure |
| T-705 | Query predicate safety | In an isolated session, run a query using a `clojure.string/*` predicate | Determine whether it errors cleanly or hangs the DB worker. **Run last — may require restarting Logseq.** |
| T-706 | Timeout recovery | After a timeout, re-probe and re-read the affected entity | Establish committed state; check `recovered_after_timeout` |

## Suite 8 — Envelope contract consistency

| ID | Test | Expected |
|---|---|---|
| T-801 | `db_delete_tag` envelope | `verified_state` null; `previous_state` populated |
| T-802 | `db_remove_property` envelope | Compare against T-801; flag any inconsistency |
| T-803 | `db_delete_block_experimental` envelope | `verified_entities` empty; `previous_entities` populated; absence diagnostic present |
| T-804 | Non-delete writes | `previous_entities` / `previous_state` behavior on creates and edits |
| T-805 | Cascade reporting | After a cascade delete, check whether the diagnostic covers descendants or only the target UUID |

---

## Suggested run order

Suite 1 first as a smoke test — if node structure is broken, later suites are
uninterpretable. Then Suite 2, since value-storage behavior determines whether
property results elsewhere can be trusted. Then Suites 3, 4, 5, 6. Suite 8 can
be filled in from envelopes captured during earlier suites. Suite 7 last, and
T-705 last of all, since it may take the graph down.
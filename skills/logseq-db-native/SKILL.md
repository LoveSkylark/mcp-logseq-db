---
name: logseq-db-native
description: "Use when reading or modifying a Logseq 2.x DB graph through the mcp-logseq-db server. Covers DB queries, exact property and tag identities, verified metadata writes, destructive-operation safeguards, and timeout recovery. Never use file-graph or non-DB Logseq tools."
---

# Logseq DB-Native MCP

**Latest live status:** Logseq 2.0.1 DB graph, `mcp-logseq-db` 0.2.7.

| Tested | Path | Status |
|---|---|---|
| 2026-09-02 | Datascript-backed block readers | PASS, including an existing depth-3 tree |
| 2026-09-02 | `upsertNodes` page/top-level-block creation and block edit | PASS |
| 2026-09-02 | Property/tag definition creation, rename, and verified removals | PASS for tools still listed below |
| 2026-09-02 | `upsertBlockProperty`, `addBlockTag`, `removeBlockTag`, `setBlockIcon`, `addTagExtends` | PASS after isolated fresh Logseq restart; earlier same-session timeouts were write-path degradation |
| 2026-09-02 | CLI graph-worker `delete-blocks` operation | PASS with exact absence read-back |
| 2026-09-02 | CLI graph-worker child insert and move | PASS with parent/page read-back |

Revalidate version-sensitive claims after changing Logseq or the MCP server.

Use this skill only with the `mcp-logseq-db` server and a Logseq 2.x DB graph.
This server is intentionally narrow. It exposes verified MCP tools
backed by DB HTTP reads/metadata and graph-worker structural operations.

Do not load the legacy `logseq-db-graph` or `logseq-file-graph` skill in the
same conversation.

## Hard boundaries

- Before doing any graph work, inspect the available connector and tools. The
  connector must be `mcp-logseq-db` and must expose the inventory below.
  If the connector is named `logseq`, or tools such as `upsert_nodes`,
  `create_page`, `search_blocks`, or `get_page_data` appear, the legacy server
  is active. Stop and ask the user to restart Claude Desktop with the correct
  configuration. Do not adapt this skill to the legacy tool catalog.
- Call only the MCP tool names listed in this skill. Never call or emit a raw
  `logseq.*` API method.
- Never invent a page, block, search, batch, file, monitoring, or deletion
  tool that is not in the current MCP tool list.
- Do not use Markdown `key:: value`, YAML frontmatter, file paths, or page-file
  replacement as substitutes for DB properties.
- The server can create pages and top-level blocks, edit block titles, rename
  pages, and recycle pages through verified `logseq.DB.*` aliases.
- The promoted path creates and moves block subtrees as `child` or `after`.
- Tags can be renamed and permanently deleted by exact UUID. Unlike page
  deletion, tag deletion does not use the recycle bin.
- File operations and callback subscriptions are unavailable.

## Start every workflow with capabilities

Call `capabilities` once near the start of a conversation. Treat its
reported read methods as probes of the connected Logseq process. Supported
write methods come from the server's dated live-verification manifest; they
are not re-probed on startup because doing so would mutate the graph. Candidate
methods have not passed complete read-back testing and must not be called.

`supported_write_operations` is a dated manifest, not a live mutation probe.
Even when `version_matches_manifest=true`, verify an unfamiliar path with a
dry-run or low-risk disposable write before depending on it. Version matching
is necessary but not sufficient evidence that every Logseq alias is healthy.

Read the fields precisely:

- `supported_entity_types` lists entity kinds the query tools can read.
- `db_version`, `verified_against_db_version`, and
  `version_matches_manifest` show whether the connected Logseq build matches
  the write manifest. If they differ, warn the user and use dry-runs plus
  stricter read-back before writes.
- `metadata_mutable_entity_types` lists entity kinds accepted by promoted
  metadata operations. A page UUID can be a metadata target where a tool
  accepts a block/node UUID.
- `supported_content_operations` is separate. An empty list means the MCP
  cannot create, edit, move, or delete page/block content even though those
  entities remain queryable and metadata-mutable.
- `supported_read_operations` and `supported_query_features` were probed
  against the connected process during this call.
- `supported_write_operations` and `supported_removal_operations` are the
  server's dated, promoted write manifest. They are not destructive startup
  probes. A listed method may still fail if the connected Logseq build differs.
- `supported_mcp_write_tools` lists user-facing MCP operations. Use this field
  for semantic capabilities such as tag rename/delete, which share the generic
  raw `renamePage`/`deletePage` aliases and therefore have no separate raw
  `renameTag`/`deleteTag` method names.
- `experimental_mcp_write_tools` is empty; replaced experimental tools are no
  longer registered.
- `candidate_write_operations` are allowed internally for further controlled
  testing but are not MCP capabilities. Do not call or advertise them.
- `unavailable_over_http` methods are rejected for this server/build and must
  not be retried.
- `rejected_operations` are bound aliases that failed timeout, response-shape,
  or read-back testing. Do not call them directly.
- `experimental_operations` is empty.

The Logseq API is version-sensitive. The tool list and current capability
result take precedence over examples or remembered behavior.

## Node model

Read this before reasoning about graph structure. Pages, blocks, tags, and
properties share Logseq's entity store and commonly expose `:block/uuid` and
`:block/title`. Their attributes and tags determine their role; pages normally
carry `:block/name`.

- `:block/parent` references the immediate page or block ancestor.
- `:block/page` is the denormalized owning page and is independent of the
  immediate parent for nested blocks.
- `:block/order` is a fractional-index string used to order siblings.

Before calling `get_block` or `get_block_tree`, prefer a UUID already
returned as a block by a structured reader. If entity kind is uncertain, use:

```clojure
[:find ?name
 :where
 [?entity :block/uuid #uuid "TARGET_UUID"]
 [?entity :block/name ?name]]
```

Any result means the target is a page; use `get_page_data`. Both block
readers now return `found=false` with reason `target is a page, not a block`
for this mistake. For a whole page hierarchy, call `get_page_data` for its
direct children and `get_block_tree` for each child root.

This MCP supports promoted top-level and nested block creation. The model still
matters when reading query results. A nested node whose `:block/page`
points to a non-page is malformed even if Logseq renders it under its parent.
When investigating structure, verify parent and owning page independently.

Use this bounded diagnostic when malformed ownership is suspected:

```clojure
[:find (pull ?entity [:db/id :block/title
                      {:block/page [:db/id :block/title]}])
 :where
 [?entity :block/page ?page]
 [(missing? $ ?page :block/name)]]
```

## DB identity rules

Logseq DB entities can expose bare fields such as `id`, `ident`, `uuid`, and
`title`.

- Properties: read and remove by exact namespaced ident, for example
  `:logseq.property/status` or `:plugin.property._test_plugin/MyProperty`.
- Tags: `get_tag` accepts exact ident, UUID, or resolvable title.
  `get_tags_by_name` follows Logseq's normalized internal-name lookup and a
  display title may not resolve for every plugin-created tag. Prefer
  `get_all_tags`, then retain the returned ident and UUID. All tag mutations
  use exact tag UUIDs.
- The MCP accepts a property ident for both tag-property tools. For removal,
  the server resolves that ident to the property UUID required by Logseq.
- A bare property display name is rejected by this MCP before HTTP when an
  exact ident is required. Continue to pass full idents.
- Blocks/nodes: all metadata mutations use exact block UUIDs.
- Never select a destructive target from a fuzzy search result alone.
- Resolve the entity, show its exact identity, and validate its current state
  before removal.

`upsert_property` accepts a display title. Logseq may generate a
plugin-namespaced ident. Always retain the exact ident returned in
`verified_state`; use that ident for later reads and removal.
Generated idents vary by creation route and may include random suffixes. Never
construct or predict an ident from a display title.

## Read workflow

1. Call `capabilities`.
2. Use the narrowest structured reader available:
  - `list_pages` to discover pages and UUIDs.
   - `get_page_data` to read one page and its direct child blocks. It does
     not recursively include nested descendants. Missing nested blocks in this
     response are not evidence of deletion; use `get_block` or a Datascript
     parent/page query to inspect the complete hierarchy.
    - `get_block` to read one exact block UUID. This MCP tool performs an
      exact `logseq.DB.datascriptQuery` pull; it never calls the rejected raw
      `logseq.DB.getBlock` alias. A `getBlock` entry in `rejected_operations`
      therefore does not imply that `get_block` is unsupported or using the
      wrong path. Do not claim otherwise from timing alone.
    - `get_block_tree` to read a known block and all descendants. It uses
      one exact root lookup and one page-scoped Datascript query, then builds
      the requested subtree locally. Prefer it over repeated `get_block`
      calls when hierarchy is needed. Respect `node_count` and `truncated`;
      increase `max_depth` or `max_nodes` only when the omitted descendants are
      necessary. The accepted bounds are depth 0-100 and nodes 1-1000.
    - `search` for text discovery.
    - `list_properties(expand=true)` for detailed property definitions.
    - `list_tags(expand=true)` for detailed tags/classes.
   - `get_all_properties` to discover property definitions.
   - `get_property` for one exact property ident.
   - `get_all_tags` to discover tags/classes.
   - `get_tag` for one exact tag identity.
   - `get_tags_by_name` for an exact title lookup.
   - `get_tag_objects` for nodes associated with a known tag. Its result is
     mixed and may contain both pages and blocks; distinguish pages by
     `:block/name`/`name`.
3. Use `q`, `custom_query`, or `datascript_query` only when the
   structured readers cannot answer the question.
4. Preserve `id`, `ident`, and `uuid` in the working plan. Do not reduce an
   entity to display text.

### Query discipline

- Queries are read-only discovery tools. Do not attempt transaction forms.
- Query predicates execute inside Logseq's DB worker. An expensive or invalid
  predicate can wedge that worker even when the MCP process is healthy.
- Query/search calls are single-attempt. Never automatically repeat a timed-out
  query against the same worker.
- Prefer bounded queries that return only fields needed for the task.
- Use DB attributes such as `:block/title`, `:block/uuid`, and `:block/tags`.
- Validate query results before using any returned UUID in a write.
- Avoid dumping the entire graph when a property, tag, UUID, or title filter
  can narrow the result.

Example exact-UUID Datascript lookup:

```clojure
[:find (pull ?entity [*]) .
 :where
 [?entity :block/uuid #uuid "BLOCK_UUID"]]
```

## Plan before writing

For each mutation, establish this plan in the conversation:

```text
target: exact title plus UUID or property ident
current state: relevant properties, tags, inheritance, or icon
operation: exact MCP tool
requested state: exact typed value or relationship
reversibility: removal tool or explicit lack of one
verification: field and identity expected after the write
```

Ask for confirmation before:

- `remove_property`;
- permanently deleting a tag;
- removing a property/tag relationship that may affect inherited schemas; or
- changing metadata on multiple nodes.

## Page and block content

Use `upsert_nodes` for the supported DB content operations. The server
always runs Logseq's dry-run validation before a commit and then reads every
affected entity back.

For a single operation, prefer the explicit wrapper:

- `create_page(title)` creates one page.
- `create_top_level_block(page_uuid, title, tag_uuids)` creates one block
  directly under a page.
- `upsert_block(block_uuid, title)` edits one existing block title.

All three wrappers support `dry_run=true` and delegate to the same validated
`DB.upsertNodes` path. They do not call the timeout-prone direct aliases.

Supported operation shapes:

```json
{"operation":"add","entityType":"page","id":"temp-page","data":{"title":"Page title"}}
{"operation":"add","entityType":"block","data":{"title":"Block text","page-id":"temp-page"}}
{"operation":"edit","entityType":"block","id":"BLOCK_UUID","data":{"title":"New text"}}
```

- For an existing page, `data.page-id` must be that page's exact UUID.
- For a page created earlier in the same batch, use its temporary ID.
- Added titles must be unique within the batch so read-back is unambiguous.
- A batch may contain at most 100 operations.
- Set `dry_run=true` to validate without committing.
- Do not pass a block UUID as `data.page-id`. Although Logseq accepts it and
  renders a child, live testing showed malformed ownership where `:block/page`
  pointed to the parent block. The server rejects this.
- Use `rename_page` with an exact page UUID.
- Use `upsert_block(block_uuid, title)` for a single existing block-title
  edit. It is an edit-only convenience wrapper over `upsert_nodes`; it does
  not create, move, nest, or delete a block. Set `dry_run=true` to validate
  without committing.
- `recycle_page` recycles an exact page UUID and verifies its
  `:logseq.property/deleted-at` marker. It does not permanently erase it.
- `delete_page` is retained only as a compatibility alias. Prefer
  `recycle_page` in plans and user-facing language.
- Use `insert_block` and `move_block` for verified `child` or `after`
  placement, and `delete_block` for verified subtree deletion. True
  `before` placement is unavailable.

### Page references and backlinks

- Write `[[TARGET_PAGE_UUID]]` in a block title to create a structural
  `:block/refs` relation. The server verifies UUID bracket references after
  creation or title edits.
- `[[Page Title]]` stores literal bracket text on this write path and does not
  create `:block/refs`, even if Logseq renders it as clickable text.
- A node-typed property also creates a structural ref when its value is the
  target page's numeric `:db/id`, not its UUID string.
- Tag assignments create refs with tag semantics; they are not equivalent to
  ordinary backlinks in Logseq views.
- Check incoming references with `:block/refs`; `:block/path-refs` is not
  available on the tested build.

```clojure
[:find (pull ?source [:db/id :block/uuid :block/title
                      {:block/page [:db/id :block/title]}])
 :where
 [?source :block/refs ?target]
 [?target :block/uuid #uuid "TARGET_PAGE_UUID"]]
```

### Block hierarchy and deletion

Promoted structural tools:

- `insert_block(target_uuid, title, placement)` supports `child` and `after`.
- `move_block(block_uuid, target_uuid, placement)` supports `child` and
  `after` while preserving the complete subtree.
- `delete_block(block_uuid)` deletes and verifies the complete subtree.

Structural writes return `verified` and `diagnostic` in addition to the normal
result envelope. A completed MCP call is not proof of mutation:

- `verified=true`: the requested state was observed.
- `verified=false`: report that the operation did not complete; include the
  diagnostic and observed state. Do not retry automatically.
- `recovered_after_timeout=true`: the underlying write timed out and read-back
  determined the outcome.
- Unsupported placement values fail before HTTP and make no mutation.

## Property workflow

### Create or update a property

1. Call `get_all_properties` and check for an existing exact title/ident.
2. Choose a valid schema type: `date`, `number`, `checkbox`, `default`,
   `string`, `node`, `url`, `datetime`, `json`, or `asset`.
  Built-in definitions may display internal types such as `map`, `page`,
  `class`, or `property`; these are not accepted user-property creation types.
3. Call `upsert_property(title, schema, options)` once.
4. Retain the generated ident from `verified_state`.
5. Do not retry blindly if the tool reports an ambiguous timeout.

### Remove a property

1. Call `get_property` with the exact namespaced ident.
2. Confirm the returned ident exactly matches the requested target.
3. Explain that property removal is destructive.
4. Call `remove_property` only after confirmation.
5. The server verifies that `get_property` returns no entity afterward.
6. The server also verifies that no direct attribute use or property-created
  value entity remains. `previous_state` retains the removed definition and
  its pre-delete usage evidence.

### Block properties

- Use `upsert_block_property` with an exact page/block UUID, exact property
  ident, typed value, and optional options object. Never pass a property display
  name. The raw verified shape is `[block_uuid, property_ident, value, options]`;
  the MCP supplies `{}` when options are omitted.
- `remove_block_property` remains available for cleanup of an existing
  value. Verify exact absence afterward.

## Tag workflow

### Discover and create

- Use `get_all_tags`, `get_tag`, or `get_tags_by_name` before creating
  a tag.
- Call `create_tag` only when no existing exact tag is suitable.
- Retain the returned tag UUID and ident.
- Direct API creation commonly generates a plugin-namespaced ident and extends
  Root automatically. Read and retain the returned values; never construct the
  ident from the title.
- Use `rename_tag(tag_uuid, new_title)` to rename an exact tag.
- A rename changes title/name fields but leaves the generated ident unchanged.
  Treat the ident and UUID as durable identities. After a rename,
  `get_tag(old_title)` may still resolve through the old title fragment in
  the unchanged ident, while `get_tags_by_name(old_title)` returns nothing.
  Use UUID or exact ident when lookup semantics matter.
- Use `delete_tag(tag_uuid)` only after explicit confirmation. It verifies
  that `get_tag` returns no entity and returns the deleted snapshot in
  `previous_state`. It also verifies that no `:block/tags` or `:block/refs`
  datoms still point to the deleted tag.

### Tag properties and inheritance

- `add_tag_property(tag_uuid, property_ident)` adds a property to a tag.
  It updates `:logseq.property.class/properties`; the property also appears in
  the tag's structural refs.
- `remove_tag_property(tag_uuid, property_ident)` removes it. The server
  resolves the property ident to the UUID form required by Logseq.
- `remove_tag_extends(tag_uuid, parent_tag_uuid)` removes inheritance.
- `add_tag_extends` and `remove_tag_extends` require exact child and
  parent tag UUIDs. Do not pass titles or numeric ids.

### Tagging a page or block

- `create_top_level_block(page_uuid, title, tag_uuids)` can apply tags in
  the same creation call.
- `add_block_tag` and `remove_block_tag` require an exact page/block UUID
  and exact tag UUID. The MCP does not resolve display titles for these writes.
  These tools use the graph-worker path because it remained responsive when
  the equivalent DB HTTP aliases timed out in mixed write sequences.
- Do not insert `#tag` text as a substitute for changing `:block/tags`.

## Block icons

- `set_block_icon` requires an exact block UUID, `icon_type` of
  `tabler-icon` or `emoji`, and the icon name. Use a Tabler id such as `test` or
  the exact emoji-mart display name.
- `remove_block_icon` removes the icon from an exact block UUID.

## Verification and timeout handling

Metadata writes return `response`, `verified_state`, `previous_state`, and
`recovered_after_timeout`. Content writes return `response`,
`verified_entities`, `previous_entities`, `observed_entities`, `verified`, and
`diagnostic`. A successful HTTP status alone is not considered success.

An anticipated tool failure returns an MCP error whose text is a JSON envelope
with `verified=false`, `failure_stage`, `diagnostic`, `error_type`, and empty
verified/observed/previous fields when state was unavailable. Stages are
`validation`, `transport`, `logseq_error`, or `readback_mismatch`.

- `response` is often `null` for a successful Logseq mutation. Treat it as the
  raw API result, not verification evidence.
- `verified_state` is the server's post-write read-back. Claim success only
  when it contains the expected attribute or relationship.
- For `remove_property`, both `response` and `verified_state` are `null`
  after verified absence. For relationship/icon removals, `verified_state`
  normally contains the surviving entity without the removed value.
- `recovered_after_timeout=true` means the original response was ambiguous but
  read-back established the resulting state. Mention that recovery explicitly.
- A timed-out write may have committed. Never immediately repeat it.
- Read the exact target with a fresh tool call and reconcile observed state.
- The first write timeout opens a server-side write circuit. Every later write
  is rejected before HTTP, including calls through a different tool. Reads
  remain available for reconciliation. Check `write_circuit_open` and
  `write_circuit_reason` in `capabilities`, restart Logseq, and reconnect
  the MCP before another write. Restarting only Logseq does not reset the
  process-local MCP circuit.
- If creation timed out before Logseq returned a generated ident, stop and
  inspect the property/tag listings for a uniquely matching title.
- A failed, malformed, cancelled, or timed-out read must not poison a later
  read. A write timeout intentionally blocks later writes until MCP reconnect;
  reads remain available for reconciliation.
- The server serializes writes across concurrent MCP calls so mutation and
  read-back cannot interleave with another write.
- Read-only transport failures may be retried with a fresh connection. Writes
  are never retried automatically.
- Each HTTP attempt has a hard wall-clock deadline equal to the configured
  connect timeout plus read timeout. With the defaults, one attempt is bounded
  to 18 seconds. Query/search and write calls get one attempt; eligible normal
  reads may use `LOGSEQ_READ_ATTEMPTS` attempts.
- Read-back is polled for a bounded number of attempts to tolerate delayed
  visibility; polling repeats only reads, never writes.
- Responses exceeding the configured byte limit are rejected rather than sent
  unbounded into the MCP conversation.
- If `check_current_is_db_graph` or another trivial read times out after a
  query timeout, restart Logseq itself to clear its DB worker, then restart the
  MCP connection. Restarting only the MCP relay cannot clear a wedged worker.
- Distinguish timeout from connection refusal: timeout suggests a stuck Logseq
  worker; connection refusal means Logseq's HTTP server is not listening.
- Distinguish an MCP timeout result from a Claude Desktop UI wait. If the
  connector UI waits substantially longer than the configured bound without
  returning a tool result, do not infer which raw Logseq method ran and do not
  report the MCP tool as broken. Treat it as a client/session transport issue,
  reconnect the MCP, run `check_current_is_db_graph`, and then reconcile the
  target state. Never repeat an ambiguous mutation merely because the UI lost
  its result.
- A normal read succeeding after a write timeout does not prove the write path
  is healthy. Logseq 2.0.1 can keep reads responsive while individual writes
  repeatedly time out. Reconcile the timed-out target, restart Logseq, and test
  one disposable write before classifying the method as unsupported.

Use this discriminator:

| Symptom | Meaning | Action |
|---|---|---|
| About 4 minutes, `No result received from the Claude Desktop app` | Client/session transport | Reconnect and re-read; do not blame or repeat the tool |
| Fast MCP error with a JSON `failure_stage` | Server validation, transport, Logseq, or read-back failure | Read the diagnostic and reconcile state; do not retry blindly |
| Envelope with `recovered_after_timeout=true` | Underlying write timed out and bounded read-back ran | Trust `verified` and the observed-state fields |

One default HTTP attempt is bounded to 18 seconds. A UI wait substantially
beyond about 20 seconds is not the server's single-attempt HTTP timeout.

## Optional write access scopes

Deployments can restrict writes without restricting reads:

- `LOGSEQ_WRITE_TITLE_PREFIXES`: comma-separated prefixes for created or
  renamed titles.
- `LOGSEQ_WRITE_PROPERTY_PREFIXES`: comma-separated allowed property-ident
  prefixes.
- `LOGSEQ_WRITE_ENTITY_UUIDS`: comma-separated UUID allowlist for writes to
  existing entities.

When configured, values outside the scope fail before mutation. Empty settings
mean unrestricted writes and are appropriate only for a trusted graph.

## Known unavailable or rejected behavior

- `getFavorites`: rejected after HTTP 500 on the tested Logseq 2.0.1 build.
- `setPropertyNodeTags`: rejected after a live timeout.
- Property value choices: transport returned success, but the requested effect
  was not observable through the available property reader, so no MCP tool is
  exposed and it remains a candidate.
- `onChanged` and `onBlockChanged`: callback APIs cannot cross the ordinary
  request/response HTTP boundary.
- File reads/writes: `setFileContent` is a candidate, not a supported tool. No
  real DB file target has passed write/read-back/cleanup verification.
- `createPage`, `insertBlock`, `insertBatchBlock`, `prependBlockInPage`,
  `updateBlock`, `removeBlock`, and `moveBlock` are raw `logseq.DB.*` aliases
  that timed out during live testing and are not exposed. Use `upsert_nodes`
  for page/top-level creation and block-title edits. Use `insert_block`,
  `move_block`, and `delete_block` for worker-backed structure changes.
- `upsertBlockProperty`, `setBlockIcon`, and `addTagExtends` can time out in a
  degraded write session but succeeded with exact read-back as the first write
  after a fresh Logseq restart.
- `add_block_tag` and `remove_block_tag` use the graph-worker path because it
  remained responsive when the equivalent DB HTTP aliases timed out.
- `newBlockUUID` and `exportEdn` are bound but unnecessary for current safe
  workflows. `importEdn` is a high-impact whole-graph operation and is not
  exposed. `insertBatchBlocks` (plural) is not bound.
- Promoted `child` and `after` operations use the supported graph-worker path.
  The plugin insert/move/remove aliases timed out and are not exposed.
- `get_block_tree` successfully traversed an existing depth-3, seven-node
  subtree with owning-page references preserved.
- `get_block` returns `{found:false, block:null}` for a missing/deleted UUID;
  absence is not reported as a generic tool error.

## Tool inventory

Reads and capabilities:

- `capabilities`
- `check_current_is_db_graph`
- `get_app_info`
- `get_current_graph`
- `list_pages`
- `get_page_data`
- `search`
- `list_properties`
- `list_tags`
- `q`
- `custom_query`
- `datascript_query`
- `get_all_properties`
- `get_property`
- `get_all_tags`
- `get_tag`
- `get_tags_by_name`
- `get_tag_objects`
- `get_block`
- `get_block_tree`

Promoted writes:

- `upsert_nodes`
- `create_page`
- `create_top_level_block`
- `insert_block`
- `delete_block`
- `move_block`
- `upsert_block`
- `rename_page`
- `delete_page`
- `recycle_page`
- `upsert_property`
- `remove_property`
- `create_tag`
- `rename_tag`
- `delete_tag`
- `add_tag_property`
- `remove_tag_property`
- `add_tag_extends`
- `remove_tag_extends`
- `upsert_block_property`
- `remove_block_property`
- `add_block_tag`
- `remove_block_tag`
- `set_block_icon`
- `remove_block_icon`

## Response discipline

Before writing, state the exact entities and intended changes. After writing,
summarize the verified result, any generated ident, and any remaining
irreversible fixture or uncertainty. Never claim that unsupported page/block
content work was completed through metadata-only tools.

### Capability claims

- A tool description describes one endpoint, not the complete DB schema.
- Do not infer a graph-wide limitation from one tool refusal. Distinguish
  unavailable MCP functionality from an impossible DB state.
- Conversely, do not claim capability because the UI renders a result. Verify
  the underlying attributes through an exact read.
- Label findings with the MCP server and Logseq build that produced them. Do
  not transfer behavior from the legacy `mcp-logseq` server to this one.
- Communicate the boundary early: this server supports queries, metadata
  writes, page/top-level-block operations, nested creation, subtree movement,
  and subtree deletion. `before` placement is unavailable.
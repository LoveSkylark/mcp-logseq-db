---
name: logseq-db-native
description: "Use when reading or modifying a Logseq 2.x DB graph through the mcp-logseq-db server. Covers DB queries, exact property and tag identities, verified metadata writes, destructive-operation safeguards, and timeout recovery. Never use file-graph or non-DB Logseq tools."
---

# Logseq DB-Native MCP

**Verified provenance:** Logseq 2.0.1 DB graph, `mcp-logseq-db` 0.2.5,
live-tested 2026-09-01. Revalidate version-sensitive claims after changing
either Logseq or the MCP server.

Use this skill only with the `mcp-logseq-db` server and a Logseq 2.x DB graph.
This server is intentionally narrow. It exposes verified `db_*` MCP tools
backed exclusively by the `logseq.DB.*` API namespace.

Do not load the legacy `logseq-db-graph` or `logseq-file-graph` skill in the
same conversation.

## Hard boundaries

- Before doing any graph work, inspect the available connector and tools. The
  connector must be `mcp-logseq-db` and must expose the `db_*` inventory below.
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
- The promoted safe path cannot create nested blocks or remove blocks. With
  experimental writes enabled, guarded insert/delete tools may complete after
  an HTTP timeout and prove the result through exact read-back.
- Tags can be renamed and permanently deleted by exact UUID. Unlike page
  deletion, tag deletion does not use the recycle bin.
- File operations and callback subscriptions are unavailable.

## Start every workflow with capabilities

Call `db_capabilities` once near the start of a conversation. Treat its
reported read methods as probes of the connected Logseq process. Supported
write methods come from the server's dated live-verification manifest; they
are not re-probed on startup because doing so would mutate the graph. Candidate
methods have not passed complete read-back testing and must not be called.

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
- `experimental_mcp_write_tools` lists guarded user-facing operations when the
  experimental gate is enabled.
- `candidate_write_operations` are allowed internally for further controlled
  testing but are not MCP capabilities. Do not call or advertise them.
- `unavailable_over_http` methods are rejected for this server/build and must
  not be retried.
- `rejected_operations` are bound aliases that failed timeout, response-shape,
  or read-back testing. Do not call them directly.
- `experimental_operations` are exposed only through guarded MCP tools. They
  may time out or make no change. Use them only when safe tools cannot express
  the request and the user accepts the risk. They are absent unless
  `LOGSEQ_ENABLE_EXPERIMENTAL_WRITES=true`.

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

This MCP supports promoted top-level creation and guarded experimental nested
creation. The model still matters when reading query results. A nested node whose `:block/page`
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
- Tags: `db_get_tag` accepts exact ident, UUID, or resolvable title.
  `db_get_tags_by_name` follows Logseq's normalized internal-name lookup and a
  display title may not resolve for every plugin-created tag. Prefer
  `db_get_all_tags`, then retain the returned ident and UUID. All tag mutations
  use exact tag UUIDs.
- The MCP accepts a property ident for both tag-property tools. For removal,
  the server resolves that ident to the property UUID required by Logseq.
- A bare property display name is rejected by this MCP before HTTP when an
  exact ident is required. Continue to pass full idents.
- Blocks/nodes: all metadata mutations use exact block UUIDs.
- Never select a destructive target from a fuzzy search result alone.
- Resolve the entity, show its exact identity, and validate its current state
  before removal.

`db_upsert_property` accepts a display title. Logseq may generate a
plugin-namespaced ident. Always retain the exact ident returned in
`verified_state`; use that ident for later reads and removal.
Generated idents vary by creation route and may include random suffixes. Never
construct or predict an ident from a display title.

## Read workflow

1. Call `db_capabilities`.
2. Use the narrowest structured reader available:
  - `db_list_pages` to discover pages and UUIDs.
   - `db_get_page_data` to read one page and its direct child blocks. It does
     not recursively include nested descendants. Missing nested blocks in this
     response are not evidence of deletion; use `db_get_block` or a Datascript
     parent/page query to inspect the complete hierarchy.
    - `db_get_block` to read one exact block UUID through Datascript.
    - `db_search` for text discovery.
    - `db_list_properties(expand=true)` for detailed property definitions.
    - `db_list_tags(expand=true)` for detailed tags/classes.
   - `db_get_all_properties` to discover property definitions.
   - `db_get_property` for one exact property ident.
   - `db_get_all_tags` to discover tags/classes.
   - `db_get_tag` for one exact tag identity.
   - `db_get_tags_by_name` for an exact title lookup.
   - `db_get_tag_objects` for nodes associated with a known tag. Its result is
     mixed and may contain both pages and blocks; distinguish pages by
     `:block/name`/`name`.
3. Use `db_q`, `db_custom_query`, or `db_datascript_query` only when the
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
operation: exact db_* MCP tool
requested state: exact typed value or relationship
reversibility: removal tool or explicit lack of one
verification: field and identity expected after the write
```

Ask for confirmation before:

- `db_remove_property`;
- permanently deleting a tag;
- removing a property/tag relationship that may affect inherited schemas; or
- changing metadata on multiple nodes.

## Page and block content

Use `db_upsert_nodes` for the supported DB content operations. The server
always runs Logseq's dry-run validation before a commit and then reads every
affected entity back.

For a single operation, prefer the explicit wrapper:

- `db_create_page(title)` creates one page.
- `db_create_top_level_block(page_uuid, title, tag_uuids)` creates one block
  directly under a page.
- `db_upsert_block(block_uuid, title)` edits one existing block title.

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
- Use `db_rename_page` with an exact page UUID.
- Use `db_upsert_block(block_uuid, title)` for a single existing block-title
  edit. It is an edit-only convenience wrapper over `db_upsert_nodes`; it does
  not create, move, nest, or delete a block. Set `dry_run=true` to validate
  without committing.
- `db_recycle_page` recycles an exact page UUID and verifies its
  `:logseq.property/deleted-at` marker. It does not permanently erase it.
- `db_delete_page` is retained only as a compatibility alias. Prefer
  `db_recycle_page` in plans and user-facing language.
- No promoted safe path exists for block deletion or nested insertion; use the
  guarded experimental tools below only with explicit risk acceptance.

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

### Experimental hierarchy and deletion

The following tools expose timeout-prone aliases because no safe batch
equivalent exists:

- `db_insert_block_experimental(target_uuid, title, placement)` supports
  `child`, `before`, and `after`. It generates a UUID before the call and reads
  that exact UUID back.
- `db_move_block_experimental(block_uuid, target_uuid, placement)` supports
  `child` and `before`, then verifies parent, owning page, relative order, and
  preservation of the complete subtree.
- `db_delete_block_experimental(block_uuid)` is destructive and may delete a
  subtree. Read the target and descendants first. On success,
  `verified_entities` is empty, `previous_entities` contains the pre-delete
  snapshot, and the diagnostic confirms exact UUID absence.

These tools send only the verified positional request shapes, including a
predetermined `customUUID` for insert, `children`/`before` placement options for
move, and the required empty options object for remove. Each alias mutation is
single-shot at the HTTP layer: a transport timeout is never retried.

They return `verified` and `diagnostic` in addition to the normal result
envelope. A completed MCP call is not proof of mutation:

- `verified=true`: the requested state was observed.
- `verified=false`: report that the operation did not complete; include the
  diagnostic and observed state. Do not retry automatically.
- `recovered_after_timeout=true`: the alias timed out and read-back determined
  the outcome.
- Unsupported placement values fail before HTTP and make no mutation.

## Property workflow

### Create or update a property

1. Call `db_get_all_properties` and check for an existing exact title/ident.
2. Choose a valid schema type: `date`, `number`, `checkbox`, `default`,
   `string`, `node`, `url`, `datetime`, `json`, or `asset`.
  Built-in definitions may display internal types such as `map`, `page`,
  `class`, or `property`; these are not accepted user-property creation types.
3. Call `db_upsert_property(title, schema, options)` once.
4. Retain the generated ident from `verified_state`.
5. Do not retry blindly if the tool reports an ambiguous timeout.

### Remove a property

1. Call `db_get_property` with the exact namespaced ident.
2. Confirm the returned ident exactly matches the requested target.
3. Explain that property removal is destructive.
4. Call `db_remove_property` only after confirmation.
5. The server verifies that `db_get_property` returns no entity afterward.
6. The server also verifies that no direct attribute use or property-created
  value entity remains. `previous_state` retains the removed definition and
  its pre-delete usage evidence.

### Block properties

- Use `db_upsert_block_property` with an exact page or block UUID and property
  ident. Despite its name, the tool can set properties on a page entity.
- Use `db_remove_block_property` with the same exact identities.
- Inspect the property schema before assigning a value.
- The server resolves property-value entities when needed and verifies the
  exact requested value, not merely attribute presence. Removal verifies exact
  absence.
- `number` values are stored in value entities under
  `:logseq.property/value`; `checkbox` values are stored as literals on the
  target entity. The verifier handles both forms. A tool error remains
  ambiguous until exact read-back; never retry blindly.

## Tag workflow

### Discover and create

- Use `db_get_all_tags`, `db_get_tag`, or `db_get_tags_by_name` before creating
  a tag.
- Call `db_create_tag` only when no existing exact tag is suitable.
- Retain the returned tag UUID and ident.
- Direct API creation commonly generates a plugin-namespaced ident and extends
  Root automatically. Read and retain the returned values; never construct the
  ident from the title.
- Use `db_rename_tag(tag_uuid, new_title)` to rename an exact tag.
- A rename changes title/name fields but leaves the generated ident unchanged.
  Treat the ident and UUID as durable identities; do not expect a renamed title
  to rewrite the ident or make the old display title a valid lookup.
- Use `db_delete_tag(tag_uuid)` only after explicit confirmation. It verifies
  that `db_get_tag` returns no entity and returns the deleted snapshot in
  `previous_state`. It also verifies that no `:block/tags` or `:block/refs`
  datoms still point to the deleted tag.

### Tag properties and inheritance

- `db_add_tag_property(tag_uuid, property_ident)` adds a property to a tag.
  It updates `:logseq.property.class/properties`; the property also appears in
  the tag's structural refs.
- `db_remove_tag_property(tag_uuid, property_ident)` removes it. The server
  resolves the property ident to the UUID form required by Logseq.
- `db_add_tag_extends(tag_uuid, parent_tag_uuid)` adds inheritance.
- `db_remove_tag_extends(tag_uuid, parent_tag_uuid)` removes inheritance.
- On the tested build, adding an extension replaced the existing Root parent
  rather than appending another parent. Read the child's
  `:logseq.property.class/extends` before and after the write, preserve the
  prior parent, and explain the replacement risk to the user.
- Removing that extension restored Root in the verified test. Read back rather
  than assuming restoration on another build.

### Tagging a page or block

- `db_add_block_tag(block_uuid, tag_uuid)` accepts either a page UUID or block
  UUID and adds a semantic DB tag. For pages, the tag is added alongside the
  built-in Page class.
- `db_remove_block_tag(block_uuid, tag_uuid)` removes it from either entity.
- `db_create_top_level_block(page_uuid, title, tag_uuids)` can apply tags in
  the same creation call.
- Do not insert `#tag` text as a substitute for changing `:block/tags`.

## Block icons

- `db_set_block_icon` accepts `icon_type` of `tabler-icon` or `emoji`.
- For `tabler-icon`, pass the Tabler ID such as `flask`.
- For `emoji`, pass the case-sensitive emoji-mart display name, such as
  `Test Tube` or `Books`. Do not pass a literal glyph (`🧪`), shortcode,
  lowercase ID (`test_tube`), or plural ID (`books`). Logseq resolves the
  display name and stores its normalized ID.
- `db_remove_block_icon` removes the icon from an exact block UUID.
- The server reads back `:logseq.property/icon` after both operations.

## Verification and timeout handling

Every exposed write returns an envelope containing `response`,
`verified_state`, and `recovered_after_timeout`. A successful HTTP status alone
is not considered success.

- `response` is often `null` for a successful Logseq mutation. Treat it as the
  raw API result, not verification evidence.
- `verified_state` is the server's post-write read-back. Claim success only
  when it contains the expected attribute or relationship.
- For `db_remove_property`, both `response` and `verified_state` are `null`
  after verified absence. For relationship/icon removals, `verified_state`
  normally contains the surviving entity without the removed value.
- `recovered_after_timeout=true` means the original response was ambiguous but
  read-back established the resulting state. Mention that recovery explicitly.
- A timed-out write may have committed. Never immediately repeat it.
- Read the exact target with a fresh tool call and reconcile observed state.
- If creation timed out before Logseq returned a generated ident, stop and
  inspect the property/tag listings for a uniquely matching title.
- One failed, malformed, cancelled, or timed-out request must not prevent a
  later normal request. Report repeated failures rather than issuing a loop.
- The server serializes writes across concurrent MCP calls so mutation and
  read-back cannot interleave with another write.
- Read-only transport failures may be retried with a fresh connection. Writes
  are never retried automatically.
- Read-back is polled for a bounded number of attempts to tolerate delayed
  visibility; polling repeats only reads, never writes.
- Responses exceeding the configured byte limit are rejected rather than sent
  unbounded into the MCP conversation.
- If `db_check_current_is_db_graph` or another trivial read times out after a
  query timeout, restart Logseq itself to clear its DB worker, then restart the
  MCP connection. Restarting only the MCP relay cannot clear a wedged worker.
- Distinguish timeout from connection refusal: timeout suggests a stuck Logseq
  worker; connection refusal means Logseq's HTTP server is not listening.

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
  `updateBlock`, `removeBlock`, and `moveBlock` are bound `logseq.DB.*` aliases
  that timed out during live testing. `insertBlock`, `removeBlock`, and
  `moveBlock` are available only through guarded experimental tools. Use
  `db_upsert_nodes` for supported creation and block-title edits.
- `newBlockUUID` and `exportEdn` are bound, but are not needed by the current
  safe workflows. `importEdn` is a high-impact whole-graph operation and is not
  exposed. `insertBatchBlocks` (plural) is not bound.
- `db_insert_block_experimental` preserved both `:block/parent` and the
  owning-page `:block/page` reference in the 2026-09-01 live test. Treat a
  result as successful only when `verified=true` and both references match;
  timeout recovery without those checks is not success.
- Nested insertion was verified through depth 3 with parent links chained and
  every descendant's page pinned to the owning page.
- Experimental subtree moves preserve descendant membership and immediate
  parent links while updating owning-page references as needed.
- Experimental parent deletion verifies target and every pre-read descendant
  UUID are absent. Successful results place the complete pre-delete subtree in
  `previous_entities` and leave `verified_entities` empty.
- `db_get_block` returns `{found:false, block:null}` for a missing/deleted UUID;
  absence is not reported as a generic tool error.

## Tool inventory

Reads and capabilities:

- `db_capabilities`
- `db_check_current_is_db_graph`
- `db_get_app_info`
- `db_get_current_graph`
- `db_list_pages`
- `db_get_page_data`
- `db_search`
- `db_list_properties`
- `db_list_tags`
- `db_q`
- `db_custom_query`
- `db_datascript_query`
- `db_get_all_properties`
- `db_get_property`
- `db_get_all_tags`
- `db_get_tag`
- `db_get_tags_by_name`
- `db_get_tag_objects`
- `db_get_block`

Promoted writes:

- `db_upsert_nodes`
- `db_create_page`
- `db_create_top_level_block`
- `db_upsert_block`
- `db_rename_page`
- `db_delete_page`
- `db_recycle_page`
- `db_upsert_property`
- `db_remove_property`
- `db_create_tag`
- `db_rename_tag`
- `db_delete_tag`
- `db_add_tag_property`
- `db_remove_tag_property`
- `db_add_tag_extends`
- `db_remove_tag_extends`
- `db_upsert_block_property`
- `db_remove_block_property`
- `db_add_block_tag`
- `db_remove_block_tag`
- `db_set_block_icon`
- `db_remove_block_icon`

Experimental writes (registered only when enabled):

- `db_insert_block_experimental`
- `db_move_block_experimental`
- `db_delete_block_experimental`

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
- Communicate the boundary early: this server supports queries and metadata
  writes plus safe page/top-level-block operations, but not nested creation or
  block deletion.
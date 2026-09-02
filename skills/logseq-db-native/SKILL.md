---
name: logseq-db-native
description: "Use when reading or modifying a Logseq 2.x DB graph through the mcp-logseq-db server. Covers DB queries, exact property and tag identities, verified metadata writes, destructive-operation safeguards, and timeout recovery. Never use file-graph or non-DB Logseq tools."
---

# Logseq DB-Native MCP

**Latest live status:** Logseq 2.0.1 DB graph, `mcp-logseq-db` 0.2.8.

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
- `candidate_write_operations` are allowed internally for further controlled
  testing but are not MCP capabilities. Do not call or advertise them.
- `unavailable_over_http` methods are rejected for this server/build and must
  not be retried.
- `rejected_operations` are bound aliases that failed timeout, response-shape,
  or read-back testing. Do not call them directly.

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
- Blocks and pages: metadata mutations use exact UUIDs and the tool-specific
  target kind. Block tools reject page UUIDs; page tools reject block UUIDs.
- Never select a destructive target from a fuzzy search result alone.
- Resolve the entity, show its exact identity, and validate its current state
  before removal.

`upsert_property` accepts a display title. Logseq may generate a
plugin-namespaced ident. Always retain the exact ident returned in
`verified_state`; use that ident for later reads and removal.
Generated idents vary by creation route and may include random suffixes. Never
construct or predict an ident from a display title.

## Structuring data for Logseq DB

For deciding how to model new information (page vs. block vs. tag vs.
property, schema types, import order, common patterns), read
`reference/data-modeling.md` before planning the write. Skip it for a single
already-scoped read or write.

Quick rule: page for durable identity, property for filterable/queryable
values, tag for a shared type with inherited schema, block for narrative or
nested content. See the reference file for the full decision guide before
any multi-entity import or new schema design.

<!-- moved: Choose the right Logseq shape, Property modeling rules,
Delivery workflow for structured imports, Common modeling patterns, What
this MCP cannot safely model yet -> reference/data-modeling.md -->

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
3. Use `datascript_query` only when the structured readers cannot answer the
  question. `q` and `custom_query` are blocked because their tested result
  shapes were not reliable enough for the public tool contract.
4. Preserve `id`, `ident`, and `uuid` in the working plan. Do not reduce an
   entity to display text.

### Query discipline

- Queries are read-only discovery tools. Do not attempt transaction forms.
- Query predicates execute inside Logseq's DB worker. An expensive or invalid
  predicate can wedge that worker even when the MCP process is healthy.
- Query/search calls are single-attempt. Never automatically repeat a timed-out
  query against the same worker.
- `search` can return highlight markers as presentation text. Do not paste
  those markers into mutation inputs; read the exact page or block first and
  write only the intended title/content.
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

## Content, property, tag, and icon write workflows

For the detailed operation shapes and rules for page/block content
(`upsert_nodes` and its wrappers, references/backlinks, structural
insert/move/delete), property create/remove, tag create/rename/delete plus
inheritance, and block icons, read `reference/write-workflows.md` before
performing that kind of write. Skip it for read-only tasks.

<!-- moved: Page and block content, Page references and backlinks, Block
hierarchy and deletion, Property workflow, Tag workflow, Block icons
-> reference/write-workflows.md -->

## Verification, timeouts, and known issues

Every write result carries `verified`/`verified_state`, `previous_state`,
`recovered_after_timeout`, and (for content writes) `diagnostic`. Treat a
successful HTTP status alone as insufficient; `response` is often `null` even
on success. Never blindly retry a timed-out write.

Read `reference/troubleshooting.md` before reconciling an ambiguous result, a
timeout, a long UI wait, `write_circuit_open`, or a call that may not have
run — it has the full symptom/diagnosis table, write-access scope env vars,
and the list of known-unavailable/rejected raw methods. Not needed for an
ordinary successful call.

<!-- moved: Verification and timeout handling, Optional write access scopes,
Known unavailable or rejected behavior -> reference/troubleshooting.md -->

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
- `set_tag_parent`
- `remove_tag_extends`
- `upsert_block_property`
- `remove_block_property`
- `upsert_page_property`
- `remove_page_property`
- `add_block_tag`
- `remove_block_tag`
- `add_page_tag`
- `remove_page_tag`
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
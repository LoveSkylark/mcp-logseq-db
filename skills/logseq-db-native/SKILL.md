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

Use the DB graph model, not file-graph Markdown conventions, when deciding how
to deliver information into Logseq. In DB graphs, pages and blocks are both
nodes. Tags behave like flexible types/classes for nodes. Properties are typed
values attached to nodes or inherited through tags. The MCP exposes only the
safe subset of this model listed in the tool inventory; do not invent missing
bulk import, template, view, namespace, asset, or file-write tools.

### Choose the right Logseq shape

- If you will navigate to it directly, make it a page.
- If you will filter, sort, group, or query by it, make it a property.
- If you will group nodes by shared type or inherited schema, use a tag.
- Use the NewTag "is-a" test before creating or applying a tag: a node tagged
  `#Person` should be a person, a node tagged `#Meeting` should be a meeting,
  and a node tagged `#Project` should be a project. If the phrase sounds wrong,
  use a property or wikilink instead.
- Use a tag when you would naturally want a collection/table of similar nodes.
  In Logseq DB terms, tagged nodes are rows, tag properties are columns, and the
  tag page becomes the place to review that collection.
- Use tag properties as the schema for that collection. For `#Person`, fields
  such as `email`, `phone`, or `organization` belong on the tag; for
  `#Meeting`, fields such as `date`, `project`, and `participants` belong on
  the tag.
- Use parent tags only for real inheritance. A child tag should inherit useful
  fields from the parent, not merely sit under it in a visual taxonomy. For
  example, `#Interview` may extend `#Meeting` if interviews should have meeting
  fields plus extra interview fields.
- If it is a relationship between two specific things inside prose or outline
  context, use an inline wikilink with the target page UUID.
- If it is a typed relationship that should appear as a table column, filter, or
  repeated field, use a `node` property instead of only a wikilink.
- Use a plain wikilink for loose topical association. The Logseq forum guidance
  is clear that tags and page links are no longer interchangeable in DB graphs:
  if two notes merely share a topic, link to the topic page instead of creating
  or applying another tag.
- Keep tags minimal. Do not put multiple tags on the same node when one tag plus
  properties expresses the same meaning more clearly.
- Do not tag everything just because it belongs to a broad category. Add a tag
  only when it creates useful grouping, inherited properties, table views, or
  retrieval value.
- Prefer one strong tag plus several typed properties over many overlapping
  tags. For example, use `#Meeting` with `project`, `date`, and `status`
  properties instead of tags like `#Meeting`, `#ProjectX`, `#September`, and
  `#Open` on the same block.
- Put changing state in properties, not tags. Status, priority, due dates,
  ratings, counts, and booleans are property-shaped.
- Put durable identity in pages, not properties. A person, organization,
  project, book, source, or durable concept usually deserves a page if you will
  link to it from multiple places.
- Put narrative detail in blocks, not properties. Properties should stay compact
  enough to work as table/filter fields; long notes, quotes, evidence, and
  explanations belong in block text or child blocks.
- Use nested blocks when order or containment carries meaning. Do not flatten a
  hierarchy into many sibling blocks unless the hierarchy is irrelevant.
- Avoid encoding structured facts only in titles. Titles are for readable names;
  properties are for values Claude or Logseq should query reliably.
- Reuse existing properties and tags before creating new ones. Avoid near-
  duplicates such as `Status`, `State`, and `Progress` unless they represent
  genuinely different concepts.
- Prefer properties over tags for categories within a type. For example, a
  `category` or `flag` property on `#Project` is usually better than separate
  tags like `#TeachingProject`, `#ResearchProject`, and `#WritingProject` unless
  each subtype needs distinct inherited fields.
- For tasks, questions, cards, assets, templates, journals, code, quotes, and
  similar Logseq-native concepts, remember that DB Logseq models many features
  as tags plus properties. Use the built-in tag/property model when it exists
  instead of recreating the concept with title prefixes.
- When uncertain, create less structure first: page or block plus a clear title,
  then add tags/properties only where they support a real workflow.

Tool-specific shape guidance:

- Use a page when the thing needs a stable top-level identity, linked
  references, or its own page view: a project, person, source, meeting, area,
  or durable concept. Create it with `create_page` or `upsert_nodes`.
- Use a top-level block when the thing is an item inside a page timeline or
  outline: a note, event, observation, task-like item, quote, or imported row
  whose home is an existing page. Create it with `create_top_level_block`.
- Use nested blocks when order, context, or decomposition matters more than
  independent identity: paragraphs under a meeting, checklist items under a
  task, evidence under a claim, or substeps under a procedure. Create them with
  `insert_block` after the parent/root block exists.
- Use tags when nodes share a type and should appear together in tag tables or
  inherit the same properties: `#Person`, `#Project`, `#Meeting`, `#Source`,
  `#Decision`, or `#Task`. Create tags with `create_tag`; attach them with
  `add_block_tag`, `remove_block_tag`, `add_page_tag`, or `remove_page_tag`.
- Use tag properties for fields every node of a type should expose. Create the
  property with `upsert_property`, then attach it to the tag with
  `add_tag_property`. Remember that changing tag properties changes the schema
  shown on every tagged node.
- Use direct node properties for values that are specific to one page or block.
  Write page fields with `upsert_page_property` and block fields with
  `upsert_block_property`. Keep shared, type-level fields on tags with
  `add_tag_property`.

### Property modeling rules

- Prefer `default` or `string`/Text for free text. Text values can behave like
  nodes in the app, but this MCP verifies them as typed property values.
- Use `number` for quantities. DB graphs store numbers as numbers, so tables
  and queries sort/filter numerically.
- Use `checkbox` for true/false state. Do not encode booleans as `"yes"`,
  `"no"`, `TODO`, or Markdown checkbox text.
- Use `date` or `datetime` for calendar values. Date values link to journals in
  Logseq's DB model; do not store dates only inside titles when they need to be
  queried or table-filtered.
- Use `url` for links that should be validated and displayed as URL values.
- Use `node` for relationships to other pages or blocks. Resolve the target
  first and pass the value shape that has been verified for the property route;
  do not guess from a title string.
- Avoid property choices unless they have been verified for this MCP build.
  `addPropertyValueChoices` remains a candidate because its effect was not
  observable through the available property reader.

### Delivery workflow for structured imports

1. Identify entity types before writing. Make a small schema plan such as:
   pages for durable subjects, tags for types, tag properties for common
   fields, blocks for observations/events, and nested blocks for details.
2. Call `capabilities`, then read existing pages, tags, and properties with
   `list_pages`, `get_all_tags`, `get_all_properties`, `get_tag`, and
   `get_property`. Reuse exact UUIDs and property idents when they already
   exist.
3. Create missing properties first with `upsert_property`. Keep every returned
   ident from `verified_state`; future property calls must use the ident, not
   the display name.
4. Create missing tags with `create_tag`. If a tag should inherit from another
   tag, call `set_tag_parent` and use `acknowledge_replacement=true` only after
   showing the previous parent state.
5. Attach common properties to tags with `add_tag_property`. This models a DB
   table/type better than repeating the same property setup manually on every
   node.
6. Create pages and top-level blocks with `upsert_nodes` when batching helps, or
   the explicit wrappers when doing one item at a time. Use `dry_run=true` for
   larger imports before committing.
7. Add nested structure with `insert_block` after parent blocks exist. Do not
   try to express nested children in `upsert_nodes`; this MCP intentionally
   supports only page creation, top-level block creation, and block-title edits
   through that route.
8. Add tags after creation with the page/block-specific tools. Use
   `add_page_tag` for pages and `add_block_tag` for blocks. Do not write `#tag`
   text as a substitute for structural tagging.
9. Write per-node fields with `upsert_page_property` for pages or
  `upsert_block_property` for blocks. Keep value types aligned with the
  property definition, then read back the node or query the property datom
  before claiming success.
10. For references, prefer exact UUID bracket links such as
    `[[TARGET_PAGE_UUID]]` in block titles when using this MCP. Title links may
    render in Logseq but did not create verified `:block/refs` on the tested
    write path.

### Common modeling patterns

- CRM/contact data: create `Person`, `Organization`, and `Interaction` tags;
  attach common fields as tag properties; create each contact as a page tagged
  `Person`; record calls or notes as blocks under a CRM or journal page tagged
  `Interaction`.
- Research notes: create pages for sources and durable concepts; use tags such
  as `Source`, `Claim`, `Evidence`, and `Question`; store excerpts or findings
  as blocks with nested evidence/details; link to source pages by exact UUID.
- Projects: create a page per project, a `Project` tag, and tags such as
  `Decision`, `Risk`, `Task`, or `Milestone`; keep project events as top-level
  or nested blocks under the project page rather than encoding everything in
  the page title.
- Meetings: create a meeting page or meeting block, tag it `Meeting`, use
  nested blocks for agenda/notes/actions, and use typed date/status properties
  instead of textual prefixes when the data should be queried.

### What this MCP cannot safely model yet

- It cannot configure property choices, bidirectional properties, tag view
  layouts, table views, gallery/list views, templates, assets, namespaces, or
  Build EDN import/export.
- It cannot use Logseq's built-in MCP HTTP endpoint; this server talks to the
  authenticated `logseq.DB.*` API and selected graph-worker operations.
- It cannot make old file-graph syntax such as page-frontmatter,
  `property:: value`, or namespace path text behave like DB properties.

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
  Before mutation, the tool snapshots page-owned blocks and inbound
  `:block/refs`. If inbound references exist, it returns `verified=false`
  unless `acknowledge_reference_rewrite=true` is supplied, because Logseq can
  rewrite visible inbound page references during recycle.
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

1. Use `get_property` only when the conversation needs a visible confirmation
  snapshot before deletion. `remove_property` performs its own exact
  `getProperty` preflight and refuses missing or mismatched idents before
  mutation.
2. Confirm the exact namespaced ident with the user. Explain that property
  removal is destructive and removes the definition plus stored values.
3. Call `remove_property(property_ident)` only after confirmation. Do not pass a
  display title.
4. The server verifies that `get_property` returns no entity afterward.
5. The server also verifies that no direct attribute use or property-created
  value entity remains. `previous_state` retains the removed definition and
  its pre-delete usage evidence gathered by `remove_property` itself.

### Block properties

- Use `upsert_block_property` with an exact block UUID, exact property
  ident, typed value, and optional options object. Never pass a property display
  name. The raw verified shape is `[block_uuid, property_ident, value, options]`;
  the MCP supplies `{}` when options are omitted.
- `remove_block_property` remains available for cleanup of an existing
  value. Verify exact absence afterward.

### Page properties

- Use `upsert_page_property` with an exact page UUID, exact property ident,
  typed value, and optional options object. It uses Logseq's same DB property
  route as block properties, but validates that the target UUID is a page before
  mutation.
- Use `remove_page_property` to remove a property value from a page and verify
  exact absence afterward.
- Prefer tag properties when every page with a tag should expose the same field;
  prefer page properties for values specific to one page.

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
- Use `delete_tag(tag_uuid)` only after explicit confirmation. It permanently
  removes the tag, verifies that `get_tag` returns no entity, and returns the
  deleted snapshot in `previous_state`. It also verifies that no `:block/tags`
  or `:block/refs` datoms still point to the deleted tag. Deleting an in-use
  tag removes assignments/references graph-wide without deleting the tagged
  entities. If child tags extend the target tag, the tool refuses before
  mutation unless `acknowledge_child_reparent=true` is supplied because Logseq
  reparents those children.

### Tag properties and inheritance

- `add_tag_property(tag_uuid, property_ident)` adds a property to a tag.
  It updates `:logseq.property.class/properties`; the property also appears in
  the tag's structural refs.
- `remove_tag_property(tag_uuid, property_ident)` removes it. The server
  resolves the property ident to the UUID form required by Logseq.
- `remove_tag_extends(tag_uuid, parent_tag_uuid)` removes inheritance.
- `set_tag_parent(tag_uuid, parent_tag_uuid, acknowledge_replacement=false)`
  sets one parent through Logseq's `addTagExtends` route. If the child already
  has a different parent, the tool refuses before mutation unless replacement
  is explicitly acknowledged.
- `set_tag_parent` and `remove_tag_extends` require exact child and parent tag
  UUIDs. Do not pass titles or numeric ids.

### Tagging a page or block

- `create_top_level_block(page_uuid, title, tag_uuids)` can apply tags in
  the same creation call.
- `add_block_tag` and `remove_block_tag` require an exact block UUID and exact
  tag UUID. They reject page UUIDs before mutation. The MCP does not resolve
  display titles for these writes. These tools use the graph-worker path
  because it remained responsive when the equivalent DB HTTP aliases timed out
  in mixed write sequences.
- `add_page_tag` and `remove_page_tag` require an exact page UUID and exact tag
  UUID. They use the native DB tag route because the graph-worker block path is
  intentionally block-only.
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
  after verified absence. The tool also verifies no direct attribute use or
  property-created value entity remains. For relationship/icon removals,
  `verified_state` normally contains the surviving entity without the removed
  value. If read-back fails after a write, the error includes `previous_state`
  and `observed_state` when they are available.
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
- `upsert_block_property`, `set_block_icon`, and `set_tag_parent` can time out
  in a degraded write session but succeeded with exact read-back as the first
  write after a fresh Logseq restart. `set_tag_parent` routes through the raw
  `addTagExtends` alias.
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
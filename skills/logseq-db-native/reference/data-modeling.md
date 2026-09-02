# Data modeling reference

Load this file only when planning schema, imports, or how to structure new
information (pages vs. blocks vs. tags vs. properties). Not needed for a
single read or a single already-scoped write.

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

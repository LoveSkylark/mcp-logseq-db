# Write workflows reference

Load this file only when performing a content, property, tag, or icon
mutation. Not needed for read-only tasks.

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

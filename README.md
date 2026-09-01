# mcp-logseq-db

A narrow MCP server built specifically around the Logseq 2.x
`logseq.DB.*` HTTP API. It does not call `logseq.cli.*`, `logseq.App.*`,
`logseq.app.*`, or `logseq.Editor.*`.

## Current tools

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
- `db_upsert_nodes`
- `db_get_block`
- `db_create_page`
- `db_create_top_level_block`
- `db_upsert_block`
- `db_rename_page`
- `db_delete_page`
- `db_recycle_page` (preferred; `db_delete_page` is a compatibility alias)
- `db_insert_block_experimental`
- `db_move_block_experimental`
- `db_delete_block_experimental`

Every HTTP call uses a new client and sends `Connection: close`. Connect and
read timeouts are configured independently. Read-only transport failures may be
retried with fresh connections; writes are never retried. Writes are serialized
and their read-back is polled for bounded delayed visibility. Property values
are verified semantically, and removals verify exact absence.

`db_capabilities` distinguishes queryable entity types, metadata-mutable entity
types, content operations, candidates, unavailable methods, and bound but
rejected aliases. The server supports page and top-level block creation, block
title edits, page rename, and page recycle. Nested creation and block deletion
are exposed only through guarded experimental tools. Those aliases timed out
in some runs; guarded read-back distinguishes committed changes from no-ops.
The tools return `verified=false` with a diagnostic when the requested state is
not observed. They are not
registered unless `LOGSEQ_ENABLE_EXPERIMENTAL_WRITES=true`.

`db_get_page_data` returns only blocks directly parented by the page. Use
`db_get_block` or Datascript parent/page queries for nested descendants.

For structural page references in block titles, use `[[TARGET_PAGE_UUID]]`.
Title-based `[[Page Name]]` text does not create `:block/refs` on the tested
write path. Node-typed properties can also create refs when assigned the target
entity's numeric `db/id`.

Tag and hierarchy details verified on Logseq 2.0.1:

- `db_add_block_tag` and `db_remove_block_tag` accept either a page UUID or
	block UUID.
- `db_get_tag_objects` returns a mixed collection of pages and blocks.
- Tag rename changes display/name fields but keeps the generated ident stable.
- Tag deletion removes tag/ref relationships without deleting tagged entities.
- Experimental nested insertion preserved owning-page references through depth
	3, and experimental moves preserved complete subtrees.
- Experimental parent deletion cascades to descendants and verifies every
	pre-read subtree UUID is absent.
- `db_get_block` returns `found=false` for missing or deleted UUIDs.

## Setup

```powershell
cd mcp-logseq-db
python -m pip install -e ".[dev]"
$env:LOGSEQ_API_TOKEN = "your-token"
python -m mcp_logseq_db.server
```

Environment variables:

| Variable | Default |
| --- | --- |
| `LOGSEQ_API_TOKEN` | Required |
| `LOGSEQ_API_URL` | `http://127.0.0.1:12315` |
| `LOGSEQ_API_CONNECT_TIMEOUT` | `3` seconds |
| `LOGSEQ_API_READ_TIMEOUT` | `15` seconds |
| `LOGSEQ_VERIFY_SSL` | `true` |
| `LOGSEQ_READ_ATTEMPTS` | `2` for read-only transport failures |
| `LOGSEQ_READBACK_ATTEMPTS` | `3` |
| `LOGSEQ_READBACK_DELAY` | `0.15` seconds |
| `LOGSEQ_ENABLE_EXPERIMENTAL_WRITES` | `false` |
| `LOGSEQ_WRITE_TITLE_PREFIXES` | Unrestricted when empty |
| `LOGSEQ_WRITE_PROPERTY_PREFIXES` | Unrestricted when empty |
| `LOGSEQ_WRITE_ENTITY_UUIDS` | Unrestricted when empty |
| `LOGSEQ_MAX_RESPONSE_BYTES` | `5000000` |

The workspace `.vscode/mcp.json` prompts for the token and starts the same
stdio server without storing the token.

## Claude Desktop skill

Import `dist/logseq-db-native.zip` through Claude Desktop Skills. The editable
skill source is in `skills/logseq-db-native/`. Use this skill only with the
`mcp-logseq-db` connector; do not enable the legacy DB/file graph skills in the
same conversation.

## Live verification baseline

Tested against the local Logseq 2.0.1 DB graph on 2026-09-01:

| Method | Result |
| --- | --- |
| `logseq.DB.getAllProperties` | HTTP 200; bare-field property array |
| `logseq.DB.getProperty` | HTTP 200; exact property ident required |
| `logseq.DB.getAllTags` | HTTP 200; bare-field tag array |
| `logseq.DB.getTag` | HTTP 200 for ident, UUID, and exact title |
| `logseq.DB.getTagsByName` | HTTP 200; exact title returns an array |
| `logseq.DB.getTagObjects` | HTTP 200; no positive instances in test graph |
| `logseq.DB.q` | HTTP 200 |
| `logseq.DB.customQuery` | HTTP 200 |
| `logseq.DB.datascriptQuery` | HTTP 200 |
| `logseq.DB.listPages` | HTTP 200; DB namespace alias verified |
| `logseq.DB.listTags` | HTTP 200; DB namespace alias verified |
| `logseq.DB.listProperties` | HTTP 200; DB namespace alias verified |
| `logseq.DB.getPageData` | Bound; missing page returns a specific error |
| `logseq.DB.search` | HTTP 200; DB namespace alias verified |
| `logseq.DB.checkCurrentIsDbGraph` | HTTP 200; DB namespace alias verified |
| `logseq.DB.upsertNodes` | Dry-run, page/top-level block creation, and block edit verified |
| `logseq.DB.renamePage` | HTTP 200 and read-back verified |
| `logseq.DB.deletePage` | HTTP 200; recycle marker verified |
| `logseq.DB.moveBlock` | Timed out with no change; rejected |
| `logseq.DB.newBlockUUID` | HTTP 200; redundant with current batch workflow |
| `logseq.DB.exportEdn` | Bound; not exposed due unbounded graph response |
| `logseq.DB.importEdn` | Bound; not exposed because it is a high-impact whole-graph operation |
| `logseq.DB.upsertProperty` | HTTP 200; `(title, schema, options)` |
| `logseq.DB.removeProperty` | HTTP 200; exact ident; absence verified |
| `logseq.DB.createTag` | HTTP 200; `(title, options)`; exact identity verified |
| `logseq.DB.addTagProperty` | HTTP 200; tag UUID and property ident; verified |
| `logseq.DB.removeTagProperty` | HTTP 200; tag UUID and property UUID; verified |
| `logseq.DB.addTagExtends` | HTTP 200; child and parent tag UUIDs; verified |
| `logseq.DB.removeTagExtends` | HTTP 200; child and parent tag UUIDs; verified |
| `logseq.DB.upsertBlockProperty` | HTTP 200; block UUID and property ident; verified |
| `logseq.DB.removeBlockProperty` | HTTP 200; block UUID and property ident; verified |
| `logseq.DB.addBlockTag` | HTTP 200; block and tag UUIDs; verified |
| `logseq.DB.removeBlockTag` | HTTP 200; block and tag UUIDs; verified |
| `logseq.DB.setBlockIcon` | HTTP 200; Tabler ID and case-sensitive emoji-mart display name verified |
| `logseq.DB.removeBlockIcon` | HTTP 200; absence verified |
| `logseq.DB.addPropertyValueChoices` | HTTP 200; effect not observable; not exposed |
| `logseq.DB.getFileContent` | HTTP 200/null for missing path; not exposed |
| `logseq.DB.getFavorites` | HTTP 500; blocked |
| `logseq.DB.setPropertyNodeTags` | Timed out; blocked |

The timed-out `setPropertyNodeTags` request was followed by successful normal
requests and exact cleanup without restarting Logseq or the test process.

All promoted writes also passed through MCP in one reversible end-to-end run.
Emoji names `Test Tube` and `Books` were verified; Logseq stores their normalized
IDs `test_tube` and `books`. The F4A2 malformed child/grandchild and tag were
removed. Its ten pages are recycled, so their blocks remain queryable, and the
user property `MCP Lab F4A2 Status` remains because all tested API removal forms
were no-ops.

Monitoring callbacks (`onChanged`, `onBlockChanged`) cannot be transported as
ordinary request/response HTTP calls and remain unavailable. Block, tag, icon,
and file operations not listed as tools remain unexposed until they pass
write/read-back/cleanup testing.

## Tests

```powershell
python -m pytest -q
```

With Logseq running and `LOGSEQ_API_TOKEN` set, run the non-destructive live
reliability sequence:

```powershell
python scripts/live_reliability.py
```

Datascript/custom-query predicates run inside Logseq's DB worker. Query and
search requests are never retried after a timeout. If a trivial health check
also times out afterward, restart Logseq itself before reconnecting the MCP;
restarting only the relay cannot clear a wedged Logseq worker.
# mcp-logseq-db

A narrow MCP server built specifically around Logseq 2.x DB graphs. Reads and
metadata use the authenticated `logseq.DB.*` HTTP API; structural block insert,
move, delete, and existing-block tag changes use Logseq's graph-worker CLI
outliner operations.

## Current tools

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
- `add_block_tag`
- `remove_block_tag`
- `add_page_tag`
- `remove_page_tag`
- `set_block_icon`
- `remove_block_icon`
- `upsert_nodes`
- `get_block`
- `get_block_tree`
- `create_page`
- `create_top_level_block`
- `insert_block`
- `delete_block`
- `move_block`
- `upsert_block`
- `rename_page`
- `delete_page`
- `recycle_page` (preferred; `delete_page` is a compatibility alias)

Every HTTP API call uses a new client and sends `Connection: close`. Connect and
read timeouts are configured independently. Read-only transport failures may be
retried with fresh connections; writes are never retried. Writes are serialized
and their read-back is polled for bounded delayed visibility. Property values
are verified semantically, and removals verify exact absence. When a metadata
write reaches Logseq but verification fails, the MCP error includes
`previous_state` and `observed_state` whenever the server could read them back.

`capabilities` distinguishes queryable entity types, metadata-mutable entity
types, content operations, candidates, unavailable methods, and bound but
rejected aliases. The server supports page and top-level block creation, block
title edits, page rename, page recycle, nested block creation, subtree movement,
and subtree deletion. Structural block operations use Logseq's graph-worker CLI
outliner operations rather than timeout-prone plugin aliases. Stable insert and
move support `child` and `after`; `before` placement is unavailable. The tools
return `verified=false` with a diagnostic when the requested state is not
observed. `q` and `custom_query` are intentionally blocked because the tested
result shapes were not useful enough for a safe public contract. No
experimental tools are registered.

`get_page_data` returns only blocks directly parented by the page. Use
`get_block` for one exact block or `get_block_tree` for a recursive
subtree. The tree reader uses one exact root lookup plus one owning-page query,
then assembles descendants locally. It defaults to depth 20 and 1,000 nodes and
returns `truncated=true` when either bound stops traversal.

For structural page references in block titles, use `[[TARGET_PAGE_UUID]]`.
Title-based `[[Page Name]]` text does not create `:block/refs` on the tested
write path. Node-typed properties can also create refs when assigned the target
entity's numeric `db/id`.

Tag and hierarchy details verified on Logseq 2.0.1:

- `get_tag_objects` returns a mixed collection of pages and blocks.
- Tag rename changes display/name fields but keeps the generated ident stable.
- Tag deletion permanently removes the tag and its tag/ref relationships without
  deleting tagged entities.
- `set_tag_parent` replaces the current parent only when explicitly
  acknowledged.
- Block tag tools are block-only. Page tag changes use `add_page_tag` and
  `remove_page_tag` through the native DB tag route.
- CLI graph-worker child insertion and movement both preserved parent and owning
	page in the 2026-09-02 live run.
- `get_block` returns `found=false` for missing or deleted UUIDs.
- `get_block_tree` returns nested `children` while excluding unrelated page
	siblings.

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
| `LOGSEQ_ENABLE_EXPERIMENTAL_WRITES` | Legacy compatibility setting; no current tools use it |
| `LOGSEQ_WRITE_TITLE_PREFIXES` | Unrestricted when empty |
| `LOGSEQ_WRITE_PROPERTY_PREFIXES` | Unrestricted when empty |
| `LOGSEQ_WRITE_ENTITY_UUIDS` | Unrestricted when empty |
| `LOGSEQ_MAX_RESPONSE_BYTES` | `5000000` |

Each HTTP attempt has a hard wall-clock deadline equal to
`LOGSEQ_API_CONNECT_TIMEOUT + LOGSEQ_API_READ_TIMEOUT`.

All write tools own their raw positional argument shapes. Callers provide only
named MCP parameters; exact UUIDs, property idents, placements, options, entity
kinds, and current target state are validated before mutation. If any write
times out, a process-local circuit breaker blocks every later write before HTTP
while leaving reads available for reconciliation. Restart Logseq and reconnect
the MCP before writing again. `capabilities` reports
`write_circuit_open` and `write_circuit_reason`. Query and search calls are
single-attempt and are never retried after a timeout.

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
| `logseq.DB.q` | HTTP 200; blocked because result projection was too limited |
| `logseq.DB.customQuery` | HTTP 200; blocked because result shape was not usable |
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
| `logseq.DB.moveBlock` | Timed out with no change; public `move_block` uses the graph-worker path |
| `logseq.DB.newBlockUUID` | HTTP 200; redundant with current batch workflow |
| `logseq.DB.exportEdn` | Bound; not exposed due unbounded graph response |
| `logseq.DB.importEdn` | Bound; not exposed because it is a high-impact whole-graph operation |
| `logseq.DB.upsertProperty` | HTTP 200; `(title, schema, options)` |
| `logseq.DB.removeProperty` | HTTP 200; exact ident; absence verified |
| `logseq.DB.createTag` | HTTP 200; `(title, options)`; exact identity verified |
| `logseq.DB.addTagProperty` | HTTP 200; tag UUID and property ident; verified |
| `logseq.DB.removeTagProperty` | HTTP 200; tag UUID and property UUID; verified |
| `logseq.DB.addTagExtends` | Exact two-UUID shape verified after fresh restart; exposed as `set_tag_parent` |
| `logseq.DB.removeTagExtends` | HTTP 200; child and parent tag UUIDs; verified |
| `logseq.DB.upsertBlockProperty` | Exact block UUID/ident/value/options shape verified after fresh restart |
| `logseq.DB.removeBlockProperty` | HTTP 200; block UUID and property ident; verified |
| `logseq.DB.addBlockTag` | Exact block/tag and page/tag UUID shapes verified after fresh restart |
| `logseq.DB.removeBlockTag` | Exact block/tag and page/tag UUID shapes verified after fresh restart |
| `logseq.DB.setBlockIcon` | Exact UUID/type/name shape verified after fresh restart |
| `logseq.DB.removeBlockIcon` | HTTP 200; absence verified |
| `logseq.DB.addPropertyValueChoices` | HTTP 200; effect not observable; not exposed |
| `logseq.DB.getFileContent` | HTTP 200/null for missing path; not exposed |
| `logseq.DB.getFavorites` | HTTP 500; blocked |
| `logseq.DB.setPropertyNodeTags` | Timed out; blocked |

The `setPropertyNodeTags` timeout is a historical observation. In the current
server, reads remain available after a write timeout, but every later write is
blocked until the MCP reconnects.

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

Datascript predicates run inside Logseq's DB worker. Query and search requests
are never retried after a timeout. If a trivial health check
also times out afterward, restart Logseq itself before reconnecting the MCP;
restarting only the relay cannot clear a wedged Logseq worker.

`search` may return highlight markers as presentation text. Do not paste those
markers into mutation inputs; read the exact page or block first and write only
the intended title/content.

`recycle_page` snapshots page-owned blocks and inbound `:block/refs` before it
mutates. If inbound references exist, it returns `verified=false` unless
`acknowledge_reference_rewrite=true` is supplied. Recycling marks the page with
`:logseq.property/deleted-at`; it does not guarantee that page-owned blocks are
erased from query results.
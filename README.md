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
- `upsert_page_property`
- `remove_page_property`
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
  deleting tagged entities. If child tags extend the deleted tag, `delete_tag`
  refuses until `acknowledge_child_reparent=true` is supplied.
- `set_tag_parent` replaces the current parent only when explicitly
  acknowledged.
- Block tag tools are block-only. Page tag changes use `add_page_tag` and
  `remove_page_tag` through the native DB tag route.
- Block and page property tools validate the target kind before calling the
  shared DB property route.
- CLI graph-worker child insertion and movement both preserved parent and owning
	page in the 2026-09-02 live run.
- `get_block` returns `found=false` for missing or deleted UUIDs.
- `get_block_tree` returns nested `children` while excluding unrelated page
	siblings.

## Install from GitHub

Install from the public repository with Python 3.11 or newer. On the Windows
Logseq host used for validation, Python 3.13 is used:

```powershell
git clone https://github.com/LoveSkylark/mcp-logseq-db.git
cd mcp-logseq-db
py -3.13 -m pip install --upgrade pip
py -3.13 -m pip install .
```

For editable development, install with test dependencies:

```powershell
git clone https://github.com/LoveSkylark/mcp-logseq-db.git
cd mcp-logseq-db
py -3.13 -m pip install -e ".[dev]"
```

To install a local release wheel after building it:

```powershell
py -3.13 -m pip wheel --no-deps . -w dist
py -3.13 -m pip install --force-reinstall --no-deps .\dist\mcp_logseq_db-0.2.8-py3-none-any.whl
```

Run the server directly for a smoke test:

```powershell
$env:LOGSEQ_API_TOKEN = "your-token"
py -3.13 -m mcp_logseq_db.server
```

Or, after installation, use the console script:

```powershell
$env:LOGSEQ_API_TOKEN = "your-token"
mcp-logseq-db
```

### macOS install

Install Python 3.11 or newer first. With Homebrew:

```zsh
brew install python git
git clone https://github.com/LoveSkylark/mcp-logseq-db.git
cd mcp-logseq-db
python3 -m pip install --upgrade pip
python3 -m pip install .
```

For editable development on macOS:

```zsh
git clone https://github.com/LoveSkylark/mcp-logseq-db.git
cd mcp-logseq-db
python3 -m pip install -e ".[dev]"
```

Run a smoke test from Terminal:

```zsh
export LOGSEQ_API_TOKEN="your-token"
export LOGSEQ_API_URL="http://127.0.0.1:12315"
python3 -m mcp_logseq_db.server
```

If the console script is on your `PATH`, this is equivalent:

```zsh
export LOGSEQ_API_TOKEN="your-token"
mcp-logseq-db
```

To find the installed executable path for MCP client configuration:

```zsh
python3 -m site --user-base
which mcp-logseq-db
```

Depending on how Python was installed, the command path is commonly one of:

```text
/opt/homebrew/bin/mcp-logseq-db
/usr/local/bin/mcp-logseq-db
~/Library/Python/3.x/bin/mcp-logseq-db
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

## Connect to Claude Desktop

1. Enable the Logseq HTTP API server in Logseq Desktop and copy its API token.
2. Install `mcp-logseq-db` into the same local user account that runs Claude
  Desktop.
3. Edit Claude Desktop's MCP configuration file:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

Use either the installed console script:

```json
{
  "mcpServers": {
    "mcp-logseq-db": {
      "command": "C:\\Users\\YOUR_USER\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\mcp-logseq-db.exe",
      "env": {
        "LOGSEQ_API_TOKEN": "paste-your-logseq-api-token-here",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

Or run it through the Python launcher:

```json
{
  "mcpServers": {
    "mcp-logseq-db": {
      "command": "py",
      "args": ["-3.13", "-m", "mcp_logseq_db.server"],
      "env": {
        "LOGSEQ_API_TOKEN": "paste-your-logseq-api-token-here",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

Restart Claude Desktop after saving the file. In a new conversation, ask Claude
to call `capabilities`; a healthy connection should report Logseq DB support and
the `mcp-logseq-db` tool inventory.

On macOS, Claude Desktop uses this configuration file:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Example macOS configuration using the Python module entry point:

```json
{
  "mcpServers": {
    "mcp-logseq-db": {
      "command": "python3",
      "args": ["-m", "mcp_logseq_db.server"],
      "env": {
        "LOGSEQ_API_TOKEN": "paste-your-logseq-api-token-here",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

If `which mcp-logseq-db` prints a stable path, you can use the installed command
instead:

```json
{
  "mcpServers": {
    "mcp-logseq-db": {
      "command": "/opt/homebrew/bin/mcp-logseq-db",
      "env": {
        "LOGSEQ_API_TOKEN": "paste-your-logseq-api-token-here",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

Do not enable this connector together with legacy Logseq file-graph or non-DB
MCP servers in the same conversation. The tools intentionally target Logseq 2.x
DB graphs and exact DB identifiers.

## Connect to Claude Code with uv

Claude Code can add a local stdio MCP server from the command line. This is the
closest form to one-line MCP install snippets that use `uv` or `npx`.

If you have `uv` installed, run this from the project where you want Claude Code
to use Logseq:

```bash
claude mcp add mcp-logseq-db \
  --env LOGSEQ_API_TOKEN=your_token_here \
  --env LOGSEQ_API_URL=http://127.0.0.1:12315 \
  -- uvx --from git+https://github.com/LoveSkylark/mcp-logseq-db.git mcp-logseq-db
```

The equivalent `uv run --with` form is:

```bash
claude mcp add mcp-logseq-db \
  --env LOGSEQ_API_TOKEN=your_token_here \
  --env LOGSEQ_API_URL=http://127.0.0.1:12315 \
  -- uv run --with "mcp-logseq-db @ git+https://github.com/LoveSkylark/mcp-logseq-db.git" mcp-logseq-db
```

After adding it, check the connection:

```bash
claude mcp list
claude mcp get mcp-logseq-db
```

Inside Claude Code, open `/mcp` and reconnect if the server was already cached
or failed before Logseq was running.

Use `--scope user` if you want the server available across all Claude Code
projects, or `--scope project` if you want Claude Code to write a shareable
`.mcp.json` entry for the current repository. Keep real tokens out of committed
project files.

This command configures Claude Code, not Claude Desktop. Claude Desktop still
uses `claude_desktop_config.json` or an imported connector configuration.

## Publish to PyPI

Publishing to PyPI registers the `mcp-logseq-db` package name so users can run
the short install command:

```bash
uvx mcp-logseq-db
```

and the short Claude Code MCP command:

```bash
claude mcp add mcp-logseq-db \
  --env LOGSEQ_API_TOKEN=your_token_here \
  --env LOGSEQ_API_URL=http://127.0.0.1:12315 \
  -- uv run --with mcp-logseq-db mcp-logseq-db
```

Before the first upload, create accounts at both package indexes:

- TestPyPI: `https://test.pypi.org/account/register/`
- PyPI: `https://pypi.org/account/register/`

Verify your email address and enable two-factor authentication. Check that the
name is available before uploading:

```powershell
py -3.13 -m pip index versions mcp-logseq-db
```

If the package does not exist yet, the first successful upload creates the PyPI
project. Build both a wheel and source distribution from a clean checkout:

```powershell
Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
py -3.13 -m pip install --upgrade build twine
py -3.13 -m build
py -3.13 -m twine check dist\*
```

Do a TestPyPI upload first:

```powershell
py -3.13 -m twine upload --repository testpypi dist\*
```

When prompted, use:

```text
username: __token__
password: pypi-your-testpypi-token
```

Then test the package from TestPyPI in a fresh environment. Dependencies are
resolved from real PyPI because TestPyPI may not mirror them all:

```powershell
py -3.13 -m venv .venv-testpypi
.\.venv-testpypi\Scripts\Activate.ps1
python -m pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple mcp-logseq-db
python -m pip show mcp-logseq-db
python -c "from importlib.metadata import version; print(version('mcp-logseq-db'))"
deactivate
```

Publish to real PyPI only after the TestPyPI package installs cleanly:

```powershell
py -3.13 -m twine upload dist\*
```

Use the real PyPI API token for this upload:

```text
username: __token__
password: pypi-your-real-pypi-token
```

For long-term releases, prefer PyPI Trusted Publishing from GitHub Actions over
long-lived API tokens. On PyPI, add a trusted publisher for:

```text
Owner: LoveSkylark
Repository: mcp-logseq-db
Workflow name: publish.yml
Environment name: pypi
```

Then a GitHub Actions workflow can build and publish with short-lived OIDC
credentials instead of storing a PyPI token. The manual `twine upload` path above
is simpler for the first local release.

After publishing, verify the public install path:

```powershell
py -3.13 -m venv .venv-pypi
.\.venv-pypi\Scripts\Activate.ps1
python -m pip install mcp-logseq-db
python -m pip show mcp-logseq-db
python -c "from importlib.metadata import version; print(version('mcp-logseq-db'))"
deactivate
```

`mcp-logseq-db` itself is a stdio MCP server command. Run it from an MCP client
configuration, not as an interactive help command.

## Connect to GitHub Copilot in VS Code

GitHub Copilot Chat in VS Code can use MCP servers from a workspace or user MCP
configuration. Create `.vscode/mcp.json` in your checkout if you want this MCP
available only for the workspace:

```json
{
  "inputs": [
    {
      "id": "logseq-api-token",
      "type": "promptString",
      "description": "Logseq API token",
      "password": true
    }
  ],
  "servers": {
    "mcp-logseq-db": {
      "type": "stdio",
      "command": "py",
      "args": ["-3.13", "-m", "mcp_logseq_db.server"],
      "env": {
        "LOGSEQ_API_TOKEN": "${input:logseq-api-token}",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

On macOS, the VS Code workspace configuration can use `python3` as the command:

```json
{
  "inputs": [
    {
      "id": "logseq-api-token",
      "type": "promptString",
      "description": "Logseq API token",
      "password": true
    }
  ],
  "servers": {
    "mcp-logseq-db": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "mcp_logseq_db.server"],
      "env": {
        "LOGSEQ_API_TOKEN": "${input:logseq-api-token}",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

For a machine-wide setup, add the same server definition to VS Code's user MCP
configuration instead of the workspace file. Restart or reload VS Code, then use
Copilot Chat's MCP tools view to enable `mcp-logseq-db` and run `capabilities`.

## Deploy the Claude Skill

The Claude Skill gives Claude operational guidance for using this MCP safely. It
does not contain the Logseq API token; the token belongs only in the local MCP
server configuration.

Use the packaged skill:

```text
dist/logseq-db-native.zip
```

In Claude Desktop, open Settings, go to Skills, import the zip, then enable the
`logseq-db-native` skill in conversations where the `mcp-logseq-db` connector is
available. The editable skill source is in `skills/logseq-db-native/`.

After editing the skill source, rebuild the zip from PowerShell:

```powershell
Compress-Archive -Path .\skills\logseq-db-native -DestinationPath .\dist\logseq-db-native.zip -Force
```

## GPT-style alternative to Claude Skills

ChatGPT/GPT clients do not use Claude Skill zip files. Use the same knowledge as
plain instructions instead:

- Create a Custom GPT or project/workspace instruction set named
  `logseq-db-native`.
- Paste the contents of `skills/logseq-db-native/SKILL.md` into the instruction
  or knowledge area.
- If the GPT client supports MCP connectors, configure the same stdio command
  and environment variables shown above.
- If the GPT client does not support local MCP, it can still use the skill text
  as guidance, but it cannot call `mcp-logseq-db` tools directly.

Keep the MCP server configuration and API token local. Do not paste
`LOGSEQ_API_TOKEN` into model instructions, skill text, repository files, or
Custom GPT knowledge.

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
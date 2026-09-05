# mcp-logseq-db

An MCP server for Logseq 2.x **DB** graphs. Reads and writes go through the
authenticated `logseq.DB.*` HTTP API; nothing here touches file graphs.

## The thing to know before using it

**This API returns success for calls that do nothing.** A wrong identifier
type, a name where a UUID belongs, or an unsupported combination produces
`null` or `{:block 1}` — indistinguishable from a successful write.

So every write in this server is followed by a read-back, and every result
carries `verified`. A `verified=false` result means the write did not take
effect even though no error was raised. **Treat the response as evidence of
nothing; only the read-back counts.**

That single fact shapes the rest of the design: identifiers are validated at
the boundary, `capabilities` reports three states rather than two, and there is
a separate live script that checks the server's assumptions about Logseq are
still true.

## Tools

**Reads**

`capabilities` · `getPageUUID` · `getPage` · `getBlockUUID` · `getBlock` ·
`getBlockTree` · `getTagUUID` · `getTag` · `getTagUsers` · `getPropertyIndent`
· `getProperyUsers`

**Lists** — no arguments, each returns a whole kind

`listPages` · `listJournals` · `listTags` · `listProperties` ·
`listClosedValues` · `listOrphanTags` · `listOrphanProperties` · `listAssets` ·
`listStatus` · `listRecycled`

**Writes** — each verifies by read-back

`createBlock` · `createManyBlocks` · `createPageofBlocks` · `updateBlock` ·
`removeBlock` · `creatTag` · `deleteTag` · `addTag` · `removeTag` ·
`createProperty` · `deleteProperty` · `addProperty` · `removeProperty`

There is one `addTag`, not an `addPageTag` and an `addBlockTag` — a page **is**
a block in the DB, so the target is uniform and there is nothing to choose
between. The same applies to `addProperty`.

`getPage` takes a `detail` selector: `page`, `blocks`, `tags`, `properties`,
`declared`, or `all`. These are not interchangeable. A page's own tags and its
blocks' tags live in different places, and properties that a page *declares*
through its classes have no datoms at all — they appear in no other query.

## Limits worth knowing up front

**Property writes are sandboxed.** Only `plugin.property.<caller-id>/*` is
writable. Properties created in the Logseq UI live under `user.property/*` and
are readable but not writable, as are built-ins under `:logseq.property/`. This
is Logseq's restriction, not this server's.

**No page create, rename, or delete tool.** All three routes exist and none is
exposed.

**No block move.** No route has been found for it at all.

**`deleteTag` and `deleteProperty` are unverified.** Both are destructive and
neither has been confirmed working. Check `verified` in the result.

**Recycled pages survive deletion**, keeping their UUID, tags, and blocks, so
`listPages` excludes them explicitly and `listRecycled` shows them. Inbound
references are not rewritten.

## Install

Python 3.11 or newer.

```bash
git clone https://github.com/LoveSkylark/mcp-logseq-db.git
cd mcp-logseq-db
pip install .
```

Development, with test dependencies:

```bash
pip install -e ".[dev]"
```

On Windows substitute `py -3.13 -m pip`. A local wheel:

```powershell
py -3.13 -m pip wheel --no-deps . -w dist
py -3.13 -m pip install --force-reinstall --no-deps .\dist\mcp_logseq_db-0.3.0-py3-none-any.whl
```

Smoke test — enable **Settings → Features → HTTP APIs server** in Logseq first
and copy its token:

```bash
export LOGSEQ_API_TOKEN="your-token"
python -m mcp_logseq_db.server
```

## Configuration

| Variable | Default |
| --- | --- |
| `LOGSEQ_API_TOKEN` | required |
| `LOGSEQ_API_URL` | `http://127.0.0.1:12315` |
| `LOGSEQ_PLUGIN_ID` | unset — makes the property sandbox check exact rather than namespace-wide |
| `LOGSEQ_PROBE_WRITES` | `true` — set false to skip write probing in `capabilities` |
| `LOGSEQ_API_CONNECT_TIMEOUT` | `3` seconds |
| `LOGSEQ_API_READ_TIMEOUT` | `15` seconds |
| `LOGSEQ_VERIFY_SSL` | `true` |
| `LOGSEQ_READ_ATTEMPTS` | `2`, for dedicated reads only |
| `LOGSEQ_READBACK_ATTEMPTS` | `3` |
| `LOGSEQ_READBACK_DELAY` | `0.15` seconds |
| `LOGSEQ_WRITE_TITLE_PREFIXES` | unrestricted when empty |
| `LOGSEQ_WRITE_PROPERTY_PREFIXES` | unrestricted when empty |
| `LOGSEQ_WRITE_ENTITY_UUIDS` | unrestricted when empty |
| `LOGSEQ_MAX_RESPONSE_BYTES` | `5000000` |

Each HTTP attempt has a hard deadline of connect + read timeout.

## Reliability

Every call uses a fresh client and sends `Connection: close`, so one failure
cannot poison the next request.

**Writes are never retried.** A timed-out write may already have been applied;
repeating it could double it. Instead the outcome is reported as ambiguous and
a process-local circuit blocks further writes while leaving reads open — reads
are how you find out what actually happened. Recovery means reading the target,
restarting **Logseq** (not just the relay, since a wedged DB worker survives
that), and reconnecting.

Queries are single-attempt for the same reason: a Datascript predicate runs
inside Logseq's DB worker, and one that timed out once will time out again
while doubling the load.

`capabilities` reports `writes_disabled` when the circuit is open.

## Connecting a client

**Claude Desktop** — `%APPDATA%\Claude\claude_desktop_config.json`, or
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS:

```json
{
  "mcpServers": {
    "mcp-logseq-db": {
      "command": "py",
      "args": ["-3.13", "-m", "mcp_logseq_db.server"],
      "env": {
        "LOGSEQ_API_TOKEN": "your-token",
        "LOGSEQ_API_URL": "http://127.0.0.1:12315"
      }
    }
  }
}
```

Use `python3` instead of `py` on macOS and Linux, or the installed
`mcp-logseq-db` console script if it is on your `PATH`. Restart the client,
then ask it to call `capabilities`.

**Claude Code**

```bash
claude mcp add mcp-logseq-db \
  --env LOGSEQ_API_TOKEN=your_token_here \
  --env LOGSEQ_API_URL=http://127.0.0.1:12315 \
  -- uvx --from git+https://github.com/LoveSkylark/mcp-logseq-db.git mcp-logseq-db
```

**VS Code / Copilot** — `.vscode/mcp.json` with the same command and a
`promptString` input for the token.

Do not enable this alongside a file-graph or legacy Logseq MCP server in the
same conversation. The tools target DB graphs and exact DB identifiers.

## Claude Skill

`dist/logseq-db-native.zip` gives Claude operational guidance for using this
server: identifier rules, the sandbox, what verification means. Import it under
Settings → Skills and enable it where this connector is available. Source is in
`skills/logseq-db-native/`.

It contains no token. The token belongs only in the MCP server configuration —
never in skill text, model instructions, or committed files.

## Tests

```bash
pytest                      # everything except tests marked `live`
scripts/test.ps1 -Docker    # clean container, no local Python involved
```

Nothing in the suite needs Logseq running.

Separately, `scripts/live_reliability.py` checks whether the server's
assumptions about Logseq are still true — that `page-id` accepts a block UUID,
that property writes are still namespaced, that a page name still fails
silently. The unit tests cannot answer those questions, because the fakes
encode the same beliefs the code does. Run it after a Logseq upgrade.

## Documentation

| | |
| --- | --- |
| [`doc/architecture.md`](doc/architecture.md) | why the server is built this way |
| [`doc/api-reference.md`](doc/api-reference.md) | each tool and the HTTP call behind it |
| [`doc/data-model.md`](doc/data-model.md) | how a DB graph is shaped |
| [`doc/logseq-api-surface.md`](doc/logseq-api-surface.md) | what the plugin API offers and why most is unexposed |
| [`tests/README.md`](tests/README.md) | running and extending the suite |
| [`scripts/README.md`](scripts/README.md) | the live checks |

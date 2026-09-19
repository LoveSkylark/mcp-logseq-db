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

`capabilities` · `getPageUUID` · `isTitleAvailable` · `inspectPage` ·
`pageStats` · `getBlockUUID` · `getBlock` · `getBlockTree` · `findBacklinks` ·
`findOrphans` · `getTagUUID` · `getTag` · `getTagUsers` ·
`getPropertyIndent` · `getProperyUsers`

**Lists** — no arguments, each returns a whole kind

`listPages` · `listJournals` · `listTags` · `listProperties` ·
`listClosedValues` · `listOrphanTags` · `listOrphanProperties` · `listAssets` ·
`listStatus` · `listRecycled`

**Writes** — each verifies by read-back

`importPage` · `repairLinks` · `createPage` · `renamePage` ·
`deletePage` · `clearPage` · `createBlock` · `createPageofBlocks` ·
`updateBlock` · `moveBlock` · `removeBlock` ·
`creatTag` · `deleteTag` · `addTag` · `removeTag` · `createProperty` ·
`deleteProperty` · `addProperty` · `removeProperty`

There is one `addTag`, not an `addPageTag` and an `addBlockTag` — a page **is**
a block in the DB, so the target is uniform and there is nothing to choose
between. The same applies to `addProperty`.

`getBlockTree` reads one block's subtree and reports `truncated` when a bound
stopped it; like `getBlockUUID` it walks `:block/parent`, so a block whose
`:block/page` disagrees still appears.

`inspectPage` takes a `detail` selector: `page`, `blocks`, `tags`,
`properties`, `declared`, or `all`. These are not interchangeable. A page's own
tags and its blocks' tags live in different places, and properties that a page
*declares* through its classes have no datoms at all — they appear in no other
query. It is called `inspectPage` rather than `getPage` because it returns far
more than a page entity, and because `logseq.DB.getPage` is a different and
much narrower thing.

`pageStats` is the one read whose response size does not depend on the page.
Every other read returns payload proportional to content, which makes auditing
many pages expensive; this returns a fixed set of counts regardless. Three of
them are worth distinguishing: `own_blocks` includes the empty block
`createPage` seeds, `empty_blocks` counts those, and `content_blocks` is the
difference — the figure to pair with a reference count when judging whether a
page carries anything.

## Cost lives at the tool boundary

What a caller pays for is what crosses in and out, not the work a tool does
internally. `importPage` on a 68-block page makes 34 `insertBatchBlock` calls
inside the server and returns one summary; building the same page with
`createBlock` would be 68 requests and 68 responses.

So the tools that loop internally are the cheap ones. `repairLinks()` with no
page argument sweeps the whole graph. `importPage` builds a page from markdown
in one call. `clearPage` loops `removeBlock` itself. `pageStats` returns counts
rather than content. And `listJournals(with_counts=true)` attaches block and
reference counts to every journal in four queries, where deciding which
journals hold anything otherwise costs one `pageStats` per journal — the usual
opening move of a migration, and around fifty calls to answer one question.
`listPages` takes the same option, capped at 500 pages per call. Both are
opt-in, so the bare listing stays as cheap as it was.

A corollary worth stating, since it is counter-intuitive: **slowness is not
cost**. A graph-wide sweep may take minutes of internal calls and still be far
cheaper than doing a fraction of it by hand.

## Terse write results

Every write tool takes `verbose`, defaulting to `true` for compatibility. The
envelope carries the target entity twice — before and after — which is the
point when you need to see what Logseq stored, and pure overhead when you do
not. Moving a 400-word block returns that block's text twice to tell you a
parent id.

`verbose: false` returns `verified`, `uuid`, `parent`, `page`, `order`,
`diagnostic`, and the assigned `ident` where there is one. **The verification
path is unchanged** — the write is still read back and still compared, and
`verified` means exactly what it did. Only the payload is left unserialised.
On a failure the observed entities are kept as digests rather than reduced to
a count, since a write cannot safely be repeated to get detail.

It is per call rather than a global default, because for `clearPage`,
`removeBlock` and `deletePage` the payload is the only surviving record of
what was destroyed or what still points at it.

## Limits worth knowing up front

**Property writes are sandboxed.** Only `plugin.property.<caller-id>/*` is
writable. Properties created in the Logseq UI live under `user.property/*` and
are readable but not writable, as are built-ins under `:logseq.property/`. This
is Logseq's restriction, not this server's.

**Property values are blocks.** Reference-typed values are materialized as
blocks on the holder's page. `clearPage` identifies and preserves them; other
tools should not assume every block on a page is content.

**Logseq parses content on write.** A markdown heading becomes a native
heading, but `[[X]]` mints a page and `#X` mints a tag — so `importPage`
escapes both to `{{link:X}}` and `{{tag:X}}` rather than creating a stub for
every unresolved target. `repairLinks` converts them once the targets exist,
and creating missing pages needs two explicit arguments plus a cap.

**Batches are not atomic.** `createPageofBlocks` makes one call per parent, so
a failure partway leaves earlier levels committed. The result names the level
that failed; audit with `findOrphans` rather than retrying.

**Page creation avoids `upsertNodes`.** That method fails on synced graphs —
it returns "The Imported EDN has N validation error(s)" for a write its own dry
run accepts, while `createPage`, `insertBlock`, `updateBlock` and `createTag`
all succeed against the same graph. Local graphs are unaffected. Nothing in
this server routes through it any more.

**`moveBlock` is confirmed working** on all placements, including across
pages. It no-ops when the position would not change, which the tool reports as
`verified: false` rather than a false success — so moving a block to the parent
it already has takes two moves, out and back.

**`placement=child` prepends; use `last-child` to append.** That is the
route's own behaviour and it is left alone, because callers depend on it — but
it means moving several blocks in source order with `child` reverses them at
the destination, silently. `last-child` reads the target's children and moves
the block after the current last one, then verifies it ended up *last* rather
than merely under the right parent. Nothing in this API can write
`:block/order` directly, which is why appending costs that extra read.

**Destructive tools require acknowledgement.** `deletePage`, `deleteTag` and
`deleteProperty` refuse until you confirm, listing what would be affected.

**`listClosedValues` depends on the graph.** `Status` and `Priority` carry
permitted values on a mature graph but not on a freshly created one, so an
empty result means this graph has no enums rather than that the feature is
missing.

**`:block/page` can disagree with `:block/parent`, and that is harmless.** On
some graphs a block's `:block/page` points at an ancestor block rather than at
the page. Logseq renders the outline from `:block/parent`, so the block
displays normally — only a query written against `:block/page` misses it.
`pageStats` and `findOrphans` report the count to explain a surprising query
result, not because anything needs repairing. Moving such blocks rewrites
their order for no benefit.

**Recycled pages survive deletion**, keeping their UUID, tags, and blocks, so
`listPages` excludes them explicitly and `listRecycled` shows them. Inbound
references are not rewritten.

**A recycled page still holds its title, and the two resolution paths disagree
about that on purpose.** `getPageUUID` will not resolve a recycled page — a
link resolving to a page the user deleted is worse than a miss — so it reports
such a title as not found. `createPage` and `renamePage` both refuse it, because
the entity is still there. Both behaviours are right; neither was discoverable,
and the gap cost a repair: a page recycled in the belief the title would be
released, found mid-repair to be still holding it, with recycling not
reversible.

`isTitleAvailable(title)` closes that. It runs the write path's own check and
reports `available`, plus `held_by` with each holder's UUID, kind and whether
it is recycled. Call it before any rename or page creation. Note the kinds:
pages, tags, blocks and properties share one title space, so a plain block
with the title you want is enough to make `createPage` refuse.

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

`scripts/build-skill.ps1` packages `skills/logseq-db-native/` into
`dist/logseq-db-native.zip`, which gives Claude operational guidance for using
this server: identifier rules, the sandbox, what verification means. Import it
under Settings → Skills and enable it where this connector is available.

It contains no token. The token belongs only in the MCP server configuration —
never in skill text, model instructions, or committed files.

## Tests

```bash
pytest                      # everything except tests marked `live`
scripts/test.ps1 -Docker    # clean container, no local Python involved
```

Nothing in the suite needs Logseq running.

Separately, `scripts/live_reliability.py` checks whether the server's
assumptions about Logseq are still true — that a block UUID is accepted where a
parent is expected, that property writes are still namespaced, that a page name
still fails silently. The unit tests cannot answer those questions, because the
fakes encode the same beliefs the code does. Run it after a Logseq upgrade.

## Documentation

| | |
| --- | --- |
| [`doc/architecture.md`](doc/architecture.md) | why the server is built this way |
| [`doc/api-reference.md`](doc/api-reference.md) | each tool and the HTTP call behind it |
| [`doc/logseq-api-surface.md`](doc/logseq-api-surface.md) | what the plugin API offers and why most is unexposed |
| [`skills/logseq-db-native/reference/data-modeling.md`](skills/logseq-db-native/reference/data-modeling.md) | how a DB graph is shaped |
| [`tests/README.md`](tests/README.md) | running and extending the suite |
| [`scripts/README.md`](scripts/README.md) | the live checks |

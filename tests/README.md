# Tests

## Running them

```bash
pytest                      # everything except tests marked `live`
pytest -q tests/test_content.py
pytest -k outline           # match by name
pytest -x --lf              # stop at first failure, then rerun just that one
```

From the repo root, in PowerShell:

```powershell
.\scripts\test.ps1              # local
.\scripts\test.ps1 -Clean       # wipe stale bytecode first
.\scripts\test.ps1 -Docker      # clean container, no local Python involved
.\scripts\test.ps1 -Docker -Python 3.11
```

Nothing here needs Logseq running. The whole suite uses fakes and finishes in
well under a second.

## Check the header before trusting a result

Run without `-q` and the first lines say which copy of the package was
imported:

```
mcp_logseq_db: E:\git\mcp-logseq-db\src\mcp_logseq_db\__init__.py  [src]
python: 3.13.1
```

`[src]` means the working tree. `INSTALLED COPY` means a `pip install .` build
shadowed it and the run tested something other than your edits — the usual
cause of "it passes for me". `conftest.py` forces `src` to the front of
`sys.path` to prevent this; the banner is there so you can confirm it worked.

If results look impossible, `.\scripts\test.ps1 -Clean` clears `__pycache__`.
Three interpreters have written bytecode into this repo, and mixed-version
`.pyc` files produce failures that disappear on a second run.

## What each file covers

**`test_boundaries.py`** — identifier validation, write scopes, environment
settings. Three modules, one concern: every guard here exists to turn a silent
no-op into a loud error. These are the tests that care about the *message* as
much as the rejection, because a caller who passed a title where a UUID
belonged needs to be told which mistake they made.

**`test_http_reliability.py`** — transport. Failure isolation, retry policy,
the write circuit breaker, `write_and_verify`, and the wire contract. No write
is ever retried: a timed-out write may already have been applied, so retrying
could double it.

**`test_content.py`** — pages and blocks. Creation, nesting, batching,
outlines, subtree deletion, reads.

**`test_mutations.py`** — tags and property values. Every operation that
accepts a target is tested against both a page and a block, because a page *is*
a block in the DB and that is why there is one tool rather than two.

**`test_capabilities.py`** — capability probing. Asserts that claims about
Logseq are actually probed, and that an inconclusive probe reports `unknown`
rather than guessing.

**`test_server.py`** — the MCP surface. Which tools exist, and what a caller
sees when one fails. Behaviour is covered by the modules above; repeating it
through the server layer would only test the wiring twice.

## Two conventions worth knowing

**The fakes model a graph, not a response queue.** `test_content` and
`test_mutations` build a small DB with real parent/page relationships, so a
test can ask which parent a block ended up under, or whether a subtree
actually went. A canned-response queue cannot express either, and — more
importantly — cannot fail when a write does nothing.

That matters because this API returns success for calls that do nothing. A
wrong identifier type produces HTTP 200 and a null body, exactly like a
successful write. Several tests set `write_effective=False` to simulate that
and assert the read-back catches it. If you add a write path, add the matching
no-op test; a write without one is a write whose failure is invisible.

**`test_server.py` keeps a `REMOVED_TOOLS` map.** Each removed tool is a test
carrying the reason it went. Restoring one by assuming it was an oversight
fails with an explanation rather than quietly widening the public surface.

## Adding a test that needs a real Logseq

Mark it `live` and it is deselected by default, in Docker, and in CI:

```python
@pytest.mark.live
async def test_something_against_the_real_graph():
    ...
```

Run those with `pytest -m live` or `.\scripts\test.ps1 -Live`, with Logseq
running and `LOGSEQ_API_TOKEN` set.

Live tests answer a different question from the rest of the suite. These files
check that the code does what we think; a live test checks that our model of
Logseq is still right. Every wrong assumption found so far — that `removeBlock`
was unavailable, that `page-id` only accepted pages, that property writes were
unrestricted — was invisible to the fakes, because the fakes encoded the same
wrong assumption the code did. Markers are strict, so a typo'd one is a
collection error rather than a silently skipped test.
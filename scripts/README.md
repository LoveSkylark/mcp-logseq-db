# Scripts

Tools that talk to a **running Logseq**. Nothing here is part of the package or
the test suite — `pytest` never touches these files, and they never run in CI.

They exist because the unit tests cannot answer one question: *is our model of
Logseq still correct?* The fakes in `tests/` encode the same beliefs the code
does, so a belief that goes stale stays invisible to them. Every wrong
assumption found so far was of that kind.

All scripts read `LOGSEQ_API_TOKEN` and `LOGSEQ_API_URL` from the environment,
the same as the server.

```powershell
$env:LOGSEQ_API_TOKEN = "your-token"
$env:LOGSEQ_API_URL   = "http://127.0.0.1:12315"
```

---

## `live_reliability.py`

The one to run after changing anything in `src/`, and after a Logseq upgrade.

```bash
python scripts/live_reliability.py             # read-only, safe anywhere
python scripts/live_reliability.py --write     # also exercises write paths
python scripts/live_reliability.py --explore   # probe the open questions
python scripts/live_reliability.py --skip-reliability   # contract only
```

Two sections, and the second is the point.

**Reliability** — does a timeout poison the next request, does cancellation
wedge the client, do concurrent reads stay isolated. The unit suite covers this
against fakes; here it runs against the real worker.

**Contract** — are the assumptions the code is built on still true. Each check
corresponds to something that was once wrong:

| Check | Why it is there |
| --- | --- |
| `getBlock`, `removeBlock`, `updateBlock` reachable | A hardcoded capability list called all three rejected. Block deletion was routed through a CLI for months because of it. |
| `createPage`, `insertBlock`, `insertBatchBlock`, `moveBlock`, `renamePage` reachable | Every write tool sits on these, and each one replaced an `upsertNodes` path. |
| `upsertProperty` rejects a namespaced title | The namespace comes from caller identity and cannot be chosen. |
| Recycled pages still queryable | They keep the Page class, so every page listing must exclude them explicitly. |
| Which way a page NAME behaves as a parent | Recorded rather than asserted. `insertBlock` resolves one as of 2026-09-20, where `upsertNodes` ignored one — so a mistyped argument now lands a real write, and the boundary UUID check is load-bearing rather than belt and braces. |

Three checks were dropped when `upsertNodes` left the allowlist: that
`edit`+`page` was unsupported, that `operation` was still `add`\|`edit`, and
that `data` rejected `parent-id`. All three described the contract of a route
nothing uses any more. `upsertNodes` itself cannot be usefully probed
read-only, because it fails on **synced** graphs and succeeds on local ones.

Read-only mode probes write methods with deliberately invalid arguments. A
validation error proves a method exists without mutating anything.

`--write` adds the findings that need a real write: that `createPage` is
idempotent on title, that `insertBlock` returns the entity it created, that a
**block** UUID as the parent nests *and* leaves `:block/page` pointing at the
page, what a page **name** as the parent now does (it resolves — see the table
above), that `moveBlock` reparents and then no-ops on a repeat, that
`children: true` still prepends, and that `deletePage` accepts a UUID and
recycles rather than destroys. It works on a scratch page and recycles it
afterwards; nothing existing is touched.

`--explore` reports rather than checks: which `logseq.DB.*` routes would close
the remaining gaps in the tool surface (tag inheritance, tag-level property
declaration), and which property namespaces exist in this graph. With
`--write` it also creates one property to discover this caller's assigned
namespace id, and leaves it in place.

Failures are collected rather than raised, so one stale belief does not hide
the rest. Exit code is non-zero if any check failed.

**A contract failure is not necessarily a bug in this repo.** It means Logseq
changed or the model was wrong. Confirm by hand before changing code — and
update the fakes in `tests/`, which by then encode the old belief.

---

## `probe_page_db.py`

A standalone CLI for poking at a graph by hand. Stdlib only, no dependency on
`src/`, so it also works as a reference for the raw HTTP shapes.

```bash
python scripts/probe_page_db.py list pages
python scripts/probe_page_db.py page get <uuid> --detail all
python scripts/probe_page_db.py block create <parent-uuid> "a title"
python scripts/probe_page_db.py outline <page-uuid> outline.txt
python scripts/probe_page_db.py raw logseq.DB.getAllTags
python scripts/probe_page_db.py -v ...          # echo each call to stderr
```

Every write does a read-back and prints `VERIFIED` or `UNVERIFIED`, exiting
non-zero on the latter. Commands whose route was never confirmed against a live
graph are marked `[UNVERIFIED]` in their help text — treat their output as a
hypothesis until you have seen the read-back.

`raw` deliberately bypasses the validation the other commands apply. That is
right for a probe tool and wrong for anything else; do not copy the pattern
into the server.

---

## Which to reach for

Changed code in `src/` → `pytest`, then `live_reliability.py`.

Upgraded Logseq → `live_reliability.py --write`. This is the run that catches a
behaviour change before it reaches users.

Exploring what the API does → `probe_page_db.py`, or Postman.

Found something surprising → add a contract check to `live_reliability.py` so
it stays found. A discovery that lives only in someone's memory gets
rediscovered the expensive way.
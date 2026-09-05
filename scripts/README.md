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
| `edit` + `page` still unsupported | If this changes, page editing gains a route and the operation table needs updating. |
| `operation` is still `add`\|`edit` | There is no retraction verb, which is why tag removal cannot go through `upsertNodes`. |
| `data` still rejects `parent-id` | The allowlist is closed. If it opens, `createBlock` can take more than a title. |
| `upsertProperty` rejects a namespaced title | The namespace comes from caller identity and cannot be chosen. |
| Recycled pages still queryable | They keep the Page class, so every page listing must exclude them explicitly. |

Read-only mode probes write methods with deliberately invalid arguments. A
validation error proves a method exists without mutating anything.

`--write` adds the two findings that need a real write: that `page-id` accepts
a **block** UUID and nests, and that a page **name** in `page-id` reports
success while writing nothing. It works on a scratch page and recycles it
afterwards; nothing existing is touched.

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
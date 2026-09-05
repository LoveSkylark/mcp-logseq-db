# Troubleshooting

Read when a write reports `verified=false`, a call times out, or writes stop
being accepted.

## Symptoms

| What you see | What it means | What to do |
| --- | --- | --- |
| `verified=false`, `previous_state` == `observed_state` | The write did nothing. Almost always an identifier of the wrong type. | Re-resolve the identifier. Do not retry with the same arguments. |
| `verified=false`, states differ | Something happened, but not what was asked. | Read the target fully before anything else. This is the more serious case. |
| `failure_stage: validation` | Rejected before reaching Logseq. | The `diagnostic` names what was passed — a title, an ident, a placeholder. Fix and retry. |
| `failure_stage: logseq_error` | Logseq returned an error. | Read it; it is usually specific. `"Editing a page, tag or property isn't supported yet"` is a hard limit, not a transient failure. |
| `failure_stage: transport` | Never got a reply. | The outcome is unknown. Read the target to find out. |
| `failure_stage: readback_mismatch` | The write returned, the read-back disagreed. | Same as `verified=false` above. |
| `writes_disabled` in `capabilities` | A previous write timed out ambiguously and the circuit opened. | See below. |
| A read is fine but every write is refused | Same circuit. | See below. |
| `PermissionError` on a write | An operator scope (`LOGSEQ_WRITE_*`) excludes the target. | Configuration, not a bug. Tell the user which scope. |
| `"outside this caller's namespace"` | A `user.property/*` or built-in property write. | Not possible over HTTP. Do not look for a workaround. |
| `deletePage` refuses with a reference count | Entities link to the page and those links are not rewritten. | Report the count to the user; only set `acknowledge_reference_rewrite` once they have decided. |
| A page "deleted" but still readable by UUID | Expected. Deleting recycles: the entity survives with `:logseq.property/deleted-at`. | Check `listPages` excludes it. Use `listRecycled` to see it. |
| A dry run succeeded but the real call failed | Expected. `dry_run` validates locally and never calls the API. | Never report a dry run as a completed change. |

## The write circuit

A timed-out write is **ambiguous**: Logseq may have applied it before the
connection dropped. Retrying could double it, so the server blocks every
subsequent write while leaving reads open.

That asymmetry is the point — reads are how you find out what actually
happened.

To recover:

1. Read the target and establish its real state.
2. Restart **Logseq**, not just the MCP. A wedged DB worker survives a relay
   restart, and the circuit exists because the worker was unresponsive.
3. Reconnect the MCP.

Never work around this by reconnecting and repeating the write. Reconcile
first.

## Timeouts

Datascript predicates run inside Logseq's DB worker, so an expensive query can
wedge the worker while the MCP process stays healthy. Queries are
single-attempt for that reason: a query that timed out once will time out
again, and retrying doubles the load on something already struggling.

If a trivial read also times out afterwards, the worker is wedged. Restart
Logseq.

A long UI freeze during a write usually means the same thing. Wait for the read
to succeed before assuming the write failed.

## Diagnosing a silent no-op

This is the characteristic failure, so it is worth recognising quickly.

The response is `null` or `{:block 1}` and nothing changed. Causes, in order of
likelihood:

**Wrong identifier type.** A property given a UUID instead of an ident. A tag
given its ident instead of its UUID. A block UUID where a page UUID belongs.

**A name where a UUID belongs.** No field resolves titles. `page-id` in
particular looks like it might and does not.

**An unsupported combination.** `upsertNodes` accepts exactly `add`+`page`,
`add`+`block`, `edit`+`block`. `edit`+`page` returns an explicit error;
anything else may not.

**A discarded field.** `data` is a closed allowlist — `page-id` and `title`
only. An explicit `ident` passed to `createProperty` is accepted and thrown
away.

To confirm which, read the target with `getPage(..., detail="all")` or
`getBlock`, and compare against what you asked for.

## Escaping errors

`FST_ERR_CTP_INVALID_JSON_BODY` means the request body was not valid JSON and
Logseq never saw it. Usually a Datascript query with unescaped quotes — the
query travels as a string inside the JSON, so `#uuid "..."` must be
`#uuid \"...\"`.

If the query passed through a Logseq block on its way, that is the cause:
`#uuid` autocompletes into a tag reference (`#[[...]]`), and `[[`, `((`, `{{`
transform too. Use a code block.

The MCP tools build their own queries, so this only arises with hand-written
ones.

## Operator scopes

Optional, off by default. An empty value means no restriction, not deny-all.

| Variable | Restricts |
| --- | --- |
| `LOGSEQ_WRITE_TITLE_PREFIXES` | titles of created pages, tags, properties — not block content |
| `LOGSEQ_WRITE_PROPERTY_PREFIXES` | property idents, narrowing within the sandbox |
| `LOGSEQ_WRITE_ENTITY_UUIDS` | which entities may be modified |
| `LOGSEQ_PLUGIN_ID` | makes the sandbox check exact rather than namespace-wide |
| `LOGSEQ_PROBE_WRITES` | set false to skip write probing in `capabilities` |

A `PermissionError` from any of these is configuration working as intended.
Name which scope blocked the write rather than retrying.

## Claims that need checking, not assuming

`capabilities` reports `unknown` when a probe was inconclusive. That is not
"unavailable" — try the tool and read `verified`.

Nothing here is guaranteed across Logseq versions. If
`graph.version_matches` is false, this build is not the one the tools were
verified against, and a behaviour difference is a plausible explanation for
anything surprising.

A previous version of this server carried a hardcoded list that reported three
working methods as rejected, and block deletion was routed around them for
months. If something is documented as unavailable and you have reason to doubt
it, that doubt is worth acting on — `scripts/live_reliability.py` re-checks the
load-bearing assumptions on demand.

# Verification, timeouts, and known issues reference

Load this file only when a write result looks ambiguous, a call times out, a
long wait happens, or before a session with many writes. Not needed for
ordinary reads or a single successful write.

## Verification and timeout handling

Metadata writes return `response`, `verified_state`, `previous_state`, and
`recovered_after_timeout`. Content writes return `response`,
`verified_entities`, `previous_entities`, `observed_entities`, `verified`, and
`diagnostic`. A successful HTTP status alone is not considered success.

An anticipated tool failure returns an MCP error whose text is a JSON envelope
with `verified=false`, `failure_stage`, `diagnostic`, `error_type`, and empty
verified/observed/previous fields when state was unavailable. Stages are
`validation`, `transport`, `logseq_error`, or `readback_mismatch`.

- `response` is often `null` for a successful Logseq mutation. Treat it as the
  raw API result, not verification evidence.
- `verified_state` is the server's post-write read-back. Claim success only
  when it contains the expected attribute or relationship.
- For `remove_property`, both `response` and `verified_state` are `null`
  after verified absence. The tool also verifies no direct attribute use or
  property-created value entity remains. For relationship/icon removals,
  `verified_state` normally contains the surviving entity without the removed
  value. If read-back fails after a write, the error includes `previous_state`
  and `observed_state` when they are available.
- `recovered_after_timeout=true` means the original response was ambiguous but
  read-back established the resulting state. Mention that recovery explicitly.
- A timed-out write may have committed. Never immediately repeat it.
- Read the exact target with a fresh tool call and reconcile observed state.
- The first write timeout opens a server-side write circuit. Every later write
  is rejected before HTTP, including calls through a different tool. Reads
  remain available for reconciliation. Check `write_circuit_open` and
  `write_circuit_reason` in `capabilities`, restart Logseq, and reconnect
  the MCP before another write. Restarting only Logseq does not reset the
  process-local MCP circuit.
- If creation timed out before Logseq returned a generated ident, stop and
  inspect the property/tag listings for a uniquely matching title.
- A failed, malformed, cancelled, or timed-out read must not poison a later
  read. A write timeout intentionally blocks later writes until MCP reconnect;
  reads remain available for reconciliation.
- The server serializes writes across concurrent MCP calls so mutation and
  read-back cannot interleave with another write.
- Read-only transport failures may be retried with a fresh connection. Writes
  are never retried automatically.
- Each HTTP attempt has a hard wall-clock deadline equal to the configured
  connect timeout plus read timeout. With the defaults, one attempt is bounded
  to 18 seconds. Query/search and write calls get one attempt; eligible normal
  reads may use `LOGSEQ_READ_ATTEMPTS` attempts.
- Read-back is polled for a bounded number of attempts to tolerate delayed
  visibility; polling repeats only reads, never writes.
- Responses exceeding the configured byte limit are rejected rather than sent
  unbounded into the MCP conversation.
- If `check_current_is_db_graph` or another trivial read times out after a
  query timeout, restart Logseq itself to clear its DB worker, then restart the
  MCP connection. Restarting only the MCP relay cannot clear a wedged worker.
- Distinguish timeout from connection refusal: timeout suggests a stuck Logseq
  worker; connection refusal means Logseq's HTTP server is not listening.
- Distinguish an MCP timeout result from a Claude Desktop UI wait. If the
  connector UI waits substantially longer than the configured bound without
  returning a tool result, do not infer which raw Logseq method ran and do not
  report the MCP tool as broken. Treat it as a client/session transport issue,
  reconnect the MCP, run `check_current_is_db_graph`, and then reconcile the
  target state. Never repeat an ambiguous mutation merely because the UI lost
  its result.
- A normal read succeeding after a write timeout does not prove the write path
  is healthy. Logseq 2.0.1 can keep reads responsive while individual writes
  repeatedly time out. Reconcile the timed-out target, restart Logseq, and test
  one disposable write before classifying the method as unsupported.

Use this discriminator:

| Symptom | Meaning | Action |
|---|---|---|
| About 4 minutes, `No result received from the Claude Desktop app` | Client/session transport | Reconnect and re-read; do not blame or repeat the tool |
| Fast MCP error with a JSON `failure_stage` | Server validation, transport, Logseq, or read-back failure | Read the diagnostic and reconcile state; do not retry blindly |
| Envelope with `recovered_after_timeout=true` | Underlying write timed out and bounded read-back ran | Trust `verified` and the observed-state fields |

One default HTTP attempt is bounded to 18 seconds. A UI wait substantially
beyond about 20 seconds is not the server's single-attempt HTTP timeout.

## Optional write access scopes

Deployments can restrict writes without restricting reads:

- `LOGSEQ_WRITE_TITLE_PREFIXES`: comma-separated prefixes for created or
  renamed titles.
- `LOGSEQ_WRITE_PROPERTY_PREFIXES`: comma-separated allowed property-ident
  prefixes.
- `LOGSEQ_WRITE_ENTITY_UUIDS`: comma-separated UUID allowlist for writes to
  existing entities.

When configured, values outside the scope fail before mutation. Empty settings
mean unrestricted writes and are appropriate only for a trusted graph.

## Known unavailable or rejected behavior

- `getFavorites`: rejected after HTTP 500 on the tested Logseq 2.0.1 build.
- `setPropertyNodeTags`: rejected after a live timeout.
- Property value choices: transport returned success, but the requested effect
  was not observable through the available property reader, so no MCP tool is
  exposed and it remains a candidate.
- `onChanged` and `onBlockChanged`: callback APIs cannot cross the ordinary
  request/response HTTP boundary.
- File reads/writes: `setFileContent` is a candidate, not a supported tool. No
  real DB file target has passed write/read-back/cleanup verification.
- `createPage`, `insertBlock`, `insertBatchBlock`, `prependBlockInPage`,
  `updateBlock`, `removeBlock`, and `moveBlock` are raw `logseq.DB.*` aliases
  that timed out during live testing and are not exposed. Use `upsert_nodes`
  for page/top-level creation and block-title edits. Use `insert_block`,
  `move_block`, and `delete_block` for worker-backed structure changes.
- `upsert_block_property`, `set_block_icon`, and `set_tag_parent` can time out
  in a degraded write session but succeeded with exact read-back as the first
  write after a fresh Logseq restart. `set_tag_parent` routes through the raw
  `addTagExtends` alias.
- `add_block_tag` and `remove_block_tag` use the graph-worker path because it
  remained responsive when the equivalent DB HTTP aliases timed out.
- `newBlockUUID` and `exportEdn` are bound but unnecessary for current safe
  workflows. `importEdn` is a high-impact whole-graph operation and is not
  exposed. `insertBatchBlocks` (plural) is not bound.
- Promoted `child` and `after` operations use the supported graph-worker path.
  The plugin insert/move/remove aliases timed out and are not exposed.
- `get_block_tree` successfully traversed an existing depth-3, seven-node
  subtree with owning-page references preserved.
- `get_block` returns `{found:false, block:null}` for a missing/deleted UUID;
  absence is not reported as a generic tool error.

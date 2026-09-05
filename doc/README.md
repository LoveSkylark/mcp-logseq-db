# Documentation

| File | What it answers |
| --- | --- |
| [`architecture.md`](architecture.md) | Why the server is built the way it is. Read this first. |
| [`api-reference.md`](api-reference.md) | What each tool does and the raw HTTP call behind it. |
| [`data-model.md`](data-model.md) | How a Logseq DB graph is shaped, and which queries reach what. |
| [`logseq-api-surface.md`](logseq-api-surface.md) | The full Logseq plugin API, and why most of it is not exposed. |

Related, elsewhere in the repo: [`../tests/README.md`](../tests/README.md) for
the suite, [`../scripts/README.md`](../scripts/README.md) for the live checks.

## The one thing to know

**This API returns success for calls that do nothing.** A wrong identifier
type, an unresolvable name, or an unsupported combination produces `null` or
`{:block 1}` — indistinguishable from a successful write.

Everything else here follows from that: why every write is read back, why
identifiers are validated at the boundary, why `capabilities` reports three
states rather than two, and why there is a live contract script separate from
the test suite.

## What was removed, and why

`design.txt` — the original `upsertNodes`-first design. It assumed
`upsertNodes` was a general mutation primitive with dedicated APIs as
fallbacks. Live testing showed it accepts exactly three operation/entity
combinations and has no retraction verb at all, so the fallback ordering
described a structure that does not exist. Superseded by `architecture.md`.

`tools.txt`, `tool-list.txt` — merged into `api-reference.md`. Several of the
queries in them were wrong: `listOrphanProperties` carried the assets query,
`getPropertyIndent` matched tags as well as properties, and one query still
contained a `[[uuid]]` artifact from being pasted through the Logseq editor.

`structure.txt` — its capability table recorded methods as verified that later
turned out not to work, and marked others rejected that do. Facts that survive
live testing now live in `api-reference.md`; the graph shape moved to
`data-model.md`.

`api-tools.txt` — a flat dump of every plugin API. Kept as
`logseq-api-surface.md`, with the reason each method is or is not reachable.

# Documentation

| File | What it answers |
| --- | --- |
| [`architecture.md`](architecture.md) | Why the server is built the way it is. Read this first. |
| [`api-reference.md`](api-reference.md) | What each tool does and the raw HTTP call behind it. |
| [`logseq-api-surface.md`](logseq-api-surface.md) | The full Logseq plugin API, and why most of it is not exposed. |
| [`archive/`](archive/README.md) | Superseded documents, kept for the record. Nothing there is current. |

How a DB graph is shaped, and which queries reach what, is covered by
`api-reference.md` (the queries) and
[`../skills/logseq-db-native/reference/data-modeling.md`](../skills/logseq-db-native/reference/data-modeling.md)
(the modelling). There is no `data-model.md`; this table used to link one.

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
live testing now live in `api-reference.md`; the graph shape moved to the
skill's `reference/data-modeling.md`.

`API refrence.md` — a near-duplicate of `api-reference.md` from the
`upsertNodes` era, one typo away from the file that replaced it, which is how
two contradictory references stayed in this folder. Moved to
[`archive/`](archive/README.md) rather than deleted, because what it got wrong
is the useful part.

`api-tools.txt` — a flat dump of every plugin API. Kept as
`logseq-api-surface.md`, with the reason each method is or is not reachable.

---

## Status, 2026-09-20

One session added thirteen tools and changes, driven by a list of instructions
written after a real migration ran into the surface's gaps. Recorded here
because the open items at the bottom are the sort of thing that otherwise gets
rediscovered expensively.

### Shipped

| # | Change | Live status |
| --- | --- | --- |
| 1 | `moveBlock` gained `placement=last-child`, which APPENDS. `child` prepends and is unchanged. | Confirmed: `children:true` still prepends, so the placement is still needed |
| 2 | `verbose` on every write tool; terse returns identity and position only | Confirmed |
| 3 | `with_counts` and `limit` on `listJournals` and `listPages` — four queries whatever the graph size | Confirmed; counts agree with `pageStats` |
| 4 | `isTitleAvailable` — the write path's own check, reporting the holder and whether it is recycled | Confirmed, including the `getPageUUID` disagreement |
| 5 | `retitleOverDuplicate` — two renames take a title back from an empty duplicate, including a recycled one | Unit-tested only |
| 6 | `pageStats` reports `is_alias_of` and `aliases`; `deletePage` gained `acknowledge_alias_loss` | Confirmed both directions on a live alias |
| 7 | `moveBlocks` — a list relocated in order at ~2.5 calls per block | Confirmed: ten in ~25 calls, ordering and paging both hold |
| 8 | `searchBlocks` — substring search, count-first so zero is a real zero | Not yet run live |
| 9 | `importPage` accepts a block LIST with explicit depth, for blocks containing newlines | Confirmed |
| 10 | `purgeRecycled` / `restorePage` | **Not built** — see below |
| 11 | `findDuplicateTitles` — grouping with counts and alias status, six queries | Unit-tested only |
| 12 | `splitBlock` — parts created before the original is truncated | Unit-tested only |
| 13 | `migratePage` — a mechanical wrapper over `moveBlocks` | Unit-tested only |

### Found by testing, and fixed

- `insertBlock` RESOLVES a page name where `upsertNodes` ignored one. The
  "names are inert" rule was established against a route nothing uses any
  more, so a mistyped argument now lands a real write and the boundary UUID
  check is load-bearing rather than belt and braces.
- `insertBatchBlock` PREPENDS. Documented, not fixed — see below.
- Logseq truncates a block at a line beginning with `- ` and discards the
  rest. Blank lines and plain newlines survive. Refused at the boundary now,
  in every write path.
- `importPage` verified block COUNT only, which is how eight lines sent and
  two stored reported `verified: true`. It now compares content: equality on
  the list form, a line-count invariant on the markdown form.
- `parse_markdown` discarded pre-bullet text with a warning. It now raises.
- `create_block` and `update_block` checked structure only. `update_block` was
  the worse case: it verified the title CHANGED, and a truncated write changes
  it.
- `createPageofBlocks` reported `created_count: 55` beside 20 UUIDs, because
  the digest helper caps at 20. Identifier collections are no longer bounded.
- Two `"delete_page"` keys in one dict, so the alias guidance was dead.
- `test.ps1` only tried `py` then `python3`, and failed on a machine with
  Python on PATH as `python`.

### Not additive

The instruction list aimed for changes that needed no coordinated client
update. Three do not qualify, all trading a silent wrong answer for a loud
refusal: `parse_markdown` raising on pre-bullet text, `importPage`'s verdict
tightening from `>=` to `==` plus content comparison, and `parse_blocks`
refusing a `- ` line. Anything driving `importPage` programmatically should be
re-tested.

### Open

1. **`createPage` reported `verified: true` for a page that did not exist.**
   The returned UUID resolved to nothing, `isTitleAvailable` said the title
   was free, and a second call minted a different UUID — so it was not
   idempotent, because the first page was gone. One observation, unexplained.
   The likely mechanism is Logseq discarding an untouched empty page while the
   read-back caught it in memory. This is a false `verified` beneath
   everything else in the surface, so it is the first thing to chase. To
   settle it: create a page, query the title immediately, query again after
   thirty seconds, touching nothing in between.
2. **`insertBatchBlock` prepends, and nothing fixes it.** Each import batch
   lands above what the page already held, so building a document chapter by
   chapter yields the chapters in reverse. Order within one call is correct.
   The candidate fix is anchoring the batch on the page's current last child
   with `{"sibling": true}` instead of the page with `{"sibling": false}` —
   unverified, and it would change `createPageofBlocks` too, so probe first.
3. **`moveBlocks`' `all_or_nothing` has never executed.** Every reachable
   failure is caught before the loop, so a genuine mid-list stop needs
   `moveBlock` to return clean without landing the block, which cannot be
   induced from the client. `rolled_back` and the "POSITION WAS NOT RESTORED"
   diagnostic are unverified, and that matters because the remedy is partial
   by design: parentage is restored, position is not.
4. **No envelope reports calls issued.** Cost is a static read of the source,
   not a measurement. With `readback_attempts: 3` and `readback_delay: 0.15`,
   a contended worker could turn a 50-block `moveBlocks` into some 200 calls
   and seven seconds of sleep, with nothing distinguishing that from the happy
   path. A counter on the client would fix it for every tool at once.
5. **Restore and purge are closed as unbuildable.** A second `deletePage`
   leaves the entity, so there is no purge. `removeBlockProperty` does clear
   `:logseq.property/deleted-at`, but the page stays parented under Recycle —
   live to `listPages`, in the bin by structure. Whether that parent matters
   functionally needs a human to look at a half-restored page in the UI. If it
   is cosmetic, `restorePage` is buildable; if the page appears in both places,
   it is not. `architecture.md`'s open questions carry the detail.

### Never run live

`searchBlocks`, `retitleOverDuplicate`, `findDuplicateTitles`, `splitBlock`,
`migratePage`. The unit suite cannot answer whether the model of Logseq is
right — the fakes encode the same beliefs the code does — and on this session's
evidence roughly one assumption in three turned out to be wrong. Run
`doc/claude-desktop-skill-tester.md` against a real graph before trusting any
of them on content that matters.

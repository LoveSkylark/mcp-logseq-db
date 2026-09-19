# Repair

Read before repairing a damaged graph: duplicate titles, split identities,
stranded tags and properties.

The governing constraint is that **nothing here can be undone cheaply**.
`deletePage` recycles rather than destroys, references are never rewritten for
you, and a UUID can be neither assigned nor freed. So repair is triage first
and writes last, and the triage is what this file is mostly about.

A second constraint, learned expensively: **a count is not a symptom.** Before
repairing anything, confirm the user can actually see the problem. A metric
built on the same attribute that looks wrong will happily report a fault that
does not exist — see the note on `:block/page` under "Other damage".

## Three facts that determine every repair

**A UUID cannot be moved.** No tool writes `:block/uuid`. `createPage` takes a
title only; `createBlock` has Logseq assign the UUID; `updateBlock` edits a
title and nothing else. There is no `upsertNode`. Even a generic upsert would
not help — upsert *resolves on* the identity attribute, so a different UUID
selects a different entity rather than renaming one.

**Deleting does not free a UUID.** Recycling keeps the UUID, tags, refs and
blocks. A title is released for reuse, an identity never is.

**Empty does not mean safe.** The most common serious error is recycling an
empty page that is the target of live references. Content and identity land on
different pages often enough that emptiness alone is never sufficient grounds
to delete. Always pair a block count with a reference count.

## Counting cheaply

Start with a counted LISTING, not a per-page read.
`listPages(with_counts=true)` and `listJournals(with_counts=true)` attach
`own_blocks`, `content_blocks` and `refs` to every entry in four queries
total, which covers the whole first triage pass — which pages hold content,
and which of the empty ones are referred to anyway. `listPages` is capped at
500 rows per call, so a larger graph needs `limit` and successive slices.

`pageStats(page_uuid)` is then the per-page primitive, for the pages the
listing singled out. It returns integers only, so its cost does not depend on
how large the page is — the whole point of the tool, and what made a
full-graph audit affordable before the listings could do it in bulk.

- `own_blocks` — blocks whose `:block/page` is this page. This is "does the
  page have content".
- `subtree_blocks` — everything reachable by `:block/parent`, **including
  nested pages**. Using this as a content count will make an empty container
  look populated.
- `nested_pages` — sub-pages beneath this one. Structure, not damage.
- `true_orphans` — blocks whose `:block/page` differs from their nearest
  ancestor page. **Not damage.** Logseq renders from `:block/parent`, so these
  display normally; the count only explains why `own_blocks` is lower than
  `subtree_blocks`. Never a reason to write.
- `refs`, `tag_holders`, `property_values` — inbound references by mechanism.

One call gives both halves of the safety check: a block count and a reference
count. Never treat emptiness alone as grounds to delete.

`findOrphans(page_uuid)` names the blocks behind a `true_orphans` count. It is
informational — useful for understanding a query result, never a repair
signal. It returns the blocks in full, so it is wasted payload unless you
actually need the identities.

`findBacklinks(uuid)` is for when the count is not enough and you need to know
*what* refers to something. It reports refs, tag holders and property values
separately; property values are real references that the Logseq UI does not
show in its backlink panel, so the total will exceed what the user sees on
screen. Its payload scales with reference count, so prefer `pageStats` unless
you need the identities.


## Triage: classify before writing

Every duplicate-title pair falls into one of five classes. Classify first —
the classes have different repairs and two of them are not damage at all.

| Class | Signature | Repair |
| --- | --- | --- |
| **A — dead stub** | 0 own blocks, 0 refs, twin has content | Recycle the stub |
| **B — split identity** | 0 own blocks, **has refs**, twin has content | Repoint or rebuild (below) |
| **C — genuine split** | Both have own blocks | Stop. Human decision |
| **D — nested structure** | One is a sub-page of a container | Not damage. Leave |
| **E — near-title variant** | Titles differ (typo, case, singular/plural) | Usually leave |

Class A is the only one safe to repair unattended, and only after both counts
are confirmed for that specific page.

Class D used to be a trap because `findOrphans` reported every block under a
nested page as an orphan. It no longer does: a nested page is treated as a page
boundary, and its blocks are counted against it rather than against the
container.

A container still looks distinctive — few or zero `own_blocks`, many
`subtree_blocks`, and `nested_pages` above zero. That combination is ordinary
structure. Leave it.

Class E cannot usually be fixed by renaming. If the correctly spelled page
already exists, renaming the typo collides with it and yields a fresh
duplicate instead of a fix. Tags and pages share one title space, so the same
collision applies to tags. Report these; do not rename.

## Repairing a split identity (Class B)

The shape: page **A** holds the content and nothing links to it; page **B** is
empty and owns the UUID every reference points at. Clicking a link lands the
user on a blank page while the real material sits elsewhere under the same
title. Identical creation timestamps across many such pairs indicate one bad
import rather than user error.

Three routes.

**Repoint via placeholders** — the usual choice. De-resolve each reference to
`{{link:A-title}}`, recycle B, then let `repairLinks` resolve them against A.

- Cost: one `updateBlock` per referencing block, one `deletePage`, one
  `repairLinks`.
- Lossless, and A is never touched.
- Safer than writing `[[A-uuid]]` by hand: you name the page by title and the
  tool resolves it, verifying `:block/refs` actually gained A's id.

The sequence matters:

1. `findBacklinks(B)` — the blocks to rewrite.
2. For each, `getBlock` then `updateBlock`, replacing `[[<B-uuid>]]` with
   `{{link:A-title}}`. **The stored text holds B's UUID, not its title** —
   Logseq rewrites a reference to the target's UUID when it resolves, so
   searching for the title finds nothing.
3. `findBacklinks(B)` again — `refs` should be empty.
4. `deletePage(B)`. **Before repairing, not after.** A and B share a title, and
   `repairLinks` resolves by title: with both pages present it reports the name
   as ambiguous and skips it. This works only because `getPageUUID` excludes
   recycled pages, so the recycled B is not a candidate.
5. `repairLinks(dry_run=true)` to confirm A resolves and nothing is ambiguous,
   then `repairLinks()`. Leave `create_missing` off — if A does not resolve,
   step 2 or 3 was incomplete and creating a third page makes it worse.

**Repoint directly** — rewrite each referencing block so its `[[uuid]]` names
A, then recycle B.

- Same cost, minus the `repairLinks` call.
- Requires writing A's UUID into the text by hand, with no verification that
  the reference resolved. Prefer the placeholder route unless B must survive.

**Rebuild** — recreate A's outline on B, then recycle A.

- Cost: roughly one `createPageofBlocks` call per parent that has children.
  Depth, not block count, drives the number.
- **Lossy.** New block UUIDs are minted, so block-level references into A
  break. Only titles are carried; per-block tags and property values are
  separate follow-up calls and do not come along.
- Preserves the referenced UUID, so no referencing block is edited.

**Choose by comparing reference count against block count**, not by instinct:

```
repoint  ≈ refs + 2 calls, lossless
rebuild  ≈ parents-with-children + 1 calls, lossy
```

These pages usually have few references and much content, so repoint normally
wins on both axes — two edits beat reconstructing 163 blocks. Prefer rebuild
only when references clearly outnumber blocks and no block-level references
exist. When the two are close, prefer repoint: it is lossless, and that
outranks a small call saving.

**Direction matters more than route.** Both repoint options move canonical
identity to A, so anything outside the graph holding B's UUID goes stale.
Rebuild keeps B's identity instead. Choose by which side is referenced: move
references toward the identity that has content, or move content toward the
identity that is referenced.

Before rebuilding, always run `findBacklinks` against the **content** page to
check for block-level references. If any exist, rebuild will break them and
repoint is the only safe route.

## Order of operations

Cheap and reversible first; irreversible last.

1. `listPages`, and group by title to find candidate duplicates.
2. For each candidate, `findOrphans` on both members → own-block counts.
3. `findBacklinks` on any member with 0 own blocks → reference count.
4. Classify A–E. Write the ledger.
5. Confirm with the user for anything except Class A.
6. Execute writes one page at a time, checking `verified` on each.

Never invert steps 2 and 3 to save a call. A reference count without a block
count cannot distinguish a dead stub from a link hub.

## The ledger

Keep a row per pair, and report it before writing. It is the artifact the user
approves, and it makes a resumed session cheap.

```
title | keep-uuid | keep own-blocks | other-uuid | other own-blocks | other refs | class | action
```

Record UUIDs, never `:db/id` integers — those are internal and renumber when
the graph is rebuilt.

## Other damage

**`:block/page` disagreeing with `:block/parent` — NOT damage.** `pageStats`
reports a `true_orphans` count and `findOrphans` names the blocks. On some
graphs a block's `:block/page` points at an ancestor block rather than at the
page.

Leave it alone. Logseq renders the outline from `:block/parent`, so the blocks
display normally and the user can reach them; only a query written against
`:block/page` misses them. The count exists to explain a surprising query
result, not to prompt a repair.

Do not move these blocks to "correct" the attribute. A move rewrites
`:block/order`, so a bulk pass reorders the user's content for no benefit. A
tool that did exactly that was removed from this server after roughly 1,500
blocks had been moved — built on the assumption the blocks were invisible,
which nobody checked by opening the page until afterwards.

If a user reports content genuinely missing from a page, verify it in the UI
before believing any count. `own_blocks` disagreeing with `subtree_blocks` is
not evidence of missing content.


**Recycled pages with inbound references.** `listRecycled` shows pages the user
can no longer find while links still point at them. Repoint the references, or
have the user restore the page from Recycle.

**Orphan tags and properties.** `listOrphanTags` and `listOrphanProperties`
list entities nothing uses. Safe to remove, but confirm first: `deleteProperty`
takes every value with it graph-wide and is not reversible by recreating, since
a new property is a new entity.

**Property writes outside the sandbox.** Properties made in the Logseq UI live
under `user.property/*` and are readable but not writable; built-ins under
`:logseq.property/` likewise. This is a Logseq restriction. Report it plainly
and do not look for a workaround.

**Empty blocks.** Usually deliberate spacing in an outline. Not damage. Leave
them unless the user asks.

## Stop rules

Stop and report rather than continuing when:

- Both members of a pair have content (Class C). Merging is a modelling
  decision, not a cleanup.
- A page that looks like a duplicate turns out to be a container (Class D).
- A `findOrphans` call returns a large orphan list. That is a query-visibility
  quirk, not damage — report it and stop. Do not move blocks over it.
- A write returns `verified: false`. Read `previous_state` and
  `observed_state`; do not retry with the same arguments.
- The repair would edit prose or pick a canonical identity on the user's
  behalf. Get explicit per-page agreement.

A blanket approval to "repair everything" covers Class A. It does not cover
edits to the user's writing, and it does not license guessing at intent when
the structure is ambiguous.

## Reporting

State counts, not impressions: own blocks, reference counts, and which UUID
survived. After a recycle, say that the page is restorable from Recycle with
its UUID intact, and that inbound references were not rewritten. When a repair
is impossible, name which of the three it is — the tool does not exist, the
route does not exist, or Logseq forbids it — because they call for different
responses from the user.

# Logseq DB import repair

This skill documents crash-causing corruption patterns found in graphs migrated from Logseq OG (file-based) to Logseq DB-native, how to diagnose them with only the `mcp-logseq-db` MCP tools (no JS stack trace is available through this interface), and how to fix them while changing as little of the original document as possible.

## Guiding principle: repair, don't rebuild

The goal is always to keep the page's structure, wording, ordering and block UUIDs as close to the original as the tooling allows. Never respond to a crash by deleting and re-importing a page's content from scratch (`importPage(replace=true)` or similar). Instead:

- Use `updateBlock` to fix only the offending block's text in place, preserving its UUID, its position, and any properties (like `:logseq.property/heading`) that should survive.
- Use `removeBlock` only for blocks that are confirmed to be invisible junk (empty content, no legitimate purpose) — never for a block that has visible text, even if it's also part of the bug.
- Use `moveBlock` / `moveBlocks` to relocate content during diagnosis, never as the repair itself unless the goal is genuinely to reorganize.
- After any fix, verify with `pageStats` and `findOrphans` on the affected page(s) rather than re-reading the whole tree.

## Known crash-causing patterns

Both patterns below were found in a graph migrated from Logseq OG. Both are silent at the data layer (writes succeed, `pageStats`/`findOrphans` show no damage) but crash the Logseq DB renderer with its generic "Something went wrong" error-boundary screen when the page is opened. A full app quit-and-relaunch may be needed to see a fix take effect if the renderer already crashed on that page in the current app session.

### Pattern 1 — a heading block whose entire content is a single wiki-link

Example: a block with content `# [[6a93617c-78e8-48fc-be23-5b64c897e648]]` (a level-1 heading) or `#### [[6a936179-74b2-43b5-a252-31e06f24880b]]` (level-4), where the bracketed link is the block's *only* content — no other text alongside it.

This crashed regardless of whether the link was self-referential (pointing at the page the block itself lives on) or pointed at an unrelated page (including a page that is itself an alias target). Self-reference is not required to trigger it — a plain heading-only link to any other page reproduced the crash too.

**Fix:** `updateBlock` the block's title to plain text — either the link target's display title, or any short descriptive text — keeping the heading markdown level (`#`, `##`, etc.) if the block should stay a heading. This clears the `refs` edge on that block entirely (verify via `getBlock` — the `refs` field should be absent after the fix) while preserving the heading's position and level.

```
Before: {"content": "#### [[6a936179-74b2-43b5-a252-31e06f24880b]]", ":logseq.property/heading": 4, "refs": [{"id": 504}]}
After:  {"content": "Conflict", ":logseq.property/heading": 4}   # refs gone
```

If the information the link conveyed matters (e.g. it was providing a cross-reference), consider adding it back as a *non-heading* sibling or child block with a normal `[[link]]` — plain-content blocks with links elsewhere on the same page were tested extensively and never reproduced this crash. Only a heading block whose sole content is the link is implicated.

### Pattern 2 — an empty block carrying a raw `:block/link` attribute

These are blocks with `content: ""` (nothing visible in the UI) that nonetheless carry a `link` property — e.g. `getBlock` returns `{"content": "", "title": "", "link": {"id": 3130}}`. The referenced id is often a raw internal entity id that doesn't resolve to any block owned by the page in question — i.e. a dangling cross-page reference edge.

These are believed to be leftover artifacts from Logseq OG's `((block-reference))` / block-embed syntax: the display text was cleared out at some point during migration, but the underlying `:block/link` datascript edge was left behind, pointing at a block that may no longer exist in a form the DB renderer expects.

They are usually found as the sole child of an otherwise ordinary content block (e.g. a glossary entry `"Campaign"` with one empty child carrying this stray link). They render as nothing in the UI, so removing them loses no visible information.

**Fix:** `removeBlock` on the empty block directly. Since it has no content, this cannot lose any visible information. Do not remove or alter its parent (the visible block it's attached to) — only the empty stub child itself.

## Diagnostic method: bisection via non-destructive moves

There is no way to get a JS stack trace or crash log through the `mcp-logseq-db` tools. When a page crashes and the cause isn't obviously one of the two patterns above, use binary-search bisection:

1. Create a disposable scratch page (`createPage`) to hold suspect content during testing. Never test destructively on the real page.
2. Create a landing block on the scratch page (`createBlock`), then move roughly half of the suspect page's top-level content there with `moveBlocks(..., placement="last-child")`. This preserves block UUIDs, content, and relative order — the move can always be reversed.
3. Ask the user to fully quit and relaunch the Logseq app (not just reopen the page/tab — a page that has already crashed the renderer once in the running app session may stay in a bad state even after the underlying data is fixed) and report whether the real page still crashes.
4. If it still crashes, the bad block is in the half that stayed — repeat the split on that half. If it stops crashing, the bad block was in the half you moved out — move that half back in smaller pieces and repeat.
5. Continue halving until a single block is isolated. Confirm by moving that one block back alone and re-testing, then moving it out again to confirm the crash toggles with its presence.
6. Once confirmed, apply the appropriate fix from the patterns above (don't leave it stranded on the scratch page).
7. Before merging bisected content back onto the real page, proactively scan it for Pattern 2 (see below) — a properties scan is cheap and catches other latent instances that bisection alone might not isolate quickly, since multiple bad blocks can coexist on one page.
8. When reassembling moved content, use `moveBlocks` again with the blocks sorted back into their *original* document order (capture original `:block/order` values before you start moving things, e.g. via `inspectPage(detail="blocks")`, so you can restore the original reading order rather than whatever order the scratch page's shuffling left them in).
9. Delete the scratch page once the real page is confirmed fixed and everything has been moved back.

## Proactively scanning a page for Pattern 2 before it causes a crash

`:block/link` is a raw internal Datascript attribute, not a Logseq "property" in the user-facing sense — it does **not** appear in `listProperties`, and `getProperyUsers` refuses it ("not a title or a UUID" — it wants a full namespaced ident like `:user.property/foo-xxxx`, and `link` isn't namespaced). The only way to surface it with current tools is:

1. Call `inspectPage(page_uuid, detail="properties")` on the page (this can be a large payload for a big page — expect it to be saved to a file rather than returned inline; read it with the file-reading tool and grep/parse with a short script rather than paging through it by hand).
2. Filter the returned `properties` array for entries where `property.ident == "link"`.
3. For each match, check whether `holder.content == ""` — if so, it's very likely a Pattern 2 dangling stub and a `removeBlock` candidate. (A non-empty holder with a `link` property may be legitimate content that also happens to carry this attribute — don't remove those without checking `getBlock` on it directly first.)

This workaround is verbose and payload-heavy for large pages. A dedicated lookup tool for the raw `:block/link` attribute does not exist yet. The request for that tool is tracked separately as its own instruction document, not as part of this skill — see `mcp-logseq-db-feature-request.md` (delivered to the user directly).

# Archive

Superseded documents, kept only because they record what was believed at the
time and why it was wrong. **Nothing here is current.** Read
`../api-reference.md` and `../architecture.md` instead.

| File | Why it is here |
| --- | --- |
| `api-reference-upsertnodes-era.md` | A near-duplicate of `api-reference.md` from when `upsertNodes` was the primary mutation route. It documents `createManyBlocks`, `getPage`, and `upsertNodes` add/edit as the routes behind block and page writes, and states that moving a block has no route at all. Every one of those claims is now false: `upsertNodes` fails on synced graphs and is out of the client allowlist, and `moveBlock` is confirmed working on all placements. It survived as `API refrence.md` — one typo away from the file that replaced it, which is how two contradictory references stayed in the same folder for as long as they did. |

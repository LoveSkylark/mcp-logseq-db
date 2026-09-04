"""Self-cleaning live probe: can `logseq.DB.upsertNodes` add/remove a page tag?

`add_page_tag`/`remove_page_tag` currently go through the plain HTTP
`logseq.DB.addBlockTag`/`removeBlockTag` aliases because the graph-worker CLI
tag path was confirmed page-UUID-hostile (see `probe_page_tag_cli.py`), which
leaves page tag writes exposed to the ambiguous HTTP write-circuit-breaker
risk that block tags no longer have. Per this project's architecture
(`doc/design.txt`), `upsertNodes()` is the preferred DB mutation primitive
whenever it is both expressible and empirically reliable, so this script
tests an `{"operation":"edit","entityType":"page","data":{"tags":[...]}}`
shape (now accepted by `VerifiedContent.upsert_nodes`) directly against a
running Logseq DB graph and reports three things:

1. Whether the shape is accepted at all (not rejected/timed out).
2. Whether writing `data.tags` REPLACES a page's full tag set or only ADDS
   to it (tests by giving a page an existing tag through the proven
   `add_page_tag` route, then upserting a different single tag and reading
   back the result).
3. Whether writing an empty `data.tags` list can clear all tags, which is the
   mechanism a future `remove_page_tag` implementation would need.

Creates two scratch tags and one scratch page, runs the probes, then deletes
all three regardless of outcome. Run this against a real Logseq DB graph you
don't mind writing throwaway data to.
"""

import asyncio
import uuid

from mcp_logseq_db.client import LogseqDBClient
from mcp_logseq_db.content import VerifiedContent
from mcp_logseq_db.mutations import VerifiedMutations
from mcp_logseq_db.settings import Settings


async def page_tag_ids(client: LogseqDBClient, page_uuid: str) -> set[int]:
    page_state = await client.call("logseq.DB.getPageData", [page_uuid])
    tags = page_state.get("tags") if isinstance(page_state, dict) else None
    if not isinstance(tags, list):
        return set()
    return {tag["id"] for tag in tags if isinstance(tag, dict) and isinstance(tag.get("id"), int)}


async def main() -> None:
    settings = Settings.from_env()
    client = LogseqDBClient(
        settings.api_url,
        settings.api_token,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        read_attempts=settings.read_attempts,
        readback_attempts=settings.readback_attempts,
        readback_delay=settings.readback_delay,
        verify_ssl=settings.verify_ssl,
    )
    content = VerifiedContent(client)
    mutations = VerifiedMutations(client)

    suffix = uuid.uuid4().hex[:10]
    tag_a_uuid: str | None = None
    tag_b_uuid: str | None = None
    page_uuid: str | None = None
    try:
        tag_a = await mutations.create_tag(f"MCP page-tag upsert probe A {suffix}")
        tag_a_uuid = tag_a.verified_state["uuid"]
        tag_a_id = tag_a.verified_state["id"]
        print(f"INFO created scratch tag A {tag_a_uuid}")

        tag_b = await mutations.create_tag(f"MCP page-tag upsert probe B {suffix}")
        tag_b_uuid = tag_b.verified_state["uuid"]
        tag_b_id = tag_b.verified_state["id"]
        print(f"INFO created scratch tag B {tag_b_uuid}")

        page_result = await content.create_page(f"MCP page-tag upsert probe page {suffix}")
        page_uuid = page_result.verified_entities[0]["uuid"]
        print(f"INFO created scratch page {page_uuid}")

        # Step 1: give the page tag A through the proven native route.
        await mutations.add_page_tag(page_uuid, tag_a_uuid)
        before_ids = await page_tag_ids(client, page_uuid)
        print(f"INFO page tags after native add_page_tag(A): {before_ids}")

        # Step 2: dry-run upsert_nodes with only tag B, then commit.
        edit_op = {
            "operation": "edit",
            "entityType": "page",
            "id": page_uuid,
            "data": {"tags": [tag_b_uuid]},
        }
        try:
            await content.upsert_nodes([edit_op], dry_run=True)
            print("INFO dry-run accepted the edit-page-tags shape")
        except Exception as error:  # noqa: BLE001 - report exact rejection
            print(f"RESULT dry-run rejected the shape: {type(error).__name__}: {error}")
            return

        try:
            result = await content.upsert_nodes([edit_op], dry_run=False)
        except Exception as error:  # noqa: BLE001 - report exact rejection or timeout
            print(f"RESULT live upsert raised {type(error).__name__}: {error}")
            return

        after_ids = await page_tag_ids(client, page_uuid)
        print(f"INFO page tags after upsert_nodes(tags=[B]): {after_ids} verified={result.verified}")
        if after_ids == {tag_b_id}:
            print("RESULT upsertNodes REPLACES the page's full tag set")
        elif after_ids == {tag_a_id, tag_b_id}:
            print("RESULT upsertNodes ADDS to the page's existing tag set (union, not replace)")
        else:
            print(f"RESULT upsertNodes produced an unexpected tag set: {after_ids}")

        # Step 3: does an empty data.tags list clear all tags?
        clear_op = {
            "operation": "edit",
            "entityType": "page",
            "id": page_uuid,
            "data": {"tags": []},
        }
        try:
            result = await content.upsert_nodes([clear_op], dry_run=False)
            cleared_ids = await page_tag_ids(client, page_uuid)
            print(f"INFO page tags after upsert_nodes(tags=[]): {cleared_ids} verified={result.verified}")
            if not cleared_ids:
                print("RESULT upsertNodes CAN clear all page tags with an empty data.tags list")
            else:
                print("RESULT upsertNodes did NOT clear page tags with an empty data.tags list")
        except Exception as error:  # noqa: BLE001 - report exact rejection or timeout
            print(f"RESULT clearing tags raised {type(error).__name__}: {error}")
    finally:
        if page_uuid is not None:
            try:
                await content.delete_page(page_uuid)
                print(f"INFO cleaned up scratch page {page_uuid}")
            except Exception as error:  # noqa: BLE001 - best-effort cleanup
                print(f"WARN failed to clean up scratch page {page_uuid}: {error}")
        if tag_a_uuid is not None:
            try:
                await mutations.delete_tag(tag_a_uuid)
                print(f"INFO cleaned up scratch tag A {tag_a_uuid}")
            except Exception as error:  # noqa: BLE001 - best-effort cleanup
                print(f"WARN failed to clean up scratch tag A {tag_a_uuid}: {error}")
        if tag_b_uuid is not None:
            try:
                await mutations.delete_tag(tag_b_uuid)
                print(f"INFO cleaned up scratch tag B {tag_b_uuid}")
            except Exception as error:  # noqa: BLE001 - best-effort cleanup
                print(f"WARN failed to clean up scratch tag B {tag_b_uuid}: {error}")


if __name__ == "__main__":
    asyncio.run(main())

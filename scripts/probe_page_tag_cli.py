"""Self-cleaning live probe: does the graph-worker CLI accept a page UUID?

`add_block_tag`/`remove_block_tag` use `LogseqDBClient.update_block_tag_via_cli`
(the `upsert block --uuid ... --update-tags/--remove-tags` graph-worker CLI
path) because the equivalent DB HTTP alias was found to time out in mixed
write sequences. `add_page_tag`/`remove_page_tag` still go through the plain
HTTP `logseq.DB.addBlockTag` alias because the CLI path was assumed to be
block-only. This script actually calls the CLI path with a page UUID instead
of assuming, and reports whether it succeeds, is rejected, or hangs.

Creates one scratch tag and one scratch page, attempts the CLI tag update
against the page, reads back the result, then deletes both regardless of
outcome. Run this against a real Logseq DB graph you don't mind writing a
throwaway page/tag to.
"""

import asyncio
import uuid

from mcp_logseq_db.client import LogseqDBClient
from mcp_logseq_db.content import VerifiedContent
from mcp_logseq_db.mutations import VerifiedMutations
from mcp_logseq_db.settings import Settings


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
    tag_uuid: str | None = None
    page_uuid: str | None = None
    try:
        tag_result = await mutations.create_tag(f"MCP page-tag CLI probe tag {suffix}")
        tag_uuid = tag_result.verified_state["uuid"]
        tag_ident = tag_result.verified_state["ident"]
        print(f"INFO created scratch tag {tag_uuid} ({tag_ident})")

        page_result = await content.create_page(f"MCP page-tag CLI probe page {suffix}")
        page_uuid = page_result.verified_entities[0]["uuid"]
        print(f"INFO created scratch page {page_uuid}")

        try:
            cli_response = await client.update_block_tag_via_cli(
                page_uuid, tag_ident, remove=False
            )
            print(f"INFO CLI call returned without raising: {cli_response!r}")
        except Exception as error:  # noqa: BLE001 - report exact rejection or timeout
            print(f"INFO CLI call raised {type(error).__name__}: {error}")

        page_state = await client.call("logseq.DB.getPageData", [page_uuid])
        tags = page_state.get("tags") if isinstance(page_state, dict) else None
        tag_ids = {t.get("id") for t in tags} if isinstance(tags, list) else set()
        applied = tag_result.verified_state["id"] in tag_ids if tags else False
        if applied:
            print("RESULT graph-worker CLI DOES accept page UUIDs and applied the tag")
        else:
            print("RESULT graph-worker CLI did NOT apply the tag to the page")
    finally:
        if tag_uuid is not None:
            try:
                await mutations.delete_tag(tag_uuid)
                print(f"INFO cleaned up scratch tag {tag_uuid}")
            except Exception as error:  # noqa: BLE001 - best-effort cleanup
                print(f"WARN failed to clean up scratch tag {tag_uuid}: {error}")
        if page_uuid is not None:
            try:
                await content.delete_page(page_uuid)
                print(f"INFO cleaned up scratch page {page_uuid}")
            except Exception as error:  # noqa: BLE001 - best-effort cleanup
                print(f"WARN failed to clean up scratch page {page_uuid}: {error}")


if __name__ == "__main__":
    asyncio.run(main())

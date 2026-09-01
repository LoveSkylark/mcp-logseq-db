"""Non-destructive reliability checks against a running Logseq DB graph."""

import asyncio
import json
import os
import uuid

import httpx

from mcp_logseq_db.client import LogseqDBClient
from mcp_logseq_db.settings import Settings


async def raw_call(
    url: str,
    token: str,
    method: str,
    args: list[object],
    read_timeout: float,
    verify_ssl: bool,
) -> httpx.Response:
    timeout = httpx.Timeout(connect=3, read=read_timeout, write=3, pool=3)
    async with httpx.AsyncClient(
        timeout=timeout,
        verify=verify_ssl,
        headers={"Authorization": f"Bearer {token}", "Connection": "close"},
    ) as client:
        return await client.post(
            f"{url.rstrip('/')}/api",
            json={"method": method, "args": args},
        )


async def require_normal_read(client: LogseqDBClient, label: str) -> None:
    result = await client.call("logseq.DB.getAllTags", [])
    if not isinstance(result, list):
        raise RuntimeError(f"{label}: expected tag list")
    print(f"PASS {label}")


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

    app_info = await client.call("logseq.DB.getAppInfo", [])
    print(f"INFO Logseq version {app_info.get('version') if isinstance(app_info, dict) else 'unknown'}")
    await require_normal_read(client, "initial read")

    try:
        await raw_call(
            settings.api_url,
            settings.api_token,
            "logseq.DB.getBlock",
            ["00000000-0000-0000-0000-000000000000"],
            0.25,
            settings.verify_ssl,
        )
        print("INFO timeout probe returned before deadline")
    except httpx.TimeoutException:
        print("PASS intentional timeout observed")
    await require_normal_read(client, "read after timeout")

    interrupted = asyncio.create_task(
        raw_call(
            settings.api_url,
            settings.api_token,
            "logseq.DB.getBlock",
            ["00000000-0000-0000-0000-000000000000"],
            10,
            settings.verify_ssl,
        )
    )
    await asyncio.sleep(0)
    interrupted.cancel()
    try:
        await interrupted
    except asyncio.CancelledError:
        print("PASS request cancellation observed")
    await require_normal_read(client, "read after cancellation")

    concurrent = await asyncio.gather(
        *(client.call("logseq.DB.getAllTags", []) for _ in range(5))
    )
    if not all(isinstance(result, list) for result in concurrent):
        raise RuntimeError("concurrent reads returned an unexpected shape")
    print("PASS five concurrent isolated reads")

    prefix = f"MCP reliability dry run {uuid.uuid4().hex[:10]}"
    operations = [
        {
            "operation": "add",
            "entityType": "page",
            "data": {"title": f"{prefix} {index:02d}"},
        }
        for index in range(50)
    ]
    await client.call("logseq.DB.upsertNodes", [operations, {"dry-run": True}])
    title = operations[0]["data"]["title"]
    query = (
        "[:find ?entity . :where "
        f"[?entity :block/title {json.dumps(title)}]]"
    )
    committed = await client.call("logseq.DB.datascriptQuery", [query])
    if committed is not None:
        raise RuntimeError("dry-run unexpectedly committed an entity")
    print("PASS 50-operation dry-run did not commit")
    print("PASS live reliability sequence complete")


if __name__ == "__main__":
    asyncio.run(main())
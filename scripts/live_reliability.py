"""
Live checks against a running Logseq DB graph.

Two kinds of check live here, and the second is the point.

RELIABILITY asks whether the transport holds up: does a timeout poison the
next request, does cancellation leave the client wedged, do concurrent reads
stay isolated. The unit suite covers this against fakes; here it runs against
the real worker.

CONTRACT asks whether our MODEL OF LOGSEQ is still true. The unit tests cannot
answer that -- the fakes encode the same beliefs the code does, so a belief
that goes stale stays invisible to them. Every wrong assumption found so far
was of this kind:

    removeBlock was reported unavailable; it works
    page-id was assumed to mean "page"; it is a parent pointer
    property writes were assumed unrestricted; they are namespaced
    a success response was treated as evidence; it is not

A contract check that starts failing means Logseq changed, or we were wrong.
Either way the fakes are now lying and the code needs revisiting.

Usage:
    python scripts/live_reliability.py             # read-only, safe anywhere
    python scripts/live_reliability.py --write     # also exercises writes

Read-only mode probes write methods with deliberately invalid arguments, which
proves a method exists without mutating anything. --write creates a scratch
page, exercises the real write paths against it, and recycles it afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

import httpx

from mcp_logseq_db.client import LogseqDBClient
from mcp_logseq_db.settings import Settings

BAD_ARG = "__live_contract_probe__"
NIL_UUID = "00000000-0000-0000-0000-000000000000"

_failures: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"PASS {label}" + (f" -- {detail}" if detail else ""))


def info(label: str) -> None:
    print(f"INFO {label}")


def fail(label: str, detail: str) -> None:
    """Record rather than raise, so one stale belief does not hide the rest."""
    _failures.append(f"{label}: {detail}")
    print(f"FAIL {label} -- {detail}")


async def raw_call(settings: Settings, method: str, args: list[Any],
                   read_timeout: float) -> httpx.Response:
    """Bypass the client's allowlist, for probes it deliberately forbids."""
    timeout = httpx.Timeout(connect=3, read=read_timeout, write=3, pool=3)
    async with httpx.AsyncClient(
        timeout=timeout,
        verify=settings.verify_ssl,
        headers={"Authorization": f"Bearer {settings.api_token}",
                 "Connection": "close"},
    ) as client:
        return await client.post(
            f"{settings.api_url.rstrip('/')}/api",
            json={"method": method, "args": args})


# ------------------------------------------------------------ reliability

async def require_normal_read(client: LogseqDBClient, label: str) -> None:
    result = await client.call("logseq.DB.getAllTags", [])
    if not isinstance(result, list):
        fail(label, "expected a tag list")
        return
    ok(label)


async def reliability(client: LogseqDBClient, settings: Settings) -> None:
    print("\n=== reliability ===")
    await require_normal_read(client, "initial read")

    try:
        await raw_call(settings, "logseq.DB.getBlock", [NIL_UUID], 0.25)
        info("timeout probe returned before its deadline")
    except httpx.TimeoutException:
        ok("intentional timeout observed")
    await require_normal_read(client, "read after timeout")

    interrupted = asyncio.create_task(
        raw_call(settings, "logseq.DB.getBlock", [NIL_UUID], 10))
    await asyncio.sleep(0)
    interrupted.cancel()
    try:
        await interrupted
    except asyncio.CancelledError:
        ok("request cancellation observed")
    await require_normal_read(client, "read after cancellation")

    results = await asyncio.gather(
        *(client.call("logseq.DB.getAllTags", []) for _ in range(5)))
    if all(isinstance(r, list) for r in results):
        ok("five concurrent isolated reads")
    else:
        fail("concurrent reads", "unexpected shape")


# --------------------------------------------------------------- contract

async def probe(settings: Settings, method: str, args: list[Any]) -> str:
    """
    Classify a method without mutating anything.

    An invalid argument provokes a validation error from a method that exists
    and a not-supported error from one that does not. A null tells us nothing
    -- which is itself the finding, and why writes are never trusted on their
    response alone.
    """
    try:
        response = await raw_call(settings, method, args, 10)
    except httpx.TimeoutException:
        return "timeout"
    body = response.text.lower()
    if "supported" in body and "not" in body or "n't supported" in body:
        return "unsupported"
    if any(marker in body for marker in
           ("invalid", "missing required key", "disallowed key",
            "should be either", "can't include", "required")):
        return "exists"
    if response.text.strip() in ("", "null"):
        return "null"
    return "responded"


async def contract(client: LogseqDBClient, settings: Settings) -> None:
    print("\n=== contract ===")

    # These three were reported as rejected by an earlier capability list and
    # routed around for months. If this check ever regresses, verify against
    # the API directly before believing it.
    for method, args in (
        ("logseq.DB.getBlock", [BAD_ARG]),
        ("logseq.DB.removeBlock", [BAD_ARG]),
        ("logseq.DB.updateBlock", [BAD_ARG, BAD_ARG]),
    ):
        verdict = await probe(settings, method, args)
        if verdict in ("exists", "null", "responded"):
            ok(f"{method} is reachable", verdict)
        else:
            fail(f"{method} is reachable", verdict)

    # upsertNodes accepts exactly three combinations. A fourth appearing means
    # the operation table in the architecture doc is out of date.
    verdict = await probe(settings, "logseq.DB.upsertNodes", [[{
        "operation": "edit", "entityType": "page",
        "id": NIL_UUID, "data": {"title": "x"}}]])
    if verdict == "unsupported":
        ok("edit+page is still unsupported")
    else:
        fail("edit+page is still unsupported",
             f"got {verdict} -- page editing may now be possible")

    # The operation vocabulary. No retraction verb is why tag removal cannot
    # go through upsertNodes.
    response = await raw_call(settings, "logseq.DB.upsertNodes", [[{
        "operation": BAD_ARG, "entityType": BAD_ARG,
        "id": NIL_UUID, "data": {}}]], 10)
    if "add or edit" in response.text:
        ok("operation vocabulary is still add|edit")
    else:
        fail("operation vocabulary is still add|edit",
             response.text.strip()[:160])

    # `data` is a closed allowlist. If parent-id is ever accepted, nesting has
    # a second route and createBlock's contract can be widened.
    response = await raw_call(settings, "logseq.DB.upsertNodes", [[{
        "operation": "add", "entityType": "block",
        "data": {"page-id": NIL_UUID, "title": "x", "parent-id": NIL_UUID}}]], 10)
    if "disallowed" in response.text.lower():
        ok("data allowlist still rejects parent-id")
    else:
        fail("data allowlist still rejects parent-id",
             response.text.strip()[:160])

    # The property sandbox. If this stops holding, user-namespace properties
    # become writable and a whole class of tool constraints can be dropped.
    response = await raw_call(settings, "logseq.DB.upsertProperty",
                              [f"{BAD_ARG}/Name", {"type": "default"}], 10)
    if "can't include" in response.text or "/" in response.text:
        ok("upsertProperty still rejects a namespaced title")
    else:
        info(f"upsertProperty namespaced-title response: "
             f"{response.text.strip()[:120]}")

    # Recycled pages keep the Page class, so every page listing must exclude
    # them explicitly or they appear live.
    recycled = await client.call("logseq.DB.datascriptQuery", [
        "[:find (count ?page) . :where "
        "[?page :logseq.property/deleted-at _]]"])
    if isinstance(recycled, int):
        ok("recycled pages are still queryable", f"{recycled} present")
    else:
        info("no recycled pages in this graph")


# ----------------------------------------------------------- write checks

async def writes(client: LogseqDBClient, settings: Settings) -> None:
    """
    Exercise the real write paths on a scratch page, then clean up.

    Nothing here touches existing content. The page is recycled at the end;
    recycling preserves the entity, so it stays queryable rather than
    vanishing -- see the note printed on completion.
    """
    print("\n=== writes (scratch page) ===")
    marker = uuid.uuid4().hex[:8]
    title = f"MCP live check {marker}"

    await client.call("logseq.DB.upsertNodes", [
        [{"operation": "add", "entityType": "page", "data": {"title": title}}],
        {"dry-run": False}])
    page = await client.call("logseq.DB.datascriptQuery", [
        "[:find (pull ?page [:db/id :block/uuid]) . :where "
        f"[?page :block/name] [?page :block/title {json.dumps(title)}]]"])
    if not isinstance(page, dict):
        fail("scratch page created", "page not found after creation")
        return
    page_uuid = page["uuid"]
    ok("scratch page created", page_uuid)

    async def children_of(parent_uuid: str) -> list[dict[str, Any]]:
        return await client.call("logseq.DB.datascriptQuery", [
            "[:find [(pull ?child [:db/id :block/uuid :block/title]) ...] "
            f":where [?parent :block/uuid #uuid \"{parent_uuid}\"] "
            "[?child :block/parent ?parent]]"]) or []

    # A block parented by the page.
    await client.call("logseq.DB.upsertNodes", [
        [{"operation": "add", "entityType": "block",
          "data": {"page-id": page_uuid, "title": "parent block"}}],
        {"dry-run": False}])
    top = [b for b in await children_of(page_uuid)
           if b["title"] == "parent block"]
    if not top:
        fail("top-level block created", "not present after write")
        return
    ok("top-level block created")

    # THE finding: page-id accepts a BLOCK uuid and nests. If this fails,
    # nested creation has no HTTP route and createBlock's contract is wrong.
    await client.call("logseq.DB.upsertNodes", [
        [{"operation": "add", "entityType": "block",
          "data": {"page-id": top[0]["uuid"], "title": "nested block"}}],
        {"dry-run": False}])
    nested = [b for b in await children_of(top[0]["uuid"])
              if b["title"] == "nested block"]
    if nested:
        ok("page-id accepts a block uuid and nests")
    else:
        fail("page-id accepts a block uuid and nests",
             "the child was not created under the block")

    # A name where a uuid belongs: success, and nothing written. This is the
    # failure mode the whole verification layer exists for.
    await client.call("logseq.DB.upsertNodes", [
        [{"operation": "add", "entityType": "block",
          "data": {"page-id": title, "title": "should not exist"}}],
        {"dry-run": False}])
    stray = await client.call("logseq.DB.datascriptQuery", [
        '[:find (count ?block) . :where '
        '[?block :block/title "should not exist"]]'])
    if not stray:
        ok("a page NAME as page-id still fails silently",
            "reported success, wrote nothing")
    else:
        fail("a page NAME as page-id still fails silently",
             "it created a block -- names may now resolve")

    # Deletion, and that it takes the subtree.
    await client.call("logseq.DB.removeBlock", [top[0]["uuid"]])
    remaining = await children_of(page_uuid)
    if not any(b["title"] == "parent block" for b in remaining):
        ok("removeBlock deleted the block over HTTP")
    else:
        fail("removeBlock deleted the block over HTTP", "still present")

    await client.call("logseq.DB.deletePage", [title])
    info(f"scratch page recycled: {title}")
    info("recycling preserves the entity, so it remains queryable")


# ------------------------------------------------------------------ main

async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="also exercise write paths on a scratch page")
    parser.add_argument("--skip-reliability", action="store_true")
    args = parser.parse_args()

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
    version = (app_info.get("version")
               if isinstance(app_info, dict) else "unknown")
    info(f"Logseq {version}")
    if version != "2.0.1":
        info("this is not the version the tools were verified against; "
             "a contract failure below may be a version difference")

    if not args.skip_reliability:
        await reliability(client, settings)
    await contract(client, settings)
    if args.write:
        await writes(client, settings)

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for failure in _failures:
            print(f"  - {failure}")
        print("\nA contract failure means Logseq changed or the model was "
              "wrong. Confirm by hand before changing code -- and update the "
              "fakes in tests/, which currently encode the old belief.")
        return 1

    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
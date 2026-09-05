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


# --------------------------------------------------------------- explore

async def explore(client: LogseqDBClient, settings: Settings,
                  *, allow_writes: bool) -> None:
    """
    Probe the questions that are open rather than settled.

    Nothing here is a pass/fail check -- these report what the API does so a
    decision can be made. Findings that turn out to be stable belong in
    `contract` afterwards, where a regression would be caught.
    """
    print("\n=== explore: block movement ===")

    # A move is conceptually two attribute writes: :block/parent and
    # :block/order. The question is only whether any route accepts them.
    for label, data in (
        ("edit+block with page-id",
         {"title": "x", "page-id": NIL_UUID}),
        ("edit+block with order",
         {"title": "x", "order": "a0"}),
        ("edit+block with parent-id",
         {"title": "x", "parent-id": NIL_UUID}),
        ("edit+block with block/parent",
         {"title": "x", "block/parent": NIL_UUID}),
    ):
        response = await raw_call(settings, "logseq.DB.upsertNodes", [[{
            "operation": "edit", "entityType": "block",
            "id": NIL_UUID, "data": data}]], 10)
        body = response.text.strip()[:150]
        if "disallowed" in body.lower():
            info(f"{label}: rejected as a disallowed key")
        elif "invalid" in body.lower() or "missing" in body.lower():
            ok(f"{label}: key ACCEPTED by the schema", body)
        else:
            info(f"{label}: {body}")

    # Methods that might move a block directly. None is in the client
    # allowlist, so these go through raw_call.
    for method, probe_args in (
        ("logseq.DB.moveBlock", [NIL_UUID, NIL_UUID, {}]),
        ("logseq.DB.insertBatchBlock", [NIL_UUID, [], {}]),
        ("logseq.DB.prependBlockInPage", [NIL_UUID, BAD_ARG]),
    ):
        verdict = await probe(settings, method, probe_args)
        if verdict == "unsupported":
            info(f"{method}: not available")
        elif verdict == "exists":
            ok(f"{method}: EXISTS -- a move route may be possible", verdict)
        else:
            info(f"{method}: {verdict}")

    print("\n=== explore: property namespaces ===")

    properties = await client.call("logseq.DB.getAllProperties", []) or []
    namespaces: dict[str, int] = {}
    for entry in properties:
        ident = entry.get("ident") if isinstance(entry, dict) else None
        if not isinstance(ident, str):
            continue
        bare = ident[1:] if ident.startswith(":") else ident
        namespace = bare.split("/", 1)[0] if "/" in bare else "(no namespace)"
        namespaces[namespace] = namespaces.get(namespace, 0) + 1

    for namespace, count in sorted(namespaces.items(),
                                   key=lambda pair: -pair[1]):
        info(f"{count:3d}  {namespace}")

    plugin_namespaces = [n for n in namespaces if n.startswith("plugin.property.")]
    if plugin_namespaces:
        callers = sorted({n.split(".", 2)[2] for n in plugin_namespaces
                          if n.count(".") >= 2})
        info(f"plugin caller ids present: {', '.join(callers)}")
        if len(callers) > 1:
            ok("more than one caller id exists",
               "so namespaces are per-caller, not global")
    else:
        info("no plugin.property.* namespace exists yet; create one to find "
             "out this caller's id")

    if not allow_writes:
        info("pass --write to discover this caller's id by creating a property")
        return

    # The caller id is assigned, not chosen. Creating a property and reading
    # the ident back is the only way to learn it.
    marker = uuid.uuid4().hex[:6]
    created = await client.call(
        "logseq.DB.upsertProperty", [f"MCPProbe{marker}", {"type": "default"}])
    ident = created.get("ident") if isinstance(created, dict) else None
    if not ident:
        fail("caller id discovery", "upsertProperty returned no ident")
        return
    ok("this caller writes to", ident.rsplit("/", 1)[0])

    # Can a property in someone else's namespace be written? If not, there is
    # no shared space and integrations are mutually invisible.
    for target, why in (
        (":user.property/__mcp_probe__", "UI-created namespace"),
        (":plugin.property.__other__/__mcp_probe__", "another plugin"),
    ):
        response = await raw_call(settings, "logseq.DB.upsertBlockProperty",
                                  [NIL_UUID, target, "x"], 10)
        body = response.text.strip()[:120]
        if "own properties" in body or "denied" in body.lower():
            info(f"{why}: refused ({body})")
        else:
            ok(f"{why}: NOT refused -- investigate", body)

    info(f"probe property {ident} was created and left in place; "
         "remove it manually if unwanted")


# ------------------------------------------------------------------ main

async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="also exercise write paths on a scratch page")
    parser.add_argument("--explore", action="store_true",
                        help="probe open questions: block movement, property "
                             "namespaces. Read-only unless --write is also set.")
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
    if args.explore:
        await explore(client, settings, allow_writes=args.write)

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

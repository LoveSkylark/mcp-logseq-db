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
    the parent argument was assumed to mean "page"; it takes either kind
    property writes were assumed unrestricted; they are namespaced
    a success response was treated as evidence; it is not
    upsertNodes was assumed to work everywhere; it fails on synced graphs
    a page NAME was assumed never to resolve; insertBlock resolves one

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

    # The routes everything now depends on. `createBlock`, `importPage`,
    # `createPageofBlocks`, `moveBlock` and the page tools all sit on these,
    # and each replaced an `upsertNodes` path.
    for method, args in (
        ("logseq.DB.createPage", [""]),
        ("logseq.DB.insertBlock", [BAD_ARG, BAD_ARG, {}]),
        ("logseq.DB.insertBatchBlock", [BAD_ARG, [], {}]),
        ("logseq.DB.moveBlock", [BAD_ARG, BAD_ARG, {}]),
        ("logseq.DB.renamePage", [BAD_ARG, BAD_ARG]),
    ):
        verdict = await probe(settings, method, args)
        if verdict in ("exists", "null", "responded"):
            ok(f"{method} is reachable", verdict)
        else:
            fail(f"{method} is reachable", verdict)

    # upsertNodes is deliberately out of the client allowlist: it fails on
    # SYNCED graphs with "The Imported EDN has N validation error(s)" for a
    # write its own dry run accepts. That cannot be probed read-only -- a
    # local graph accepts it and a synced one does not -- so what used to be
    # three contract checks on its operation vocabulary and `data` allowlist
    # are gone. They described the contract of a route nothing uses. If you
    # need them, they are in git history alongside the design they belonged
    # to.

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

    Every call goes through a route the tool surface actually uses. An earlier
    version of this function drove everything through `upsertNodes`, which is
    no longer in the client allowlist -- so `--write` raised before it checked
    anything.
    """
    print("\n=== writes (scratch page) ===")
    marker = uuid.uuid4().hex[:8]
    title = f"MCP live check {marker}"

    async def entity(entity_uuid: str) -> dict[str, Any] | None:
        found = await client.call("logseq.DB.datascriptQuery", [
            "[:find (pull ?e [*]) . :where "
            f"[?e :block/uuid #uuid \"{entity_uuid}\"]]"])
        return found if isinstance(found, dict) else None

    async def children_of(parent_uuid: str) -> list[dict[str, Any]]:
        found = await client.call("logseq.DB.datascriptQuery", [
            "[:find [(pull ?child [:db/id :block/uuid :block/title "
            ":block/order {:block/parent [:db/id]} {:block/page [:db/id]}]) "
            f"...] :where [?parent :block/uuid #uuid \"{parent_uuid}\"] "
            "[?child :block/parent ?parent]]"]) or []
        # Sorted, because pull does not guarantee order and the prepend check
        # below is entirely about order.
        return sorted(found, key=lambda child: str(child.get("order", "")))

    def uuid_of(response: Any) -> str | None:
        if isinstance(response, list):
            response = response[0] if response else None
        if isinstance(response, dict) and isinstance(response.get("uuid"), str):
            return response["uuid"]
        return None

    created = await client.call("logseq.DB.createPage", [title])
    page_uuid = uuid_of(created)
    if page_uuid:
        ok("createPage returns the created page", page_uuid)
    else:
        page = await client.call("logseq.DB.datascriptQuery", [
            "[:find (pull ?page [:db/id :block/uuid]) . :where "
            f"[?page :block/name] [?page :block/title {json.dumps(title)}]]"])
        page_uuid = uuid_of(page)
        info("createPage returned no usable entity; resolved by title instead")
    if not page_uuid:
        fail("scratch page created", "page not found after creation")
        return
    page = await entity(page_uuid)
    page_id = page.get("id") if page else None

    # Idempotent on title: this is what makes createPage safe to call twice
    # and why the duplicate guard in create_page is no longer load-bearing.
    again = await client.call("logseq.DB.createPage", [title])
    if uuid_of(again) in (page_uuid, None):
        ok("createPage is still idempotent on title")
    else:
        fail("createPage is still idempotent on title",
             f"a second call returned {uuid_of(again)}")

    # A block parented by the page. insertBlock RETURNS the entity, which is
    # what removed the read-back cycle from outline building.
    top_response = await client.call(
        "logseq.DB.insertBlock", [page_uuid, "parent block", {"sibling": False}])
    top_uuid = uuid_of(top_response)
    if top_uuid:
        ok("insertBlock returns the created entity", top_uuid)
    else:
        top = [b for b in await children_of(page_uuid)
               if b["title"] == "parent block"]
        top_uuid = top[0]["uuid"] if top else None
        fail("insertBlock returns the created entity",
             "nothing usable came back; fell back to a read")
    if not top_uuid:
        fail("top-level block created", "not present after write")
        return

    # THE finding: the parent argument accepts a BLOCK uuid and nests, and
    # insertBlock sets :block/parent and :block/page INDEPENDENTLY. upsertNodes
    # wrote its single page-id into both, which produced a real child whose
    # owning page was its parent block -- invisible to every page-scoped query.
    nested_uuid = uuid_of(await client.call(
        "logseq.DB.insertBlock", [top_uuid, "nested block", {"sibling": False}]))
    nested = await entity(nested_uuid) if nested_uuid else None
    if nested is None:
        fail("a block uuid as parent nests", "the child was not created")
    else:
        parent_id = (nested.get("parent") or {}).get("id")
        owning_page = (nested.get("page") or {}).get("id")
        top = await entity(top_uuid)
        if parent_id == (top or {}).get("id") and owning_page == page_id:
            ok("a block uuid as parent nests, and ownership stays with the page")
        else:
            fail("a block uuid as parent nests, and ownership stays with the "
                 "page", f"parent={parent_id} page={owning_page}")

    # A batch of siblings in one call, in the order they were written.
    batch = await client.call("logseq.DB.insertBatchBlock", [
        page_uuid, [{"content": "batch one"}, {"content": "batch two"}],
        {"sibling": False}])
    batch_uuids = [e["uuid"] for e in (batch if isinstance(batch, list) else [])
                   if isinstance(e, dict) and isinstance(e.get("uuid"), str)]
    if len(batch_uuids) == 2:
        ok("insertBatchBlock returns one entity per requested block")
    else:
        fail("insertBatchBlock returns one entity per requested block",
             f"asked for 2, got {len(batch_uuids)}")

    # A name where a uuid belongs. THE BELIEF THIS CHECKED WAS WRONG, and it
    # was wrong about the route rather than the behaviour: "names never
    # resolve" was established against `upsertNodes`, whose `page-id` field
    # silently ignored a page name. Block creation moved to `insertBlock` and
    # nobody re-tested it. Observed 2026-09-20 on
    # 2.0.1-alpha+nightly.20260826: insertBlock RESOLVES a page title and
    # creates a top-level block on that page.
    #
    # Neither outcome is a fault, so this records which one holds rather than
    # failing. What matters is that the tool surface validates UUIDs at the
    # boundary -- `create_block` calls `_validated_uuid`, so a name never
    # reaches this method through a tool. That guard used to be belt and
    # braces; now it is the only thing between a mistyped argument and a
    # block created somewhere nobody asked for.
    await client.call(
        "logseq.DB.insertBlock", [title, "name resolution probe",
                                  {"sibling": False}])
    landed = await client.call("logseq.DB.datascriptQuery", [
        "[:find [(pull ?block [:db/id :block/uuid "
        "{:block/page [:db/id]} {:block/parent [:db/id]}]) ...] "
        ':where [?block :block/title "name resolution probe"]]']) or []
    on_this_page = [
        b for b in landed
        if isinstance(b, dict)
        and (b.get("page") or {}).get("id") == page_id
    ]
    if not landed:
        ok("a page NAME as the parent writes nothing",
           "the pre-2026 behaviour; the boundary guard is belt and braces")
    elif on_this_page:
        ok("a page NAME as the parent RESOLVES to the page",
           f"{len(on_this_page)} block(s) created -- insertBlock accepts a "
           "title where upsertNodes ignored one, so boundary UUID validation "
           "is load-bearing")
    else:
        fail("a page NAME as the parent lands somewhere predictable",
             f"{len(landed)} block(s) were created but none on this page; "
             "the name resolved to something else entirely")

    # Moving, which this file once recorded as having no route at all.
    if nested_uuid and batch_uuids:
        await client.call("logseq.DB.moveBlock",
                          [nested_uuid, batch_uuids[0], {"children": True}])
        moved = await entity(nested_uuid)
        target = await entity(batch_uuids[0])
        if moved and target and (moved.get("parent") or {}).get("id") == target.get("id"):
            ok("moveBlock reparents a block")
        else:
            fail("moveBlock reparents a block", "the parent did not change")

        # And the no-op: moving it to the parent it already has changes
        # nothing and still returns null, which is why the tool reports that
        # as verified=false rather than as success.
        await client.call("logseq.DB.moveBlock",
                          [nested_uuid, batch_uuids[0], {"children": True}])
        unchanged = await entity(nested_uuid)
        if unchanged and moved and unchanged.get("order") == moved.get("order"):
            ok("moveBlock still no-ops when the position would not change")
        else:
            info("a repeated move changed :block/order; the no-op behaviour "
                 "may have changed")

        # `children: true` PREPENDS. This is the behaviour the last-child
        # placement exists to work around, and the reason three pages of
        # content ended up reversed before anyone read the destination back.
        # If it ever starts appending, last-child becomes redundant -- so it
        # is checked here rather than assumed.
        await client.call("logseq.DB.insertBlock",
                          [batch_uuids[0], "sibling for ordering",
                           {"sibling": False}])
        await client.call("logseq.DB.moveBlock",
                          [nested_uuid, batch_uuids[0], {"children": True}])
        ordered = await children_of(batch_uuids[0])
        first = ordered[0].get("uuid") if ordered else None
        if len(ordered) > 1 and first == nested_uuid:
            ok("children:true still PREPENDS",
               "so last-child is still needed to append")
        elif len(ordered) > 1:
            info("children:true no longer prepends -- last-child may now be "
                 "redundant, and the docs claim it is needed")
        else:
            info("not enough siblings to tell prepend from append")

    # Deletion, and that it takes the subtree.
    await client.call("logseq.DB.removeBlock", [top_uuid])
    if await entity(top_uuid) is None:
        ok("removeBlock deleted the block over HTTP")
    else:
        fail("removeBlock deleted the block over HTTP", "still present")

    # By UUID, which is the form the tool sends. Recycling, not destruction.
    await client.call("logseq.DB.deletePage", [page_uuid])
    recycled = await entity(page_uuid)
    if recycled is None:
        info(f"scratch page {title} is gone entirely, not recycled")
    elif recycled.get(":logseq.property/deleted-at") is not None:
        ok("deletePage accepts the page UUID and recycles", page_uuid)
    else:
        fail("deletePage accepts the page UUID and recycles",
             "the page is still live; the UUID form may do nothing")
    info("recycling preserves the entity, so it remains queryable")


# --------------------------------------------------------------- explore

async def explore(client: LogseqDBClient, settings: Settings,
                  *, allow_writes: bool) -> None:
    """
    Probe the questions that are open rather than settled.

    Nothing here is a pass/fail check -- these report what the API does so a
    decision can be made. Findings that turn out to be stable belong in
    `contract` afterwards, where a regression would be caught. Block movement
    graduated that way: it was explored here, then confirmed, and the checks
    for it now live in `contract` and `writes`.
    """
    print("\n=== explore: unexposed routes ===")

    # Block movement used to be the open question here, and this section
    # probed four `upsertNodes` edit+block shapes plus three candidate
    # methods looking for a route. `moveBlock` is the route, and it is
    # verified on every placement -- so what is left are the methods that
    # would close the remaining gaps in the tool surface.
    for method, probe_args, would_support in (
        ("logseq.DB.addTagExtends", [NIL_UUID, NIL_UUID],
         "tag inheritance -- a tag's parent"),
        ("logseq.DB.addTagProperty", [NIL_UUID, NIL_UUID],
         "the property slots a tag declares"),
        ("logseq.DB.prependBlockInPage", [NIL_UUID, BAD_ARG],
         "insertion at the top of a page"),
        ("logseq.DB.addPropertyValueChoices", [NIL_UUID, []],
         "closed values on a property this caller owns"),
    ):
        verdict = await probe(settings, method, probe_args)
        if verdict == "unsupported":
            info(f"{method}: not available")
        elif verdict == "exists":
            ok(f"{method}: EXISTS -- would support {would_support}", verdict)
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
                        help="probe open questions: unexposed routes, property "
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

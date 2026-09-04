#!/usr/bin/env python3
"""
Build a two-level outline on a Logseq DB page via the local HTTP API.

Strategy (three calls, regardless of how big the outline is):
  1. one batched upsertNodes  -> create every top-level section
  2. one datascriptQuery      -> read back the server-assigned UUIDs
  3. one batched upsertNodes  -> create every child, parented by UUID

Why it has to be three: block UUIDs are assigned by Logseq and are NOT
returned by the create call, and `page-id` does not resolve names. So the
children cannot reference their parents until after a read-back.

Key API facts this relies on (all verified against a live graph):
  - `page-id` is really a PARENT pointer. Give it a page UUID for a
    top-level block, or a block UUID for a child.
  - `data` is a closed allowlist: only `page-id` and `title`.
  - The response `{:block N}` is a stock acknowledgement, NOT a count of
    what was written. A bad `page-id` returns success and writes nothing.
    Every write is therefore verified by read-back below.

Usage:
    export LOGSEQ_TOKEN=...
    python3 logseq_build_outline.py <page-uuid> outline.txt
    python3 logseq_build_outline.py <page-uuid> outline.txt --dry-run

Outline file format -- indentation marks children:
    Section 1
        Alpha
        Beta
    Section 2
        Charly
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("LOGSEQ_API_URL", "http://127.0.0.1:12315/api")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def call(method, args, url, token, timeout=15):
    """POST one Logseq API call and return the decoded response."""
    payload = json.dumps({"method": method, "args": args}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit("HTTP %s from Logseq:\n%s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise SystemExit(
            "Could not reach Logseq at %s (%s).\n"
            "Is the HTTP APIs server running? "
            "Settings > Features > HTTP APIs server." % (url, e.reason)
        )

    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


# --------------------------------------------------------------------------
# outline parsing
# --------------------------------------------------------------------------

def parse_outline(text):
    """
    Parse an indented outline into [(section_title, [child, ...]), ...].

    Any indentation (spaces or tabs) marks a child of the most recent
    unindented line. Blank lines are ignored.
    """
    sections = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        indented = raw[0] in " \t"
        title = raw.strip()
        if indented:
            if not sections:
                raise SystemExit(
                    "Line %d is indented but no section precedes it: %r"
                    % (lineno, title)
                )
            sections[-1][1].append(title)
        else:
            sections.append((title, []))
    if not sections:
        raise SystemExit("Outline is empty.")
    return sections


def check_unique(sections):
    """
    Section titles must be unique -- step 2 maps title -> UUID, and a
    duplicate title makes that mapping ambiguous.
    """
    titles = [s for s, _ in sections]
    dupes = {t for t in titles if titles.count(t) > 1}
    if dupes:
        raise SystemExit(
            "Section titles must be unique within a run; duplicates: %s"
            % ", ".join(sorted(dupes))
        )


# --------------------------------------------------------------------------
# API operations
# --------------------------------------------------------------------------

def add_block_op(parent_uuid, title):
    return {
        "operation": "add",
        "entityType": "block",
        "data": {"page-id": parent_uuid, "title": title},
    }


def read_children(parent_uuid, url, token):
    """Return {title: uuid} for blocks directly parented by parent_uuid."""
    q = (
        '[:find (pull ?b [:block/uuid :block/title :block/order]) '
        ':where [?p :block/uuid #uuid "%s"] [?b :block/parent ?p]]'
        % parent_uuid
    )
    rows = call("logseq.DB.datascriptQuery", [q], url, token) or []
    out = {}
    for row in rows:
        blk = row[0] if isinstance(row, list) else row
        title = blk.get("title") or blk.get("block/title")
        uuid = blk.get("uuid") or blk.get("block/uuid")
        if title is not None and uuid:
            out[title] = uuid
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Build a two-level outline on a Logseq DB page."
    )
    ap.add_argument("page_uuid", help="UUID of the target page")
    ap.add_argument("outline", help="path to the indented outline file")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--token", default=os.environ.get("LOGSEQ_TOKEN", ""))
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the payloads that would be sent; write nothing",
    )
    args = ap.parse_args()

    if not args.token and not args.dry_run:
        raise SystemExit("No token. Set LOGSEQ_TOKEN or pass --token.")

    with open(args.outline, "r", encoding="utf-8") as fh:
        sections = parse_outline(fh.read())
    check_unique(sections)

    total_children = sum(len(c) for _, c in sections)
    print(
        "Outline: %d sections, %d children"
        % (len(sections), total_children)
    )

    # ---- step 1: create the sections -------------------------------------
    step1 = [add_block_op(args.page_uuid, t) for t, _ in sections]

    if args.dry_run:
        print("\n--- STEP 1 ---")
        print(json.dumps({"method": "logseq.DB.upsertNodes", "args": [step1]}))

    # Blocks already on the page with the same titles would confuse the
    # title -> UUID mapping, so snapshot beforehand and ignore pre-existing.
    before = {} if args.dry_run else read_children(args.page_uuid, args.url, args.token)
    collisions = [t for t, _ in sections if t in before]
    if collisions:
        raise SystemExit(
            "These section titles already exist on the page, so the "
            "read-back could not tell old from new: %s"
            % ", ".join(collisions)
        )

    if not args.dry_run:
        call("logseq.DB.upsertNodes", [step1], args.url, args.token)

    # ---- step 2: read back the assigned UUIDs ----------------------------
    if args.dry_run:
        print("\n--- STEP 2 ---")
        q = (
            '[:find (pull ?b [:block/uuid :block/title :block/order]) '
            ':where [?p :block/uuid #uuid "%s"] [?b :block/parent ?p]]'
            % args.page_uuid
        )
        print(json.dumps({"method": "logseq.DB.datascriptQuery", "args": [q]}))
        section_uuids = {t: "UUID-OF-" + t.replace(" ", "-") for t, _ in sections}
    else:
        after = read_children(args.page_uuid, args.url, args.token)
        section_uuids = {}
        missing = []
        for title, _ in sections:
            if title in after:
                section_uuids[title] = after[title]
            else:
                missing.append(title)
        if missing:
            # This is the silent-failure case: the create call reports
            # success even when it writes nothing.
            raise SystemExit(
                "Step 1 reported success but these sections were not "
                "created: %s\nCheck that the page UUID is correct."
                % ", ".join(missing)
            )
        print("Created %d sections." % len(section_uuids))

    # ---- step 3: create the children -------------------------------------
    step3 = []
    for title, children in sections:
        parent = section_uuids[title]
        for child in children:
            step3.append(add_block_op(parent, child))

    if not step3:
        print("No children to create.")
        return

    if args.dry_run:
        print("\n--- STEP 3 ---")
        print(json.dumps({"method": "logseq.DB.upsertNodes", "args": [step3]}))
        print("\nDry run only; nothing was written.")
        return

    call("logseq.DB.upsertNodes", [step3], args.url, args.token)

    # ---- verify ----------------------------------------------------------
    problems = []
    for title, children in sections:
        got = read_children(section_uuids[title], args.url, args.token)
        for child in children:
            if child not in got:
                problems.append("%s > %s" % (title, child))

    if problems:
        print("\nMISSING after write:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        raise SystemExit(1)

    print("Created %d children. Verified." % len(step3))


if __name__ == "__main__":
    main()
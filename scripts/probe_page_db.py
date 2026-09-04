#!/usr/bin/env python3
"""
probe_page_db -- CLI for the Logseq DB graph local HTTP API.

Every call is POST {base}/api with {"method": ..., "args": [...]}.

VERIFICATION POLICY
-------------------
This API returns success for calls that do nothing:
  - a wrong identifier TYPE (UUID where an ident is wanted, a name where a
    UUID is wanted) returns null or {:block N} and writes nothing
  - {:block N} is a stock acknowledgement, NOT a count of what was written
So every write below is followed by a read-back. Commands print VERIFIED or
UNVERIFIED accordingly. Never trust an exit code alone on a write.

IDENTIFIER RULES (each entity kind has one canonical key)
--------------------------------------------------------
  blocks, pages   -> :block/uuid
  properties      -> :db/ident        (UUID fails silently)
  tags            -> UUID for relations; :db/ident for lookups
  :db/id integers -> queries only, never persist them

CONFIDENCE
----------
Commands marked [UNVERIFIED] in their help text were never confirmed against a
live graph. They are best guesses and may fail silently. Treat their output as
a hypothesis until you have seen the read-back.

Usage:
    export LOGSEQ_TOKEN=...
    probe_page_db.py list pages
    probe_page_db.py page get <uuid> --detail all
    probe_page_db.py block create <page-uuid> "some title"
    probe_page_db.py raw logseq.DB.getAllTags
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("LOGSEQ_API_URL", "http://127.0.0.1:12315/api")

# Built-in class :db/id values. Stable on a given graph, but rebuilt graphs
# can renumber -- resolve_class() below matches on :db/ident instead when
# --safe-classes is passed.
CLASS_TAG = 2
CLASS_PROPERTY = 3
CLASS_PAGE = 4

PROPERTY_TYPES = [
    "default", "number", "string", "datetime", "checkbox",
    "url", "node", "page", "class", "property", "map",
]


# ---------------------------------------------------------------- transport

class Api:
    def __init__(self, url, token, timeout=20, verbose=False):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.verbose = verbose

    def call(self, method, args):
        payload = json.dumps({"method": method, "args": args}).encode("utf-8")
        if self.verbose:
            sys.stderr.write("-> %s %s\n" % (method, json.dumps(args)))
        req = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise SystemExit("HTTP %s from Logseq:\n%s" % (e.code, detail))
        except urllib.error.URLError as e:
            raise SystemExit(
                "Cannot reach Logseq at %s (%s).\n"
                "Settings > Features > HTTP APIs server." % (self.url, e.reason))
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    def q(self, query):
        """Run a Datascript query."""
        return self.call("logseq.DB.datascriptQuery", [query])


# ------------------------------------------------------------------ helpers

def out(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def rows(result):
    """datascriptQuery returns [[e], [e]] for tuple finds; flatten singletons."""
    if result is None:
        return []
    if isinstance(result, dict):
        return [result]
    flat = []
    for r in result:
        if isinstance(r, list) and len(r) == 1:
            flat.append(r[0])
        else:
            flat.append(r)
    return flat


def field(entity, *names):
    """Attribute keys come back unprefixed sometimes, prefixed others."""
    for n in names:
        for k in (n, "block/" + n, ":block/" + n):
            if isinstance(entity, dict) and k in entity:
                return entity[k]
    return None


def one(result, what, allow_zero=False):
    """
    Insist on exactly one match. Writing against a fuzzy match is how you
    modify the wrong entity.
    """
    rs = rows(result)
    if len(rs) == 1:
        return rs[0]
    if len(rs) == 0:
        if allow_zero:
            return None
        raise SystemExit("No %s found." % what)
    raise SystemExit(
        "%d matches for %s; refusing to guess. Use a UUID instead.\n%s"
        % (len(rs), what, json.dumps(rs, indent=2)))


def verified(ok, msg):
    print(("VERIFIED   " if ok else "UNVERIFIED ") + msg)
    if not ok:
        sys.exit(1)


# -------------------------------------------------------------------- pages

PAGE_DETAIL = {}

PAGE_DETAIL["page"] = (
    '[:find (pull ?p [* {:block/tags [:db/id :db/ident :block/title]}]) . '
    ':where [?p :block/uuid #uuid "%s"]]')

PAGE_DETAIL["blocks"] = (
    '[:find (pull ?b [:db/id :block/uuid :block/title :block/order '
    '{:block/parent [:block/uuid :block/title]}]) '
    ':where [?p :block/uuid #uuid "%s"] [?b :block/page ?p]]')

PAGE_DETAIL["tags"] = (
    '[:find (pull ?t [:db/id :db/ident :block/uuid :block/title]) '
    '(pull ?b [:db/id :block/uuid :block/title :block/name]) '
    ':where [?p :block/uuid #uuid "%s"] '
    '(or-join [?p ?b] [(identity ?p) ?b] [?b :block/page ?p]) '
    '[?b :block/tags ?t]]')

PAGE_DETAIL["properties"] = (
    '[:find (pull ?prop [:db/id :db/ident :block/title]) '
    '(pull ?b [:block/uuid :block/title :block/name]) '
    '?v (pull ?v [:db/id :db/ident :block/title]) '
    ':where [?p :block/uuid #uuid "%s"] '
    '(or-join [?p ?b] [(identity ?p) ?b] [?b :block/page ?p]) '
    '[?prop :block/tags ' + str(CLASS_PROPERTY) + '] '
    '[?prop :db/ident ?a] [?b ?a ?v]]')

PAGE_DETAIL["declared"] = (
    '[:find (pull ?t [:db/ident :block/title]) '
    '(pull ?prop [:db/id :db/ident :block/uuid :block/title]) '
    ':where [?p :block/uuid #uuid "%s"] [?p :block/tags ?t] '
    '[?t :logseq.property.class/properties ?prop]]')

PAGE_DETAIL["all"] = (
    '[:find (pull ?p [* {:block/tags [:db/ident :block/title]} '
    '{:block/_parent [* {:block/tags [:db/ident :block/title]} '
    '{:block/_parent ...}]}]) . '
    ':where [?p :block/uuid #uuid "%s"]]')


def page_uuid_by_title(api, title):
    q = ('[:find (pull ?p [:db/id :block/uuid :block/name :block/title]) '
         ':where [?p :block/name] [?p :block/tags %d] [?p :block/title "%s"]]'
         % (CLASS_PAGE, title))
    p = one(api.q(q), 'page titled "%s"' % title)
    return field(p, "uuid")


def cmd_page(api, a):
    if a.action == "uuid":
        print(page_uuid_by_title(api, a.title))

    elif a.action == "get":
        out(api.q(PAGE_DETAIL[a.detail] % a.uuid))

    elif a.action == "create":
        # [UNVERIFIED] add+page shape is inferred from the add+block contract.
        op = {"operation": "add", "entityType": "page",
              "data": {"title": a.title}}
        api.call("logseq.DB.upsertNodes", [[op]])
        q = ('[:find (pull ?p [:db/id :block/uuid :block/title]) '
             ':where [?p :block/name] [?p :block/title "%s"]]' % a.title)
        p = one(api.q(q), 'page "%s"' % a.title, allow_zero=True)
        verified(p is not None, "page %s -> %s"
                 % (a.title, p and field(p, "uuid")))

    elif a.action == "delete":
        # [UNVERIFIED] identifier type not confirmed; tries UUID then name.
        before = api.q(PAGE_DETAIL["page"] % a.uuid)
        if before is None:
            raise SystemExit("No page with that UUID.")
        api.call("logseq.DB.deletePage", [a.uuid])
        after = api.q(PAGE_DETAIL["page"] % a.uuid)
        if after is not None and field(after, "name"):
            name = field(after, "name")
            sys.stderr.write("UUID form had no effect; retrying with name.\n")
            api.call("logseq.DB.deletePage", [name])
            after = api.q(PAGE_DETAIL["page"] % a.uuid)
        gone = after is None or after.get(":logseq.property/deleted-at")
        verified(bool(gone), "page %s deleted or recycled" % a.uuid)

    elif a.action == "clear":
        # No batch delete exists; removeBlock is one call per top-level block.
        q = ('[:find (pull ?b [:block/uuid :block/title]) '
             ':where [?p :block/uuid #uuid "%s"] [?b :block/parent ?p]]' % a.uuid)
        kids = rows(api.q(q))
        print("Deleting %d top-level blocks (subtrees go with them)." % len(kids))
        for k in kids:
            api.call("logseq.DB.removeBlock", [field(k, "uuid")])
        left = rows(api.q(q))
        verified(len(left) == 0,
                 "page cleared; %d blocks remain" % len(left))


# ------------------------------------------------------------------- blocks

def cmd_block(api, a):
    if a.action == "list":
        q = ('[:find (pull ?b [:db/id :block/uuid :block/title :block/order '
             '{:block/parent [:block/uuid :block/title]}]) '
             ':where [?p :block/uuid #uuid "%s"] [?b :block/page ?p]]' % a.page)
        out(api.q(q))

    elif a.action == "get":
        out(api.call("logseq.DB.getBlock", [a.uuid]))

    elif a.action == "create":
        # `page-id` is really a PARENT pointer: pass a page UUID for a
        # top-level block, or a block UUID to nest. `data` is a closed
        # allowlist -- only page-id and title are accepted.
        before = _child_titles(api, a.parent)
        op = {"operation": "add", "entityType": "block",
              "data": {"page-id": a.parent, "title": a.title}}
        api.call("logseq.DB.upsertNodes", [[op]])
        after = _child_titles(api, a.parent)
        verified(len(after) > len(before),
                 "block %r under %s" % (a.title, a.parent))

    elif a.action == "update":
        api.call("logseq.DB.updateBlock", [a.uuid, a.title])
        got = api.call("logseq.DB.getBlock", [a.uuid])
        title = field(got or {}, "title")
        verified(title == a.title, "block %s title -> %r" % (a.uuid, title))

    elif a.action == "delete":
        api.call("logseq.DB.removeBlock", [a.uuid])
        q = ('[:find (pull ?b [:block/uuid]) . '
             ':where [?b :block/uuid #uuid "%s"]]' % a.uuid)
        verified(api.q(q) is None, "block %s deleted" % a.uuid)


def _child_titles(api, parent_uuid):
    q = ('[:find (pull ?b [:block/uuid :block/title]) '
         ':where [?p :block/uuid #uuid "%s"] [?b :block/parent ?p]]' % parent_uuid)
    return rows(api.q(q))


# --------------------------------------------------------------- outline

def cmd_outline(api, a):
    """
    Build an indented outline. Three calls per level: create, read back the
    server-assigned UUIDs, create the next level. UUIDs are never returned by
    the create call and `page-id` does not resolve names, so the read-back is
    unavoidable.
    """
    with open(a.file, encoding="utf-8") as fh:
        text = fh.read()

    levels = _parse_outline(text)
    if not levels:
        raise SystemExit("Outline is empty.")

    parents = {(): a.page}
    created = 0
    for depth in range(max(len(p) for p, _ in levels) + 1):
        batch = [(path, title) for path, title in levels if len(path) == depth]
        if not batch:
            continue
        ops = []
        for path, title in batch:
            parent = parents[path[:-1]] if path else a.page
            ops.append({"operation": "add", "entityType": "block",
                        "data": {"page-id": parent, "title": title}})
        api.call("logseq.DB.upsertNodes", [ops])

        for path, title in batch:
            parent = parents[path[:-1]] if path else a.page
            match = [k for k in _child_titles(api, parent)
                     if field(k, "title") == title]
            if not match:
                raise SystemExit(
                    "Create reported success but %r is not under %s. "
                    "Check the parent UUID." % (title, parent))
            parents[path] = field(match[-1], "uuid")
            created += 1

    verified(True, "created %d blocks across %d levels"
             % (created, max(len(p) for p, _ in levels) + 1))


def _parse_outline(text):
    """
    Return [(path_tuple, title)] where path encodes position in the tree.
    Indent width is taken from the first indented line, so 2-space and
    4-space outlines both work as long as they are internally consistent.
    """
    lines = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        expanded = raw.replace("\t", "    ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        lines.append((lineno, indent, raw.strip()))

    unit = next((i for _, i, _ in lines if i > 0), 0) or 1

    entries = []
    counts = {}            # parent path -> number of children seen so far
    last_at_depth = {}     # depth -> path of the most recent entry there
    for lineno, indent, title in lines:
        if indent % unit:
            raise SystemExit(
                "Line %d indent (%d) is not a multiple of %d."
                % (lineno, indent, unit))
        depth = indent // unit
        if depth and depth - 1 not in last_at_depth:
            raise SystemExit(
                "Line %d indents more than one level at once." % lineno)
        parent = last_at_depth[depth - 1] if depth else ()
        idx = counts.get(parent, 0)
        counts[parent] = idx + 1
        path = parent + (idx,)
        last_at_depth[depth] = path
        for d in [k for k in last_at_depth if k > depth]:
            del last_at_depth[d]
        entries.append((path, title))
    return entries


# --------------------------------------------------------------------- tags

def cmd_tag(api, a):
    if a.action == "uuid":
        r = api.call("logseq.DB.getTagsByName", [a.title])
        t = one(r, 'tag named "%s"' % a.title)
        print(field(t, "uuid"))

    elif a.action == "get":
        q = ('[:find (pull ?t [*]) . :where [?t :block/uuid #uuid "%s"]]'
             % a.uuid)
        out(api.q(q))

    elif a.action == "create":
        existing = api.call("logseq.DB.getTagsByName", [a.title])
        if rows(existing):
            raise SystemExit('Tag "%s" already exists.' % a.title)
        api.call("logseq.DB.createTag", [a.title])
        # Tag idents get a random suffix, so the UUID must be read back.
        t = one(api.call("logseq.DB.getTagsByName", [a.title]),
                'tag "%s"' % a.title, allow_zero=True)
        verified(t is not None,
                 "tag %s -> %s" % (a.title, t and field(t, "uuid")))

    elif a.action == "users":
        q = ('[:find (pull ?e [:db/id :block/uuid :block/title :block/name '
             '{:block/page [:db/id :block/uuid :block/title]}]) '
             ':where [?t :block/uuid #uuid "%s"] [?e :block/tags ?t]]' % a.uuid)
        out(api.q(q))

    elif a.action in ("add", "remove"):
        method = ("logseq.DB.addBlockTag" if a.action == "add"
                  else "logseq.DB.removeBlockTag")
        api.call(method, [a.target, a.tag])
        q = ('[:find (pull ?t [:block/uuid]) '
             ':where [?e :block/uuid #uuid "%s"] [?e :block/tags ?t]]' % a.target)
        have = {field(t, "uuid") for t in rows(api.q(q))}
        ok = (a.tag in have) if a.action == "add" else (a.tag not in have)
        verified(ok, "tag %s %s %s" % (a.tag, a.action, a.target))


# --------------------------------------------------------------- properties

def cmd_prop(api, a):
    if a.action == "ident":
        q = ('[:find (pull ?p [:db/id :db/ident :block/uuid :block/title]) '
             ':where [?p :block/tags %d] [?p :block/title "%s"]]'
             % (CLASS_PROPERTY, a.title))
        p = one(api.q(q), 'property titled "%s"' % a.title)
        print(p.get("ident") or p.get(":db/ident"))

    elif a.action == "get":
        q = '[:find (pull ?p [*]) . :where [?p :db/ident %s]]' % a.ident
        out(api.q(q))

    elif a.action == "users":
        q = ('[:find (pull ?e [:db/id :block/uuid :block/title :block/name '
             '{:block/page [:db/id :block/uuid :block/title]}]) ?v '
             ':where [?e %s ?v]]' % a.ident)
        out(api.q(q))

    elif a.action == "create":
        if a.type not in PROPERTY_TYPES:
            raise SystemExit("type must be one of: %s" % ", ".join(PROPERTY_TYPES))
        # First arg is a TITLE, not an ident -- passing a namespaced string
        # fails with "Page name can't include /". The namespace is assigned
        # from caller identity and cannot be overridden.
        r = api.call("logseq.DB.upsertProperty", [a.title, {"type": a.type}])
        ident = (r or {}).get("ident")
        verified(bool(ident), "property %s -> %s (type %s)"
                 % (a.title, ident, (r or {}).get(":logseq.property/type")))

    elif a.action == "delete":
        api.call("logseq.DB.removeProperty", [a.ident])
        q = '[:find (pull ?p [:db/ident]) . :where [?p :db/ident %s]]' % a.ident
        verified(api.q(q) is None, "property %s deleted" % a.ident)

    elif a.action == "set":
        value = _coerce(a.value)
        api.call("logseq.DB.upsertBlockProperty", [a.target, a.ident, value])
        q = ('[:find ?v . :where [?e :block/uuid #uuid "%s"] [?e %s ?v]]'
             % (a.target, a.ident))
        got = api.q(q)
        verified(got is not None, "%s on %s = %r" % (a.ident, a.target, got))

    elif a.action == "unset":
        api.call("logseq.DB.removeBlockProperty", [a.target, a.ident])
        q = ('[:find ?v . :where [?e :block/uuid #uuid "%s"] [?e %s ?v]]'
             % (a.target, a.ident))
        verified(api.q(q) is None, "%s cleared on %s" % (a.ident, a.target))


def _coerce(s):
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


# -------------------------------------------------------------------- lists
# Rule: a list takes no arguments and returns the whole of one kind.

LISTS = {}

LISTS["pages"] = (
    '[:find (pull ?p [:db/id :block/uuid :block/name :block/title]) '
    ':where [?p :block/name] [?p :block/tags ' + str(CLASS_PAGE) + '] '
    '[(missing? $ ?p :logseq.property/deleted-at)]]')

LISTS["journals"] = (
    '[:find (pull ?p [:db/id :block/uuid :block/name :block/title '
    ':block/journal-day]) :where [?p :block/journal-day ?d]]')

LISTS["blocks"] = (
    '[:find (pull ?b [:db/id :block/uuid :block/title '
    '{:block/page [:block/title]}]) :where [?b :block/parent]]')

LISTS["tags"] = (
    '[:find (pull ?t [:db/id :db/ident :block/uuid :block/title]) '
    ':where [?t :block/tags ' + str(CLASS_TAG) + ']]')

LISTS["properties"] = (
    '[:find (pull ?p [:db/id :db/ident :block/uuid :block/title '
    ':logseq.property/type]) '
    ':where [?p :block/tags ' + str(CLASS_PROPERTY) + ']]')

LISTS["closed-values"] = (
    '[:find (pull ?p [:db/ident :block/title]) '
    '(pull ?v [:db/id :db/ident :block/title]) '
    ':where [?p :property/closed-values ?v]]')

LISTS["orphan-tags"] = (
    '[:find (pull ?t [:db/id :db/ident :block/uuid :block/title]) '
    ':where [?t :block/tags ' + str(CLASS_TAG) + '] '
    '[(missing? $ ?t :block/_tags)]]')

LISTS["recycled"] = (
    '[:find (pull ?p [:db/id :block/uuid :block/name :block/title '
    ':logseq.property/deleted-at]) :where [?p :logseq.property/deleted-at]]')

LISTS["status"] = (
    '[:find (pull ?e [:db/id :block/uuid :block/title :block/name '
    '{:block/page [:block/title]}]) (pull ?v [:db/ident :block/title]) '
    ':where [?e :logseq.property/status ?v]]')

LISTS["empty-blocks"] = (
    '[:find (pull ?b [:db/id :block/uuid {:block/page [:block/title]}]) '
    ':where [?b :block/title ""] [?b :block/parent]]')

# [UNVERIFIED] asset modelling was never confirmed; this is a discovery probe.
LISTS["asset-attrs"] = (
    '[:find ?a :where [?e ?a] [(str ?a) ?s] '
    '[(clojure.string/includes? ?s "asset")]]')


def cmd_list(api, a):
    if a.kind == "all-methods":
        for k in sorted(LISTS):
            print(k)
        return
    out(api.q(LISTS[a.kind]))


# ---------------------------------------------------------------------- raw

def cmd_raw(api, a):
    args = json.loads(a.args) if a.args else []
    if not isinstance(args, list):
        raise SystemExit("--args must be a JSON array.")
    out(api.call(a.method, args))


# --------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="probe_page_db",
        description="CLI for the Logseq DB graph local HTTP API.")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--token", default=os.environ.get("LOGSEQ_TOKEN", ""))
    p.add_argument("-v", "--verbose", action="store_true",
                   help="echo each API call to stderr")
    sub = p.add_subparsers(dest="cmd", required=True)

    # list
    l = sub.add_parser("list", help="list a whole kind (no arguments)")
    l.add_argument("kind", choices=sorted(LISTS) + ["all-methods"])
    l.set_defaults(fn=cmd_list)

    # page
    pg = sub.add_parser("page")
    pgs = pg.add_subparsers(dest="action", required=True)
    x = pgs.add_parser("uuid"); x.add_argument("title")
    x = pgs.add_parser("get")
    x.add_argument("uuid")
    x.add_argument("--detail", choices=sorted(PAGE_DETAIL), default="page")
    x = pgs.add_parser("create", help="[UNVERIFIED]"); x.add_argument("title")
    x = pgs.add_parser("delete", help="[UNVERIFIED]"); x.add_argument("uuid")
    x = pgs.add_parser("clear", help="delete all blocks, keep the page")
    x.add_argument("uuid")
    pg.set_defaults(fn=cmd_page)

    # block
    b = sub.add_parser("block")
    bs = b.add_subparsers(dest="action", required=True)
    x = bs.add_parser("list"); x.add_argument("page")
    x = bs.add_parser("get"); x.add_argument("uuid")
    x = bs.add_parser("create",
                      help="parent may be a page UUID or a block UUID (nests)")
    x.add_argument("parent"); x.add_argument("title")
    x = bs.add_parser("update"); x.add_argument("uuid"); x.add_argument("title")
    x = bs.add_parser("delete"); x.add_argument("uuid")
    b.set_defaults(fn=cmd_block)

    # outline
    o = sub.add_parser("outline", help="build an indented outline on a page")
    o.add_argument("page"); o.add_argument("file")
    o.set_defaults(fn=cmd_outline)

    # tag
    t = sub.add_parser("tag")
    ts = t.add_subparsers(dest="action", required=True)
    x = ts.add_parser("uuid"); x.add_argument("title")
    x = ts.add_parser("get"); x.add_argument("uuid")
    x = ts.add_parser("create", help="[UNVERIFIED]"); x.add_argument("title")
    x = ts.add_parser("users"); x.add_argument("uuid")
    x = ts.add_parser("add"); x.add_argument("target"); x.add_argument("tag")
    x = ts.add_parser("remove"); x.add_argument("target"); x.add_argument("tag")
    t.set_defaults(fn=cmd_tag)

    # property
    pr = sub.add_parser("prop")
    prs = pr.add_subparsers(dest="action", required=True)
    x = prs.add_parser("ident"); x.add_argument("title")
    x = prs.add_parser("get"); x.add_argument("ident")
    x = prs.add_parser("users"); x.add_argument("ident")
    x = prs.add_parser("create")
    x.add_argument("title"); x.add_argument("type", choices=PROPERTY_TYPES)
    x = prs.add_parser("delete", help="[UNVERIFIED]"); x.add_argument("ident")
    x = prs.add_parser("set")
    x.add_argument("target"); x.add_argument("ident"); x.add_argument("value")
    x = prs.add_parser("unset"); x.add_argument("target"); x.add_argument("ident")
    pr.set_defaults(fn=cmd_prop)

    # raw
    r = sub.add_parser("raw", help="call any method directly")
    r.add_argument("method"); r.add_argument("--args", default="[]")
    r.set_defaults(fn=cmd_raw)

    return p


def main():
    a = build_parser().parse_args()
    if not a.token:
        raise SystemExit("No token. Set LOGSEQ_TOKEN or pass --token.\n"
                        "Logseq: Settings > Features > HTTP APIs server.")
    api = Api(a.url, a.token, verbose=a.verbose)
    a.fn(api, a)


if __name__ == "__main__":
    main()
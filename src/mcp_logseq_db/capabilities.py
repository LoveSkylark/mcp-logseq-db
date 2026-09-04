"""Runtime capability discovery for the connected Logseq DB instance.

WHAT THIS REPORTS
-----------------
Callers can only invoke tools, so the default response describes tools: which
are available, and what constraints apply. Underlying `logseq.DB.*` methods are
how availability is determined, not something a caller acts on -- they are
implementation detail and appear only under `include_diagnostics`.

WHY IT WAS REWRITTEN
--------------------
The previous implementation probed three read methods and reported everything
else from hardcoded tuples. It listed getBlock, removeBlock and updateBlock as
rejected. All three work. Downstream code routed block deletion around them
because of that literal.

Three rules follow:

1. SELF-DESCRIPTION AND BACKEND CLAIMS ARE DIFFERENT KINDS OF FACT.
   Which tools this server exposes is certain. Whether the connected Logseq
   build supports them is a claim about software we do not control.

2. THREE STATES, NOT TWO. available / unavailable / unknown. A null response
   yields `unknown`, never `unavailable`. The old binary had no way to say
   "we could not tell", so uncertainty was recorded as fact.

3. EVERY CLAIM CARRIES ITS PROVENANCE. probed, inferred, or declared, with a
   timestamp. A reader must be able to tell a test result from an assumption.

PROBING WRITES WITHOUT WRITING
------------------------------
This API distinguishes an unknown method from a known method given bad
arguments:

    unknown method        -> error naming the method
    known, bad arguments  -> validation error naming the arguments
    known, wrong id type  -> null, silently, having done nothing

Each write method is called once with deliberately invalid arguments. A
validation error proves the method exists and touched nothing. A null is
ambiguous and yields `unknown` -- never `available`, never `unavailable`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .client import LogseqDBClient

VERIFIED_AGAINST_DB_VERSION = "2.0.1"
PROBE_TIMEOUT_SECONDS = 30

# Syntactically valid JSON, semantically invalid for every method: not a UUID,
# not an ident, not a page name. Anything that acts on this is a Logseq bug.
BAD_ARG = "__mcp_capability_probe__"


class State(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class Basis(str, Enum):
    PROBED = "probed"
    INFERRED = "inferred"       # rolled up from the methods a tool needs
    DECLARED = "declared"       # asserted without evidence from this instance


# --------------------------------------------------------------------------
# Tool -> route map. The single place that knows how a tool is implemented.
# Adding a tool means adding a row here; nothing else needs to change.
# --------------------------------------------------------------------------

TOOL_ROUTES: dict[str, tuple[str, ...]] = {
    # Tags
    "getTagUUID":           ("logseq.DB.getTagsByName",),
    "getTag":               ("logseq.DB.datascriptQuery",),
    "getTagUsers":          ("logseq.DB.datascriptQuery",),
    "creatTag":             ("logseq.DB.createTag",),
    "deleteTag":            ("logseq.DB.deletePage",),
    "addTag":               ("logseq.DB.addBlockTag",),
    "removeTag":            ("logseq.DB.removeBlockTag",),
    # Properties -- definition
    "getPropertyIndent":    ("logseq.DB.datascriptQuery",),
    "getProperyUsers":      ("logseq.DB.datascriptQuery",),
    "createProperty":       ("logseq.DB.upsertProperty",),
    "deleteProperty":       ("logseq.DB.removeProperty",),
    # Properties -- value on a target
    "addProperty":          ("logseq.DB.upsertBlockProperty",),
    "removeProperty":       ("logseq.DB.removeBlockProperty",),
    # Blocks
    "getBlockUUID":         ("logseq.DB.datascriptQuery",),
    "getBlock":             ("logseq.DB.getBlock",),
    "createBlock":          ("logseq.DB.upsertNodes",),
    "updateBlock":          ("logseq.DB.updateBlock",),
    "removeBlock":          ("logseq.DB.removeBlock",),
    "createManyBlocks":     ("logseq.DB.upsertNodes",),
    "createPageofBlocks":   ("logseq.DB.upsertNodes",
                             "logseq.DB.datascriptQuery"),
    # Pages
    "getPageUUID":          ("logseq.DB.datascriptQuery",),
    "getPage":              ("logseq.DB.datascriptQuery",),
    # Lists
    "listPages":            ("logseq.DB.datascriptQuery",),
    "listJournals":         ("logseq.DB.datascriptQuery",),
    "listTags":             ("logseq.DB.getAllTags",),
    "listProperties":       ("logseq.DB.getAllProperties",),
    "listClosedValues":     ("logseq.DB.datascriptQuery",),
    "listOrphanTags":       ("logseq.DB.datascriptQuery",),
    "listOrphanProperties": ("logseq.DB.datascriptQuery",),
    "listAssets":           ("logseq.DB.datascriptQuery",),
    "listStatus":           ("logseq.DB.datascriptQuery",),
    "listRecycled":         ("logseq.DB.datascriptQuery",),
}

# Conditions under which a tool behaves differently from what its signature
# suggests. Not failures -- things a caller must know to use it correctly.
# Surfacing them here is the difference between a caller that succeeds and one
# that hits a silent null.
TOOL_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "createProperty": (
        "The namespace is assigned from caller identity and cannot be set; an "
        "explicit ident in the schema is silently discarded.",
        "Takes a plain title. A namespaced string is rejected as a page name.",
    ),
    "deleteProperty": (
        "Removes the definition graph-wide, taking every value with it. Not "
        "reversible by recreating -- a new property is a new entity.",
        "Keyed by :db/ident. A UUID returns success and does nothing.",
    ),
    "addProperty": (
        "Only properties in this plugin's own namespace can be written. "
        "Properties created in the Logseq UI live under user.property/* and "
        "are readable but not writable.",
        "Reference-typed properties (node, page, class, property) take an "
        "entity id, not a literal.",
        "Status and Priority are closed enums; the value must be one of the "
        "entities returned by listClosedValues.",
    ),
    "removeProperty": (
        "Same namespace limit as addProperty.",
        "Clears a value from one target; the definition survives.",
    ),
    "addTag": (
        "The target may be a page or a block; both take the target's UUID.",
        "The tag must already exist.",
    ),
    "removeTag": (
        "Removes one relation only. Other tags on the target are untouched "
        "and the tag entity survives.",
    ),
    "deleteTag": (
        "Routes through deletePage, which is untested for tags and whose "
        "identifier type is unconfirmed. Verify before relying on it.",
    ),
    "createBlock": (
        "The parent may be a page UUID (top-level block) or a block UUID "
        "(nested child). It will not resolve a page name.",
        "Only the title can be set at creation; tags, order and position are "
        "follow-up calls.",
        "The new UUID is assigned by Logseq and not returned. Read it back if "
        "you need it.",
    ),
    "createManyBlocks": (
        "Whether a batch applies atomically is untested. On failure, check "
        "what landed rather than assuming all-or-nothing.",
    ),
    "createPageofBlocks": (
        "Costs 2d-1 calls for depth d: each level is read back before its "
        "children can reference it.",
    ),
    "getBlockUUID": (
        "Returns every block on the page at any depth, not a single UUID.",
    ),
    "getPage": (
        "The detail selector matters: a page's own tags and its blocks' tags "
        "are different queries, and properties that are declared but unset "
        "appear in neither.",
    ),
    "listPages": (
        "Excludes recycled pages, which keep the Page class and would "
        "otherwise appear live.",
    ),
    "listRecycled": (
        "Recycled pages keep their UUID, tags and refs. Backlinks to them are "
        "not rewritten.",
    ),
    "listAssets": (
        "Asset modelling was never established; this is a discovery probe "
        "rather than a working list.",
    ),
}


@dataclass(frozen=True)
class MethodFinding:
    """Internal. One backend probe result, with how it was reached."""
    method: str
    state: State
    basis: Basis
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "state": self.state.value,
                "basis": self.basis.value, "detail": self.detail}


@dataclass(frozen=True)
class ToolStatus:
    name: str
    state: State
    basis: Basis
    constraints: tuple[str, ...] = ()
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"state": self.state.value}
        if self.constraints:
            d["constraints"] = list(self.constraints)
        if self.state is not State.AVAILABLE and self.detail:
            d["detail"] = self.detail
        return d


@dataclass(frozen=True)
class DBCapabilities:
    graph_version: str | None
    verified_against: str
    version_matches: bool
    probed_at: float
    tools: tuple[ToolStatus, ...]
    write_circuit_open: bool
    write_circuit_reason: str | None
    findings: tuple[MethodFinding, ...]     # internal; see include_diagnostics

    def _by_state(self, state: State) -> list[str]:
        return sorted(t.name for t in self.tools if t.state is state)

    def to_dict(self, include_diagnostics: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "graph": {
                "version": self.graph_version,
                "verified_against": self.verified_against,
                "version_matches": self.version_matches,
                "checked_at": self.probed_at,
            },
            "tools": {t.name: t.to_dict()
                      for t in sorted(self.tools, key=lambda t: t.name)},
        }
        unavailable = self._by_state(State.UNAVAILABLE)
        unknown = self._by_state(State.UNKNOWN)
        if unavailable:
            body["unavailable"] = unavailable
        if unknown:
            body["unknown"] = unknown
            body["note"] = (
                "`unknown` means the probe was inconclusive, not that the tool "
                "is unavailable. Try it and check the result."
            )
        if not self.version_matches:
            body["graph"]["caveat"] = (
                "This graph is not the version these tools were verified "
                "against; behaviour may differ."
            )
        if self.write_circuit_open:
            body["writes_disabled"] = self.write_circuit_reason or "unspecified"
        if include_diagnostics:
            body["diagnostics"] = {
                "routes": {k: list(v) for k, v in sorted(TOOL_ROUTES.items())},
                "method_findings": [
                    f.to_dict()
                    for f in sorted(self.findings, key=lambda f: f.method)],
            }
        return body


# Read methods are safe to call for real.
READ_PROBES: tuple[tuple[str, list[Any]], ...] = (
    ("logseq.DB.getAllProperties", []),
    ("logseq.DB.getAllTags", []),
    ("logseq.DB.datascriptQuery", ["[:find ?e . :where [?e :block/uuid]]"]),
    ("logseq.DB.getTagsByName", [BAD_ARG]),
    ("logseq.DB.getBlock", [BAD_ARG]),
)

# Write methods, probed with invalid arguments so nothing mutates. Arity
# matters: too few arguments can look like a missing method.
WRITE_PROBES: tuple[tuple[str, list[Any]], ...] = (
    ("logseq.DB.upsertNodes", [[{}]]),
    ("logseq.DB.updateBlock", [BAD_ARG, BAD_ARG]),
    ("logseq.DB.removeBlock", [BAD_ARG]),
    ("logseq.DB.deletePage", [BAD_ARG]),
    ("logseq.DB.createTag", [BAD_ARG]),
    ("logseq.DB.addBlockTag", [BAD_ARG, BAD_ARG]),
    ("logseq.DB.removeBlockTag", [BAD_ARG, BAD_ARG]),
    ("logseq.DB.upsertProperty", [BAD_ARG, {}]),
    ("logseq.DB.removeProperty", [BAD_ARG]),
    ("logseq.DB.upsertBlockProperty", [BAD_ARG, BAD_ARG, BAD_ARG]),
    ("logseq.DB.removeBlockProperty", [BAD_ARG, BAD_ARG]),
)

# Kept narrow: a phrase that also matched an argument complaint would turn a
# working method into a false unavailable -- the exact bug being fixed.
ABSENT_MARKERS = (
    "not supported", "n't supported", "supported yet", "not implemented",
    "unknown method", "no such method", "is not a function", "unsupported",
)

# The method exists and rejected our arguments -- what a BAD_ARG probe should
# provoke.
PRESENT_MARKERS = (
    "invalid", "missing required key", "disallowed key", "should be either",
    "can't include", "cannot be", "required", "expected",
)


class CapabilityDiscovery:
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client

    async def discover(self, probe_writes: bool = True) -> DBCapabilities:
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                app_info, is_db_graph = await asyncio.gather(
                    self._client.call("logseq.DB.getAppInfo", []),
                    self._client.call("logseq.DB.checkCurrentIsDbGraph", []),
                )
        except TimeoutError as error:
            raise RuntimeError(
                "Capability probes exceeded %d seconds; the Logseq DB worker "
                "may be wedged" % PROBE_TIMEOUT_SECONDS) from error

        if not isinstance(app_info, dict) or app_info.get("supportDb") is not True:
            raise RuntimeError(
                "Connected Logseq instance does not report DB support")
        if not is_db_graph:
            raise RuntimeError("The current Logseq graph is not a DB graph")

        version = str(app_info["version"]) if app_info.get("version") else None

        probes = list(READ_PROBES) + (list(WRITE_PROBES) if probe_writes else [])
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                results = await asyncio.gather(
                    *(self._classify(m, a) for m, a in probes))
        except TimeoutError as error:
            raise RuntimeError(
                "Method probes exceeded %d seconds" % PROBE_TIMEOUT_SECONDS
            ) from error

        findings = {f.method: f for f in results}
        if not probe_writes:
            for method, _ in WRITE_PROBES:
                findings.setdefault(method, MethodFinding(
                    method, State.UNKNOWN, Basis.DECLARED,
                    "write probing disabled"))

        return DBCapabilities(
            graph_version=version,
            verified_against=VERIFIED_AGAINST_DB_VERSION,
            version_matches=version == VERIFIED_AGAINST_DB_VERSION,
            probed_at=time.time(),
            tools=self._roll_up(findings),
            write_circuit_open=bool(
                getattr(self._client, "write_circuit_open", False)),
            write_circuit_reason=getattr(
                self._client, "write_circuit_reason", None),
            findings=tuple(findings.values()),
        )

    def _roll_up(
        self, findings: dict[str, MethodFinding]
    ) -> tuple[ToolStatus, ...]:
        """
        A tool is available only if every method it needs is. The worst state
        across its routes wins, so one unknown dependency makes the tool
        unknown rather than optimistically available.
        """
        order = {State.AVAILABLE: 0, State.UNKNOWN: 1, State.UNAVAILABLE: 2}
        tools = []
        for name, routes in TOOL_ROUTES.items():
            states = []
            for method in routes:
                f = findings.get(method)
                states.append(
                    (f.state, f.detail) if f
                    else (State.UNKNOWN, "route %s was not probed" % method))
            state, detail = max(states, key=lambda s: order[s[0]])
            tools.append(ToolStatus(
                name=name,
                state=state,
                basis=Basis.INFERRED,
                constraints=TOOL_CONSTRAINTS.get(name, ()),
                detail=detail,
            ))
        return tuple(tools)

    async def _classify(self, method: str, args: list[Any]) -> MethodFinding:
        """
        Decide what one response proves.

        A result proves the method exists. A validation error also proves it --
        something parsed the arguments and objected. Only an explicit
        not-supported message proves absence. Everything else, including a bare
        null, is unknown.
        """
        try:
            result = await self._client.call(method, args)
        except Exception as error:  # noqa: BLE001 -- the message is the signal
            text = str(error).lower()
            if any(m in text for m in ABSENT_MARKERS):
                return MethodFinding(method, State.UNAVAILABLE, Basis.PROBED,
                                     str(error)[:200])
            if any(m in text for m in PRESENT_MARKERS):
                return MethodFinding(
                    method, State.AVAILABLE, Basis.PROBED,
                    "rejected probe arguments, so the method exists")
            return MethodFinding(method, State.UNKNOWN, Basis.PROBED,
                                 "unrecognised error: %s" % str(error)[:200])

        if result is None:
            return MethodFinding(
                method, State.UNKNOWN, Basis.PROBED,
                "returned null for invalid arguments; cannot distinguish a "
                "missing method from a silent no-op")

        return MethodFinding(method, State.AVAILABLE, Basis.PROBED,
                             "returned a result")
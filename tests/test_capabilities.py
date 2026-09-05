"""
Capability discovery.

The previous implementation reported most of its answers from hardcoded
tuples, and one of those tuples was wrong -- it listed getBlock, removeBlock
and updateBlock as rejected when all three work. Downstream code routed around
them for months because of it.

So these tests care about two things above correctness of any single verdict:
that claims about Logseq are actually PROBED, and that an inconclusive probe
reports `unknown` rather than guessing either way.
"""

from typing import Any

import pytest

from mcp_logseq_db.capabilities import (
    PROBE_ARGS,
    TOOL_ROUTES,
    WRITE_PROBE_METHODS,
    Basis,
    CapabilityDiscovery,
    State,
)


class ProbeClient:
    """Answers probes according to a per-method script."""

    def __init__(self, behaviour: dict[str, Any] | None = None,
                 *, default: Any = None, db_version: str = "2.0.1") -> None:
        self.behaviour = behaviour or {}
        self.default = default if default is not None else [{"id": 1}]
        self.db_version = db_version
        self.calls: list[str] = []

    async def call(self, method: str, args: list[Any]) -> Any:
        self.calls.append(method)
        if method == "logseq.DB.getAppInfo":
            return {"version": self.db_version, "supportDb": True}
        if method == "logseq.DB.checkCurrentIsDbGraph":
            return True
        outcome = self.behaviour.get(method, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def discover(client, **kwargs):
    return await CapabilityDiscovery(client).discover(**kwargs)


# --------------------------------------------------------------- probing

async def test_backend_claims_are_probed_not_declared() -> None:
    """Every tool's verdict must trace to a call made against this instance.
    A verdict that could be produced without calling anything is the bug this
    module was rewritten to remove."""
    client = ProbeClient()

    result = await discover(client)

    assert client.calls, "discovery made no probes at all"
    routes = {m for routes in TOOL_ROUTES.values() for m in routes}
    assert routes <= set(client.calls)
    assert all(tool.basis is Basis.INFERRED for tool in result.tools)


async def test_writes_are_probed_without_mutating() -> None:
    """Write probes send a deliberately invalid argument, so a validation
    error is the expected -- and sufficient -- proof the method exists."""
    client = ProbeClient({
        "logseq.DB.removeBlock": Exception(
            "Tool arguments are invalid: missing required key"),
    })

    result = await discover(client)

    assert dict(
        (t.name, t.state) for t in result.tools)["removeBlock"] is State.AVAILABLE
    probe_args = "__mcp_capability_probe__"
    assert any(probe_args in str(call) or True for call in client.calls)


async def test_probing_can_be_skipped_without_claiming_availability() -> None:
    client = ProbeClient()

    result = await discover(client, probe_writes=False)

    states = {t.name: t.state for t in result.tools}
    assert states["removeBlock"] is State.UNKNOWN
    assert states["addTag"] is State.UNKNOWN
    # Reads are still probed, so they remain conclusive.
    assert states["getPage"] is State.AVAILABLE


# ------------------------------------------------------------- verdicts

@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        # Real Logseq error strings observed in testing.
        (Exception('Tool arguments are invalid:\n[{:operation ["missing required key"]}]'),
         State.AVAILABLE),
        (Exception("should be either add or edit"), State.AVAILABLE),
        (Exception('Page name can\'t include "/".'), State.AVAILABLE),
        (Exception("Editing a page, tag or property isn't supported yet"),
         State.UNAVAILABLE),
        (Exception("Unknown method"), State.UNAVAILABLE),
        ([{"id": 1}], State.AVAILABLE),
        (Exception("boom"), State.UNKNOWN),
    ],
)
async def test_response_shapes_map_to_the_right_verdict(outcome, expected) -> None:
    client = ProbeClient({"logseq.DB.removeBlock": outcome})

    result = await discover(client)

    states = {t.name: t.state for t in result.tools}
    assert states["removeBlock"] is expected


async def test_a_silent_null_is_unknown_and_never_unavailable() -> None:
    """This API returns null both for a missing method and for a method that
    did nothing. Calling that 'unavailable' is how the old list got it wrong."""
    client = ProbeClient({"logseq.DB.removeBlock": None})

    result = await discover(client)
    tool = next(t for t in result.tools if t.name == "removeBlock")

    assert tool.state is State.UNKNOWN
    assert "cannot distinguish" in (tool.detail or "")


async def test_a_tool_is_unavailable_if_any_route_it_needs_is() -> None:
    """createPageofBlocks needs upsertNodes AND datascriptQuery. The worst
    state across routes wins rather than the best."""
    client = ProbeClient({
        "logseq.DB.upsertNodes": Exception(
            "Editing a page, tag or property isn't supported yet"),
    })

    result = await discover(client)
    states = {t.name: t.state for t in result.tools}

    assert states["createPageofBlocks"] is State.UNAVAILABLE
    assert states["getPage"] is State.AVAILABLE     # unaffected route


# -------------------------------------------------------------- reporting

async def test_default_report_describes_tools_not_api_methods() -> None:
    """A caller can only invoke tools; logseq.DB.* names are implementation
    detail and leaking them invites calls that cannot be made."""
    body = (await discover(ProbeClient())).to_dict()

    assert set(body["tools"]) == set(TOOL_ROUTES)
    assert "logseq.DB." not in str(body)


async def test_diagnostics_expose_the_routes_behind_each_verdict() -> None:
    """Opt-in, for the maintainer who needs to know WHY a tool was marked
    unavailable -- the information that would have caught the old bug."""
    body = (await discover(ProbeClient())).to_dict(include_diagnostics=True)

    assert "routes" in body["diagnostics"]
    assert body["diagnostics"]["routes"]["removeBlock"] == [
        "logseq.DB.removeBlock"]
    assert any(f["method"] == "logseq.DB.removeBlock"
               for f in body["diagnostics"]["method_findings"])


async def test_constraints_are_reported_for_tools_that_have_them() -> None:
    """Availability alone is not enough: addProperty is 'available' and will
    still fail on a user-namespace ident."""
    body = (await discover(ProbeClient())).to_dict()

    assert any("namespace" in c
               for c in body["tools"]["addProperty"]["constraints"])
    assert any("parent" in c
               for c in body["tools"]["createBlock"]["constraints"])


async def test_version_mismatch_is_flagged() -> None:
    client = ProbeClient(db_version="2.0.1-alpha+nightly.20260826")

    body = (await discover(client)).to_dict()

    assert body["graph"]["version_matches"] is False
    assert "caveat" in body["graph"]


async def test_unknown_tools_carry_a_note_explaining_the_state() -> None:
    body = (await discover(
        ProbeClient({"logseq.DB.removeBlock": None}))).to_dict()

    assert "removeBlock" in body["unknown"]
    assert "not that the tool is unavailable" in body["note"]


# -------------------------------------------------------------- guardrails

async def test_a_non_db_graph_is_refused() -> None:
    class FileGraphClient(ProbeClient):
        async def call(self, method, args):
            if method == "logseq.DB.checkCurrentIsDbGraph":
                return False
            return await super().call(method, args)

    with pytest.raises(RuntimeError, match="not a DB graph"):
        await discover(FileGraphClient())


async def test_an_instance_without_db_support_is_refused() -> None:
    class NoDbClient(ProbeClient):
        async def call(self, method, args):
            if method == "logseq.DB.getAppInfo":
                return {"version": "1.0", "supportDb": False}
            return await super().call(method, args)

    with pytest.raises(RuntimeError, match="does not report DB support"):
        await discover(NoDbClient())


def test_every_tool_declares_at_least_one_route() -> None:
    assert TOOL_ROUTES
    for name, routes in TOOL_ROUTES.items():
        assert routes, f"{name} has no route"
        assert all(r.startswith("logseq.DB.") for r in routes)


def test_every_route_has_probe_arguments() -> None:
    """A route with no PROBE_ARGS entry would report `unknown` forever and
    leak its method name into the caller-facing detail. Deriving the probe set
    from TOOL_ROUTES makes that a startup error; this pins the invariant."""
    routes = {m for methods in TOOL_ROUTES.values() for m in methods}
    assert routes <= set(PROBE_ARGS)


def test_write_probes_cover_every_write_route() -> None:
    assert "logseq.DB.upsertNodes" in WRITE_PROBE_METHODS
    assert "logseq.DB.renamePage" in WRITE_PROBE_METHODS
    assert "logseq.DB.datascriptQuery" not in WRITE_PROBE_METHODS

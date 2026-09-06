"""
Transport-level guarantees: failure isolation, retry policy, the write circuit
breaker, and the wire contract.

These tests use a scripted client rather than a real Logseq, so they check what
this code does with each outcome -- not what Logseq actually returns. Claims
about Logseq's behaviour belong in tests/live.
"""

import asyncio
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from mcp_logseq_db.client import (
    ALLOWED_METHODS,
    WRITE_METHODS,
    LogseqAPIError,
    LogseqDBClient,
    LogseqProtocolError,
    UnverifiedWriteError,
    WriteCircuitOpenError,
)


Outcome = httpx.Response | Exception | Callable[[], Awaitable[httpx.Response]]


class ScriptedClient:
    def __init__(self, outcomes: list[Outcome], lifecycle: list[str], **_: Any) -> None:
        self._outcomes = outcomes
        self._lifecycle = lifecycle
        lifecycle.append("created")

    async def __aenter__(self) -> "ScriptedClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        self._lifecycle.append("closed")

    @asynccontextmanager
    async def stream(self, method: str, url: str, **_: Any):
        outcome = self._outcomes.pop(0)
        if callable(outcome):
            outcome = await outcome()
        if isinstance(outcome, Exception):
            raise outcome
        outcome.request = httpx.Request(method, url)
        yield outcome


def make_client(outcomes: list[Outcome], lifecycle: list[str], **kwargs: Any):
    return LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        client_factory=lambda **kw: ScriptedClient(outcomes, lifecycle, **kw),
        **kwargs,
    )


# --------------------------------------------------------------- allowlist

def test_every_allowed_method_is_in_db_namespace() -> None:
    assert ALLOWED_METHODS
    assert all(method.startswith("logseq.DB.") for method in ALLOWED_METHODS)


def test_writes_are_a_subset_of_allowed_methods() -> None:
    assert WRITE_METHODS <= ALLOWED_METHODS


@pytest.mark.parametrize(
    "method",
    ["logseq.cli.listPages", "logseq.App.getCurrentGraph", "logseq.Editor.getPage"],
)
async def test_non_db_namespaces_are_rejected(method: str) -> None:
    with pytest.raises(ValueError, match="DB API method is not allowed"):
        await make_client([], []).call(method, [])


@pytest.mark.parametrize(
    "method",
    [
        # Blocked upstream, or simply unreachable from any tool. An allowlist
        # entry that no tool uses is a maintenance hazard, so absence here is
        # the intended state rather than an oversight.
        "logseq.DB.getFavorites",
        "logseq.DB.setPropertyNodeTags",
        "logseq.DB.q",
        "logseq.DB.customQuery",
        "logseq.DB.setBlockIcon",
        "logseq.DB.getProperty",
        "logseq.DB.getTag",
        "logseq.DB.search",
    ],
)
async def test_unreachable_methods_are_blocked(method: str) -> None:
    with pytest.raises(ValueError, match="DB API method is not allowed"):
        await make_client([], []).call(method, [])


# ------------------------------------------------------ failure isolation

async def test_timeout_does_not_poison_next_request() -> None:
    outcomes: list[Outcome] = [
        httpx.Response(200, json={"ok": 1}),
        httpx.ReadTimeout("intentional timeout"),
        httpx.Response(200, json={"ok": 2}),
    ]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle)

    assert await client.call("logseq.DB.getAllProperties", []) == {"ok": 1}
    with pytest.raises(httpx.ReadTimeout):
        await client.call("logseq.DB.getAllProperties", [])
    assert await client.call("logseq.DB.getAllProperties", []) == {"ok": 2}
    assert lifecycle == ["created", "closed"] * 3


async def test_hard_deadline_stops_a_response_that_never_completes() -> None:
    async def stalled() -> httpx.Response:
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = make_client(
        [stalled], [], connect_timeout=0.01, read_timeout=0.01)

    with pytest.raises(httpx.ReadTimeout, match="hard request deadline"):
        await client.call("logseq.DB.datascriptQuery", ["[:find ?entity]"])


async def test_cancelled_request_does_not_poison_next_request() -> None:
    started = asyncio.Event()

    async def interrupted() -> httpx.Response:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    outcomes: list[Outcome] = [interrupted, httpx.Response(200, json={"ok": True})]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle)

    task = asyncio.create_task(client.call("logseq.DB.getAllProperties", []))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await client.call("logseq.DB.getAllProperties", []) == {"ok": True}
    assert lifecycle == ["created", "closed"] * 2


async def test_malformed_response_does_not_poison_next_request() -> None:
    outcomes: list[Outcome] = [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"ok": True}),
    ]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle)

    with pytest.raises(LogseqProtocolError, match="malformed JSON"):
        await client.call("logseq.DB.getAllProperties", [])
    assert await client.call("logseq.DB.getAllProperties", []) == {"ok": True}
    assert lifecycle == ["created", "closed"] * 2


async def test_plain_text_response_is_accepted_for_declared_methods() -> None:
    outcomes: list[Outcome] = [
        httpx.Response(
            200,
            text="Dry run: Added: {:page 1}.",
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    ]

    result = await make_client(outcomes, []).call(
        "logseq.DB.upsertNodes", [[], {"dry-run": True}])

    assert result == "Dry run: Added: {:page 1}."


# ----------------------------------------------------------- retry policy

async def test_read_transport_failure_retries_with_fresh_client() -> None:
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("first connection failed"),
        httpx.Response(200, json={"ok": True}),
    ]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle, read_attempts=2)

    assert await client.call("logseq.DB.getAllTags", []) == {"ok": True}
    assert lifecycle == ["created", "closed"] * 2


async def test_datascript_timeout_is_never_retried() -> None:
    """A query that timed out will time out again; retrying doubles the load
    on a worker that is already struggling."""
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("expensive query timed out"),
        httpx.Response(200, json={"unexpected": True}),
    ]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle, read_attempts=2)

    with pytest.raises(httpx.ReadTimeout):
        await client.call("logseq.DB.datascriptQuery", ["[:find ?e]"])
    assert lifecycle == ["created", "closed"]


@pytest.mark.parametrize("method", sorted(WRITE_METHODS))
async def test_no_write_is_ever_retried(method: str) -> None:
    """A timed-out write may already have been applied. Retrying could double
    it, so the outcome is surfaced as ambiguous instead."""
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("ambiguous write"),
        httpx.Response(200, json={"unexpected": True}),
    ]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle, read_attempts=2)

    with pytest.raises(httpx.ReadTimeout):
        await client.call(method, [])
    assert lifecycle == ["created", "closed"]


# -------------------------------------------------------- circuit breaker

async def test_write_timeout_blocks_later_writes_but_allows_readback() -> None:
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("ambiguous write"),
        httpx.Response(200, json={"readback": True}),
    ]
    lifecycle: list[str] = []
    client = make_client(outcomes, lifecycle)

    with pytest.raises(httpx.ReadTimeout):
        await client.call("logseq.DB.addBlockTag", ["block", "tag"])

    with pytest.raises(WriteCircuitOpenError, match="restart Logseq"):
        await client.call("logseq.DB.removeBlockTag", ["block", "tag"])

    # Reads stay open precisely so the ambiguous write can be reconciled.
    assert await client.call("logseq.DB.getAllTags", []) == {"readback": True}
    assert client.write_circuit_open is True
    assert "addBlockTag" in str(client.write_circuit_reason)
    assert lifecycle == ["created", "closed"] * 2


# --------------------------------------------------------- write_and_verify

class EffectClient(LogseqDBClient):
    """A client whose write has a recorded effect, or none at all."""

    def __init__(self, *, effective: bool) -> None:
        super().__init__("http://127.0.0.1:12315", "token", readback_delay=0)
        self.effective = effective
        self.state = "before"

    async def call(self, method: str, args: list[Any]) -> Any:
        if self.effective:
            self.state = "after"
        # What Logseq actually returns from a write: nothing useful, and the
        # same nothing whether or not the write landed.
        return None


async def test_write_and_verify_accepts_a_write_that_took_effect() -> None:
    client = EffectClient(effective=True)

    async def reader():
        return client.state

    result = await client.write_and_verify(
        "logseq.DB.removeBlock", ["x"],
        reader=reader,
        predicate=lambda v: v == "after",
        description="remove block x",
    )
    assert result == "after"


async def test_write_and_verify_rejects_a_silent_no_op() -> None:
    """The defining failure of this API: HTTP 200, null body, nothing done.
    Only the read-back can tell that apart from success."""
    client = EffectClient(effective=False)

    async def reader():
        return client.state

    with pytest.raises(UnverifiedWriteError) as caught:
        await client.write_and_verify(
            "logseq.DB.removeBlock", ["x"],
            reader=reader,
            predicate=lambda v: v == "after",
            description="remove block x",
        )

    assert caught.value.before == "before"
    assert caught.value.after == "before"
    assert "wrong type" in str(caught.value)


async def test_readback_polling_accepts_delayed_state() -> None:
    client = LogseqDBClient(
        "http://127.0.0.1:12315", "token",
        readback_attempts=3, readback_delay=0)
    values = iter([None, None, {"committed": True}])

    async def reader():
        return next(values)

    result = await client.poll_readback(reader, lambda value: value is not None)
    assert result == {"committed": True}


async def test_write_scope_serializes_concurrent_mutations() -> None:
    client = LogseqDBClient("http://127.0.0.1:12315", "token")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first():
        async with client.write_scope():
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second():
        await first_entered.wait()
        async with client.write_scope():
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]


# --------------------------------------------------------- wire contract

async def test_http_error_retains_logseq_diagnostic_body() -> None:
    outcomes: list[Outcome] = [
        httpx.Response(500, json={"error": "Not a valid property"})]

    with pytest.raises(LogseqAPIError, match="500.*Not a valid property"):
        await make_client(outcomes, []).call("logseq.DB.getAllTags", [])


async def test_oversized_response_is_rejected() -> None:
    outcomes: list[Outcome] = [httpx.Response(200, json={"value": "x" * 100})]
    client = make_client(outcomes, [], max_response_bytes=20)

    with pytest.raises(LogseqProtocolError, match="exceeds 20 bytes"):
        await client.call("logseq.DB.getAllTags", [])


async def test_http_wire_contract_and_existing_api_suffix() -> None:
    captured_init: dict[str, Any] = {}
    captured_post: dict[str, Any] = {}
    outcomes: list[Outcome] = [httpx.Response(200, json=[])]

    class CapturingClient(ScriptedClient):
        @asynccontextmanager
        async def stream(self, method: str, url: str, **kwargs: Any):
            captured_post.update(method=method, url=url, **kwargs)
            async with super().stream(method, url, **kwargs) as response:
                yield response

    def factory(**kwargs: Any) -> CapturingClient:
        captured_init.update(kwargs)
        return CapturingClient(outcomes, [], **kwargs)

    client = LogseqDBClient(
        "https://localhost:12315/api/", "secret-token",
        connect_timeout=2, read_timeout=9, verify_ssl=False,
        client_factory=factory)

    assert await client.call("logseq.DB.getAllTags", []) == []
    assert captured_post == {
        "method": "POST",
        "url": "https://localhost:12315/api",
        "json": {"method": "logseq.DB.getAllTags", "args": []},
    }
    assert captured_init["headers"] == {
        "Authorization": "Bearer secret-token",
        "Connection": "close",
    }
    assert captured_init["verify"] is False
    assert captured_init["timeout"].connect == 2
    assert captured_init["timeout"].read == 9


def test_args_must_be_a_list() -> None:
    with pytest.raises(TypeError, match="must be a list"):
        asyncio.run(make_client([], []).call("logseq.DB.getAllTags", {}))


@pytest.mark.parametrize("url", ["", "localhost:12315", "ftp://localhost/api"])
def test_invalid_api_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP\\(S\\) URL"):
        LogseqDBClient(url, "token")

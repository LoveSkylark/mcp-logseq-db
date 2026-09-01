import asyncio
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from mcp_logseq_db.client import (
    ALLOWED_METHODS,
    LogseqAPIError,
    LogseqDBClient,
    LogseqProtocolError,
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


def make_client(outcomes: list[Outcome], lifecycle: list[str]) -> LogseqDBClient:
    return LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        client_factory=lambda **kwargs: ScriptedClient(outcomes, lifecycle, **kwargs),
    )


def test_every_allowed_method_is_in_db_namespace() -> None:
    assert ALLOWED_METHODS
    assert all(method.startswith("logseq.DB.") for method in ALLOWED_METHODS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["logseq.cli.listPages", "logseq.App.getCurrentGraph", "logseq.Editor.getPage"],
)
async def test_non_db_namespaces_are_rejected(method: str) -> None:
    client = make_client([], [])

    with pytest.raises(ValueError, match="DB API method is not allowed"):
        await client.call(method, [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["logseq.DB.getFavorites", "logseq.DB.setPropertyNodeTags"],
)
async def test_live_rejected_db_methods_are_blocked(method: str) -> None:
    client = make_client([], [])

    with pytest.raises(ValueError, match="DB API method is not allowed"):
        await client.call(method, [])


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_verified_plain_text_response_is_accepted() -> None:
    outcomes: list[Outcome] = [
        httpx.Response(
            200,
            text="Dry run: Added: {:page 1}.",
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    ]

    result = await make_client(outcomes, []).call(
        "logseq.DB.upsertNodes", [[], {"dry-run": True}]
    )

    assert result == "Dry run: Added: {:page 1}."


@pytest.mark.asyncio
async def test_readback_polling_accepts_delayed_state() -> None:
    client = LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        readback_attempts=3,
        readback_delay=0,
    )
    values = iter([None, None, {"committed": True}])

    async def reader():
        return next(values)

    result = await client.poll_readback(reader, lambda value: value is not None)

    assert result == {"committed": True}


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_read_transport_failure_retries_with_fresh_client() -> None:
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("first connection failed"),
        httpx.Response(200, json={"ok": True}),
    ]
    lifecycle: list[str] = []
    client = LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        read_attempts=2,
        client_factory=lambda **kwargs: ScriptedClient(outcomes, lifecycle, **kwargs),
    )

    assert await client.call("logseq.DB.getAllTags", []) == {"ok": True}
    assert lifecycle == ["created", "closed"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("logseq.DB.upsertNodes", [[], {"dry-run": False}]),
        (
            "logseq.DB.insertBlock",
            [
                "12345678-1234-5678-1234-567812345678",
                "Child",
                {
                    "customUUID": "87654321-4321-8765-4321-876543218765",
                    "sibling": False,
                },
            ],
        ),
        (
            "logseq.DB.moveBlock",
            [
                "87654321-4321-8765-4321-876543218765",
                "12345678-1234-5678-1234-567812345678",
                {"children": True},
            ],
        ),
        (
            "logseq.DB.removeBlock",
            ["87654321-4321-8765-4321-876543218765", {}],
        ),
    ],
)
async def test_write_transport_failure_is_never_retried(
    method: str, args: list[Any]
) -> None:
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("ambiguous write"),
        httpx.Response(200, json={"unexpected": True}),
    ]
    lifecycle: list[str] = []
    client = LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        read_attempts=2,
        client_factory=lambda **kwargs: ScriptedClient(outcomes, lifecycle, **kwargs),
    )

    with pytest.raises(httpx.ReadTimeout):
        await client.call(method, args)
    assert lifecycle == ["created", "closed"]


@pytest.mark.asyncio
async def test_datascript_timeout_is_never_retried() -> None:
    outcomes: list[Outcome] = [
        httpx.ReadTimeout("expensive query timed out"),
        httpx.Response(200, json={"unexpected": True}),
    ]
    lifecycle: list[str] = []
    client = LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        read_attempts=2,
        client_factory=lambda **kwargs: ScriptedClient(outcomes, lifecycle, **kwargs),
    )

    with pytest.raises(httpx.ReadTimeout):
        await client.call("logseq.DB.datascriptQuery", ["[:find ?e]"])
    assert lifecycle == ["created", "closed"]


@pytest.mark.asyncio
async def test_http_error_retains_logseq_diagnostic_body() -> None:
    outcomes: list[Outcome] = [
        httpx.Response(500, json={"error": "Not a valid property"})
    ]

    with pytest.raises(LogseqAPIError, match="500.*Not a valid property"):
        await make_client(outcomes, []).call("logseq.DB.getAllTags", [])


@pytest.mark.asyncio
async def test_oversized_response_is_rejected() -> None:
    outcomes: list[Outcome] = [httpx.Response(200, json={"value": "x" * 100})]
    client = LogseqDBClient(
        "http://127.0.0.1:12315",
        "token",
        max_response_bytes=20,
        client_factory=lambda **kwargs: ScriptedClient(outcomes, [], **kwargs),
    )

    with pytest.raises(LogseqProtocolError, match="exceeds 20 bytes"):
        await client.call("logseq.DB.getAllTags", [])


@pytest.mark.asyncio
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
        "https://localhost:12315/api/",
        "secret-token",
        connect_timeout=2,
        read_timeout=9,
        verify_ssl=False,
        client_factory=factory,
    )

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


@pytest.mark.parametrize("url", ["", "localhost:12315", "ftp://localhost/api"])
def test_invalid_api_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP\\(S\\) URL"):
        LogseqDBClient(url, "token")
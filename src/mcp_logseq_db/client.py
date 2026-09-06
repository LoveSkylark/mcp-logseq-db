"""Failure-isolated client for the Logseq DB HTTP API.

SCOPE
-----
`ALLOWED_METHODS` is derived from what the tool surface actually routes
through -- nothing more. A method that no tool uses is not reachable, so the
allowlist doubles as documentation of the real dependency set.

WHAT CHANGED, AND WHY
---------------------
The graph-worker CLI paths are gone. They existed because a hardcoded
capability list reported `removeBlock`, `getBlock` and `updateBlock` as
rejected. All three work over HTTP; `delete_block_via_cli` was routing around
a method that was never broken. Nested block creation likewise works over HTTP
via `upsertNodes` (`page-id` accepts a block UUID), so `insert_block_via_cli`
had no reason to exist either. `move_block_via_cli` supported a tool that the
current surface does not expose; if block movement returns, it needs a route
established by testing rather than inherited from the same wrong list.

`write_and_verify` is new and is the point of this module. This API returns
success for calls that do nothing -- a wrong identifier type, an unresolvable
name, or an unsupported combination all produce `null` or a stock
acknowledgement. A write that is not read back is a write whose outcome is
unknown, so verification is built into the write path rather than left to each
caller.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

import httpx

from .access import WriteAccessPolicy

# --------------------------------------------------------------------------
# Method allowlist. Every entry is reachable from at least one tool; grouped by
# the tools that depend on it so an unused method is visible as unused.
# --------------------------------------------------------------------------

_CONNECTION_METHODS = frozenset({
    "logseq.DB.getAppInfo",             # capabilities
    "logseq.DB.checkCurrentIsDbGraph",  # capabilities
    "logseq.DB.getCurrentGraph",        # capabilities
})

_READ_METHODS = frozenset({
    # Every getPage/getBlockUUID/list*/find* tool routes here.
    "logseq.DB.datascriptQuery",
    # Dedicated reads with no query equivalent worth preferring.
    "logseq.DB.getBlock",               # getBlock
    "logseq.DB.getTagsByName",          # getTagUUID
    "logseq.DB.getAllTags",             # listTags
    "logseq.DB.getAllProperties",       # listProperties
})

WRITE_METHODS = frozenset({
    "logseq.DB.upsertNodes",            # createPage
    "logseq.DB.insertBlock",            # createBlock
    "logseq.DB.insertBatchBlock",       # createManyBlocks, createPageofBlocks
    "logseq.DB.moveBlock",              # moveBlock
    "logseq.DB.updateBlock",            # updateBlock
    "logseq.DB.removeBlock",            # removeBlock
    "logseq.DB.createTag",              # creatTag
    "logseq.DB.renamePage",             # renamePage
    "logseq.DB.deletePage",             # deleteTag, deletePage
    "logseq.DB.addBlockTag",            # addTag
    "logseq.DB.removeBlockTag",         # removeTag
    "logseq.DB.upsertProperty",         # createProperty
    "logseq.DB.removeProperty",         # deleteProperty
    "logseq.DB.upsertBlockProperty",    # addProperty
    "logseq.DB.removeBlockProperty",    # removeProperty
})

ALLOWED_METHODS = _CONNECTION_METHODS | _READ_METHODS | WRITE_METHODS

# Some responses come back as text/plain rather than JSON.
PLAIN_TEXT_METHODS = frozenset({
    "logseq.DB.datascriptQuery",
    "logseq.DB.upsertNodes",
})

# Queries can be expensive; a retry doubles the load without improving the
# odds, since a query that timed out once will time out again.
NO_RETRY_READ_METHODS = frozenset({
    "logseq.DB.datascriptQuery",
})


class LogseqProtocolError(RuntimeError):
    """Raised when Logseq returns a response that is not valid API JSON."""


class LogseqAPIError(RuntimeError):
    """Raised for a non-success response while retaining Logseq diagnostics."""


class WriteCircuitOpenError(RuntimeError):
    """Raised when an earlier ambiguous timeout has blocked later writes."""


class UnverifiedWriteError(RuntimeError):
    """
    Raised when a write returned successfully but the read-back did not show
    the expected state.

    This is the characteristic failure of this API, not an edge case: a wrong
    identifier type produces exactly this shape. Both the observed state and
    the state before the write are attached so a caller can tell "nothing
    happened" from "something else happened".
    """

    def __init__(self, message: str, *, before: Any = None, after: Any = None):
        super().__init__(message)
        self.before = before
        self.after = after


ClientFactory = Callable[..., httpx.AsyncClient]


def serialized_write(method):
    """Hold the write lock across a mutation and its verification."""
    @wraps(method)
    async def wrapper(self, *args, **kwargs):
        scope = getattr(self._client, "write_scope", None)
        if scope is None:
            return await method(self, *args, **kwargs)
        async with scope():
            return await method(self, *args, **kwargs)
    return wrapper


async def poll_readback(client, reader, predicate):
    """Use configured polling for real clients and one read for test doubles."""
    poll = getattr(client, "poll_readback", None)
    if poll is None:
        return await reader()
    return await poll(reader, predicate)


class LogseqDBClient:
    """Call Logseq with a fresh, deterministically closed client per request."""

    def __init__(
        self,
        base_url: str,
        api_token: str,
        *,
        connect_timeout: float = 3.0,
        read_timeout: float = 15.0,
        verify_ssl: bool = True,
        readback_attempts: int = 3,
        readback_delay: float = 0.15,
        read_attempts: int = 1,
        write_policy: WriteAccessPolicy | None = None,
        writable_property_prefix: str = "plugin.property.",
        max_response_bytes: int = 5_000_000,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        self._url = _api_endpoint(base_url)
        self._api_token = api_token
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        self._request_deadline = connect_timeout + read_timeout
        self._verify_ssl = verify_ssl
        self._client_factory = client_factory
        self._write_lock = asyncio.Lock()
        self._write_circuit_reason: str | None = None
        self.readback_attempts = max(1, readback_attempts)
        self.readback_delay = max(0.0, readback_delay)
        self.read_attempts = max(1, read_attempts)
        self.write_policy = write_policy or WriteAccessPolicy()
        # Property idents outside this prefix are read-only over HTTP. Held
        # here so callers of any module can consult one source.
        self.writable_property_prefix = writable_property_prefix
        self.max_response_bytes = max(1, max_response_bytes)

    # ---------------------------------------------------------------- state

    @property
    def write_circuit_open(self) -> bool:
        return self._write_circuit_reason is not None

    @property
    def write_circuit_reason(self) -> str | None:
        return self._write_circuit_reason

    @asynccontextmanager
    async def write_scope(self):
        """Serialize a mutation and all of its read-back verification."""
        async with self._write_lock:
            yield

    async def poll_readback(self, reader, predicate):
        """Retry only an idempotent read, never the preceding write."""
        last_value = None
        last_error: Exception | None = None
        for attempt in range(self.readback_attempts):
            try:
                last_value = await reader()
                last_error = None
                if predicate(last_value):
                    return last_value
            except Exception as error:  # noqa: BLE001 -- retried below
                last_error = error
            if attempt + 1 < self.readback_attempts and self.readback_delay:
                await asyncio.sleep(self.readback_delay)
        if last_error is not None:
            raise last_error
        return last_value

    # ---------------------------------------------------------------- write

    async def write_and_verify(
        self,
        method: str,
        args: list[Any],
        *,
        reader: Callable[[], Awaitable[Any]],
        predicate: Callable[[Any], bool],
        description: str,
    ) -> Any:
        """
        Perform a write and prove it happened.

        The response is deliberately ignored as evidence. `null` is returned by
        writes that succeeded and by writes that silently did nothing, so it
        carries no information; only the read-back does.

        `reader` is snapshotted before the write so the failure message can
        distinguish "unchanged" from "changed unexpectedly". It must be
        idempotent -- it is retried, the write never is.
        """
        async with self.write_scope():
            try:
                before = await reader()
            except Exception:  # noqa: BLE001 -- a missing target is a valid before
                before = None

            await self.call(method, args)

            after = await self.poll_readback(reader, predicate)
            if not predicate(after):
                raise UnverifiedWriteError(
                    f"{description}: the call returned without error but the "
                    "read-back does not show the expected state. This usually "
                    "means an identifier of the wrong type was supplied -- "
                    "this API reports success for writes that do nothing.",
                    before=before,
                    after=after,
                )
            return after

    # ----------------------------------------------------------------- call

    async def call(self, method: str, args: list[Any]) -> Any:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"Logseq DB API method is not allowed: {method!r}")
        if not isinstance(args, list):
            raise TypeError("Logseq API args must be a list")

        is_write = method in WRITE_METHODS
        if is_write and self._write_circuit_reason is not None:
            raise WriteCircuitOpenError(
                "Writes are blocked because an earlier write timed out with an "
                f"ambiguous result: {self._write_circuit_reason}. Read the "
                "target state, restart Logseq, then reconnect the MCP before "
                "another write."
            )

        attempts = (
            1
            if is_write or method in NO_RETRY_READ_METHODS
            else self.read_attempts
        )
        for attempt in range(attempts):
            try:
                async with asyncio.timeout(self._request_deadline):
                    return await self._call_once(method, args)
            except TimeoutError as error:
                timeout = httpx.ReadTimeout(
                    f"{method} exceeded the {self._request_deadline:g}-second "
                    "hard request deadline"
                )
                if is_write:
                    self._write_circuit_reason = str(timeout)
                if attempt + 1 == attempts:
                    raise timeout from error
            except httpx.TransportError as error:
                if is_write and isinstance(error, httpx.TimeoutException):
                    self._write_circuit_reason = (
                        f"{method}: {error or 'request timed out'}")
                if attempt + 1 == attempts:
                    raise
        raise RuntimeError("unreachable")

    async def _call_once(self, method: str, args: list[Any]) -> Any:
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "Connection": "close",
        }
        async with self._client_factory(
            timeout=self._timeout,
            verify=self._verify_ssl,
            headers=headers,
        ) as client:
            async with client.stream(
                "POST",
                self._url,
                json={"method": method, "args": args},
            ) as response:
                content = await self._read_limited(response, method)
                encoding = response.encoding or "utf-8"
                text = content.decode(encoding, errors="replace")
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as error:
                    body = text.strip()[:2000]
                    raise LogseqAPIError(
                        f"{method} failed ({response.status_code}): "
                        f"{body or '<empty response body>'}"
                    ) from error
                try:
                    result = json.loads(content)
                except (UnicodeDecodeError, ValueError) as error:
                    content_type = response.headers.get(
                        "content-type", "").lower()
                    if (method in PLAIN_TEXT_METHODS
                            and content_type.startswith("text/plain")):
                        result = text
                    else:
                        raise LogseqProtocolError(
                            f"{method} returned malformed JSON"
                        ) from error
            return result

    async def _read_limited(self, response: httpx.Response, method: str) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_response_bytes:
                    raise LogseqProtocolError(
                        f"{method} response exceeds "
                        f"{self.max_response_bytes} bytes"
                    )
            except ValueError:
                pass

        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > self.max_response_bytes:
                raise LogseqProtocolError(
                    f"{method} response exceeds {self.max_response_bytes} bytes"
                )
        return bytes(content)


def _api_endpoint(base_url: str) -> str:
    value = base_url.strip()
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as error:
        raise ValueError("Logseq API URL must be a valid absolute URL") from error
    if url.scheme not in {"http", "https"} or not url.host:
        raise ValueError("Logseq API URL must be an absolute HTTP(S) URL")
    if url.query or url.fragment:
        raise ValueError("Logseq API URL must not contain a query or fragment")
    path = url.path.rstrip("/")
    if not path.endswith("/api"):
        path = f"{path}/api"
    return str(url.copy_with(path=path or "/api"))

"""Failure-isolated client for the Logseq DB HTTP API."""

import asyncio
from contextlib import asynccontextmanager
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx

from .access import WriteAccessPolicy


ALLOWED_METHODS = frozenset({
    "logseq.DB.checkCurrentIsDbGraph",
    "logseq.DB.getAppInfo",
    "logseq.DB.getCurrentGraph",
    "logseq.DB.q",
    "logseq.DB.customQuery",
    "logseq.DB.datascriptQuery",
    "logseq.DB.search",
    "logseq.DB.listPages",
    "logseq.DB.listTags",
    "logseq.DB.listProperties",
    "logseq.DB.getAllPages",
    "logseq.DB.getPageData",
    "logseq.DB.getProperty",
    "logseq.DB.getAllProperties",
    "logseq.DB.getAllTags",
    "logseq.DB.getTagObjects",
    "logseq.DB.getTag",
    "logseq.DB.getTagsByName",
    "logseq.DB.upsertProperty",
    "logseq.DB.removeProperty",
    "logseq.DB.upsertBlockProperty",
    "logseq.DB.removeBlockProperty",
    "logseq.DB.createTag",
    "logseq.DB.addTagProperty",
    "logseq.DB.removeTagProperty",
    "logseq.DB.addTagExtends",
    "logseq.DB.removeTagExtends",
    "logseq.DB.addBlockTag",
    "logseq.DB.removeBlockTag",
    "logseq.DB.setBlockIcon",
    "logseq.DB.removeBlockIcon",
    "logseq.DB.addPropertyValueChoices",
    "logseq.DB.getFileContent",
    "logseq.DB.setFileContent",
    "logseq.DB.upsertNodes",
    "logseq.DB.renamePage",
    "logseq.DB.deletePage",
    "logseq.DB.insertBlock",
    "logseq.DB.moveBlock",
    "logseq.DB.removeBlock",
})

PLAIN_TEXT_METHODS = frozenset({
    "logseq.DB.q",
    "logseq.DB.customQuery",
    "logseq.DB.datascriptQuery",
    "logseq.DB.upsertNodes",
})

NO_RETRY_READ_METHODS = frozenset({
    "logseq.DB.q",
    "logseq.DB.customQuery",
    "logseq.DB.datascriptQuery",
    "logseq.DB.search",
})

WRITE_METHODS = frozenset({
    "logseq.DB.upsertProperty",
    "logseq.DB.removeProperty",
    "logseq.DB.upsertBlockProperty",
    "logseq.DB.removeBlockProperty",
    "logseq.DB.createTag",
    "logseq.DB.addTagProperty",
    "logseq.DB.removeTagProperty",
    "logseq.DB.addTagExtends",
    "logseq.DB.removeTagExtends",
    "logseq.DB.addBlockTag",
    "logseq.DB.removeBlockTag",
    "logseq.DB.setBlockIcon",
    "logseq.DB.removeBlockIcon",
    "logseq.DB.addPropertyValueChoices",
    "logseq.DB.setFileContent",
    "logseq.DB.upsertNodes",
    "logseq.DB.renamePage",
    "logseq.DB.deletePage",
})

EXPERIMENTAL_WRITE_METHODS = frozenset({
    "logseq.DB.insertBlock",
    "logseq.DB.moveBlock",
    "logseq.DB.removeBlock",
})

# Promoted only after live response-shape, read-back, cleanup, and MCP testing
# against Logseq 2.0.1 on 2026-09-01.
VERIFIED_WRITE_METHODS = frozenset({
    "logseq.DB.upsertProperty",
    "logseq.DB.removeProperty",
    "logseq.DB.upsertBlockProperty",
    "logseq.DB.removeBlockProperty",
    "logseq.DB.createTag",
    "logseq.DB.addTagProperty",
    "logseq.DB.removeTagProperty",
    "logseq.DB.addTagExtends",
    "logseq.DB.removeTagExtends",
    "logseq.DB.addBlockTag",
    "logseq.DB.removeBlockTag",
    "logseq.DB.setBlockIcon",
    "logseq.DB.removeBlockIcon",
    "logseq.DB.upsertNodes",
    "logseq.DB.renamePage",
    "logseq.DB.deletePage",
})


class LogseqProtocolError(RuntimeError):
    """Raised when Logseq returns a response that is not valid API JSON."""


class LogseqAPIError(RuntimeError):
    """Raised for a non-success response while retaining Logseq diagnostics."""


ClientFactory = Callable[..., httpx.AsyncClient]


def serialized_write(method):
    """Hold the real client's write lock across mutation and verification."""
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
        experimental_writes_enabled: bool = False,
        write_policy: WriteAccessPolicy | None = None,
        max_response_bytes: int = 5_000_000,
        client_factory: ClientFactory = httpx.AsyncClient,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/api"
        self._api_token = api_token
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        self._verify_ssl = verify_ssl
        self._client_factory = client_factory
        self._observed_methods: set[str] = set()
        self._write_lock = asyncio.Lock()
        self.readback_attempts = max(1, readback_attempts)
        self.readback_delay = max(0.0, readback_delay)
        self.read_attempts = max(1, read_attempts)
        self.experimental_writes_enabled = experimental_writes_enabled
        self.write_policy = write_policy or WriteAccessPolicy()
        self.max_response_bytes = max(1, max_response_bytes)

    @property
    def observed_methods(self) -> frozenset[str]:
        return frozenset(self._observed_methods)

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
            except Exception as error:
                last_error = error
            if attempt + 1 < self.readback_attempts and self.readback_delay:
                await asyncio.sleep(self.readback_delay)
        if last_error is not None:
            raise last_error
        return last_value

    async def call(self, method: str, args: list[Any]) -> Any:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"Logseq DB API method is not allowed: {method!r}")
        if not isinstance(args, list):
            raise TypeError("Logseq API args must be a list")

        attempts = (
            1
            if method in WRITE_METHODS
            or method in EXPERIMENTAL_WRITE_METHODS
            or method in NO_RETRY_READ_METHODS
            else self.read_attempts
        )
        for attempt in range(attempts):
            try:
                return await self._call_once(method, args)
            except httpx.TransportError:
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
            response = await client.post(
                self._url,
                json={"method": method, "args": args},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                body = response.text.strip()[:2000]
                raise LogseqAPIError(
                    f"{method} failed ({response.status_code}): "
                    f"{body or '<empty response body>'}"
                ) from error
            if len(response.content) > self.max_response_bytes:
                raise LogseqProtocolError(
                    f"{method} response exceeds {self.max_response_bytes} bytes"
                )
            try:
                result = response.json()
            except ValueError as error:
                content_type = response.headers.get("content-type", "").lower()
                if method in PLAIN_TEXT_METHODS and content_type.startswith("text/plain"):
                    result = response.text
                else:
                    raise LogseqProtocolError(
                        f"{method} returned malformed JSON"
                    ) from error
            self._observed_methods.add(method)
            return result
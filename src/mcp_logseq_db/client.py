"""Failure-isolated client for the Logseq DB HTTP API."""

import asyncio
import json
import shutil
import subprocess
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
    "logseq.DB.datascriptQuery",
    "logseq.DB.upsertNodes",
})

NO_RETRY_READ_METHODS = frozenset({
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


class WriteCircuitOpenError(RuntimeError):
    """Raised when an earlier ambiguous timeout has blocked later writes."""


ClientFactory = Callable[..., httpx.AsyncClient]
CLIRunner = Callable[..., subprocess.CompletedProcess[str]]


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
        cli_command: str = "logseq",
        cli_runner: CLIRunner = subprocess.run,
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
        self._cli_command = cli_command
        self._cli_runner = cli_runner
        self._observed_methods: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._write_circuit_reason: str | None = None
        self.readback_attempts = max(1, readback_attempts)
        self.readback_delay = max(0.0, readback_delay)
        self.read_attempts = max(1, read_attempts)
        self.experimental_writes_enabled = experimental_writes_enabled
        self.write_policy = write_policy or WriteAccessPolicy()
        self.max_response_bytes = max(1, max_response_bytes)

    @property
    def observed_methods(self) -> frozenset[str]:
        return frozenset(self._observed_methods)

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
            except Exception as error:
                last_error = error
            if attempt + 1 < self.readback_attempts and self.readback_delay:
                await asyncio.sleep(self.readback_delay)
        if last_error is not None:
            raise last_error
        return last_value

    async def delete_block_via_cli(self, block_uuid: str) -> Any:
        """Delete a block through Logseq's graph-worker outliner operation."""
        result = await self._run_graph_cli(["remove", "block", "--uuid", block_uuid])
        return result

    async def insert_block_via_cli(
        self, target_uuid: str, title: str, placement: str
    ) -> int:
        """Insert a block through Logseq's graph-worker outliner operation."""
        position = {"child": "last-child", "after": "sibling"}.get(placement)
        if position is None:
            raise ValueError("Stable block insertion supports child or after placement")
        result = await self._run_graph_cli([
            "upsert",
            "block",
            "--target-uuid",
            target_uuid,
            "--pos",
            position,
            "--content",
            title,
        ])
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], int):
            raise LogseqProtocolError("Logseq CLI insertion did not return one entity id")
        return result[0]

    async def move_block_via_cli(
        self, block_uuid: str, target_uuid: str, placement: str
    ) -> Any:
        """Move a block through Logseq's graph-worker outliner operation."""
        position = {"child": "last-child", "after": "sibling"}.get(placement)
        if position is None:
            raise ValueError("Stable block movement supports child or after placement")
        return await self._run_graph_cli([
            "upsert",
            "block",
            "--uuid",
            block_uuid,
            "--target-uuid",
            target_uuid,
            "--pos",
            position,
        ])

    async def update_block_tag_via_cli(
        self, block_uuid: str, tag_ident: str, *, remove: bool
    ) -> Any:
        """Add or remove one exact tag through the graph-worker outliner path."""
        if not tag_ident.startswith(":") or "/" not in tag_ident:
            raise ValueError("Tag ident must be an exact namespaced keyword")
        option = "--remove-tags" if remove else "--update-tags"
        return await self._run_graph_cli([
            "upsert",
            "block",
            "--uuid",
            block_uuid,
            option,
            f"[{tag_ident}]",
        ])

    async def _run_graph_cli(self, arguments: list[str]) -> Any:
        """Run one fixed Logseq CLI operation against the connected graph."""
        if self._write_circuit_reason is not None:
            raise WriteCircuitOpenError(
                "Writes are blocked because an earlier write timed out with an "
                f"ambiguous result: {self._write_circuit_reason}. Read the target "
                "state, restart Logseq, then reconnect the MCP before another write."
            )

        graph_info = await self.call("logseq.DB.getCurrentGraph", [])
        if not isinstance(graph_info, dict) or not isinstance(graph_info.get("path"), str):
            raise LogseqProtocolError("Current graph did not provide an absolute path")
        normalized_path = graph_info["path"].replace("\\", "/").rstrip("/")
        if "/graphs/" not in normalized_path:
            raise LogseqProtocolError("Current graph path is not under a graphs directory")
        root_dir, graph_name = normalized_path.rsplit("/graphs/", 1)
        if not root_dir or not graph_name or "/" in graph_name:
            raise LogseqProtocolError("Current graph path cannot identify one graph")

        executable = shutil.which(self._cli_command)
        if executable is None:
            raise RuntimeError(
                "Logseq CLI is required for supported DB block deletion but was not found"
            )
        command = [
            executable,
            "--root-dir",
            root_dir,
            "--graph",
            graph_name,
            "--output",
            "json",
            *arguments,
        ]
        try:
            completed = await asyncio.to_thread(
                self._cli_runner,
                command,
                capture_output=True,
                text=True,
                timeout=self._request_deadline,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            self._write_circuit_reason = "Logseq CLI write timed out"
            raise httpx.ReadTimeout(self._write_circuit_reason) from error

        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise LogseqProtocolError(
                "Logseq CLI write returned malformed JSON"
            ) from error
        if completed.returncode != 0 or not isinstance(result, dict) or result.get("status") != "ok":
            detail = completed.stderr.strip() or json.dumps(result)
            raise LogseqAPIError(f"Logseq CLI write failed: {detail[:2000]}")
        return result.get("data", {}).get("result")

    async def call(self, method: str, args: list[Any]) -> Any:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"Logseq DB API method is not allowed: {method!r}")
        if not isinstance(args, list):
            raise TypeError("Logseq API args must be a list")

        is_write = method in WRITE_METHODS or method in EXPERIMENTAL_WRITE_METHODS
        if is_write and self._write_circuit_reason is not None:
            raise WriteCircuitOpenError(
                "Writes are blocked because an earlier write timed out with an "
                f"ambiguous result: {self._write_circuit_reason}. Read the target "
                "state, restart Logseq, then reconnect the MCP before another write."
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
                    self._write_circuit_reason = f"{method}: {error or 'request timed out'}"
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
                    content_type = response.headers.get("content-type", "").lower()
                    if method in PLAIN_TEXT_METHODS and content_type.startswith("text/plain"):
                        result = text
                    else:
                        raise LogseqProtocolError(
                            f"{method} returned malformed JSON"
                        ) from error
            self._observed_methods.add(method)
            return result

    async def _read_limited(self, response: httpx.Response, method: str) -> bytes:
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_response_bytes:
                    raise LogseqProtocolError(
                        f"{method} response exceeds {self.max_response_bytes} bytes"
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
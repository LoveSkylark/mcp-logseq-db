"""Environment settings for the DB-only server."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Callable, TypeVar

import httpx

Number = TypeVar("Number", int, float)

# Property idents this caller may write take the form
# :plugin.property.<plugin_id>/<Title>. The namespace is assigned by Logseq
# from caller identity and cannot be chosen, so the id is discovered from the
# graph rather than configured -- but it can be pinned when the graph is not
# reachable at startup, or when a caller wants the guard to fail loudly on a
# mismatch instead of adapting.
PLUGIN_ID_PATTERN = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]*\Z")

FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    api_url: str
    api_token: str
    connect_timeout: float
    read_timeout: float
    verify_ssl: bool
    readback_attempts: int
    readback_delay: float
    read_attempts: int
    plugin_id: str | None
    probe_writes: bool
    write_title_prefixes: tuple[str, ...]
    write_property_prefixes: tuple[str, ...]
    write_entity_uuids: frozenset[str]
    max_response_bytes: int

    @property
    def writable_property_prefix(self) -> str:
        """
        The ident prefix this caller can write, as far as configuration knows.

        With a plugin id this is exact -- `plugin.property.my_plugin/`. Without
        one it is the namespace family, which is wide enough to admit another
        plugin's properties; those pass the local guard and then fail at the
        API. Set LOGSEQ_PLUGIN_ID to close that gap.
        """
        if self.plugin_id:
            return f"plugin.property.{self.plugin_id}/"
        return "plugin.property."

    @property
    def property_prefixes(self) -> tuple[str, ...]:
        """
        Prefixes the write policy should enforce.

        The sandbox prefix is always included, so the namespace limit and any
        configured allowlist are a single mechanism rather than two that can
        disagree. An explicit LOGSEQ_WRITE_PROPERTY_PREFIXES narrows further
        within the sandbox; it cannot widen past it.
        """
        if self.write_property_prefixes:
            return self.write_property_prefixes
        return (self.writable_property_prefix,)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("LOGSEQ_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("LOGSEQ_API_TOKEN is required")
        return cls(
            api_url=_validated_api_url(
                "LOGSEQ_API_URL", "http://127.0.0.1:12315"),
            api_token=token,
            connect_timeout=_positive_float("LOGSEQ_API_CONNECT_TIMEOUT", 3.0),
            read_timeout=_positive_float("LOGSEQ_API_READ_TIMEOUT", 15.0),
            verify_ssl=_flag("LOGSEQ_VERIFY_SSL", True),
            readback_attempts=_positive_int("LOGSEQ_READBACK_ATTEMPTS", 3),
            readback_delay=_nonnegative_float("LOGSEQ_READBACK_DELAY", 0.15),
            # Applies only to the dedicated reads. datascriptQuery -- the route
            # for nearly every read -- never retries: a query that timed out
            # once will time out again, and a retry doubles the load on a
            # worker that is already struggling.
            read_attempts=_positive_int("LOGSEQ_READ_ATTEMPTS", 2),
            plugin_id=_plugin_id("LOGSEQ_PLUGIN_ID"),
            # Write probing calls each write method once with deliberately
            # invalid arguments, which mutates nothing but costs ~11 requests
            # per capabilities call. Turning it off marks every write tool
            # `unknown` rather than assuming it works.
            probe_writes=_flag("LOGSEQ_PROBE_WRITES", True),
            write_title_prefixes=_csv("LOGSEQ_WRITE_TITLE_PREFIXES"),
            write_property_prefixes=_csv("LOGSEQ_WRITE_PROPERTY_PREFIXES"),
            write_entity_uuids=frozenset(_csv("LOGSEQ_WRITE_ENTITY_UUIDS")),
            max_response_bytes=_positive_int(
                "LOGSEQ_MAX_RESPONSE_BYTES", 5_000_000),
        )


def _validated_api_url(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip()
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL as error:
        raise RuntimeError(f"{name} must be a valid absolute URL") from error
    if url.scheme not in {"http", "https"} or not url.host:
        raise RuntimeError(f"{name} must be an absolute HTTP(S) URL")
    return raw


def _plugin_id(name: str) -> str | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    # Accept the bare id or the full namespace, since the graph reports idents
    # in the longer form and pasting one is the obvious mistake.
    if raw.startswith(":"):
        raw = raw[1:]
    if raw.startswith("plugin.property."):
        raw = raw[len("plugin.property."):].split("/", 1)[0]
    if not PLUGIN_ID_PATTERN.match(raw):
        raise RuntimeError(
            f"{name} must be a plugin id such as _test_plugin, or the full "
            "namespace plugin.property._test_plugin"
        )
    return raw


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in FALSE_VALUES


def _env_number(
    name: str,
    default: Number,
    parser: Callable[[str], Number],
    is_valid: Callable[[Number], bool],
    description: str,
) -> Number:
    raw = os.getenv(name, str(default))
    try:
        value = parser(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be {description}") from error
    if not is_valid(value):
        raise RuntimeError(f"{name} must be {description}")
    return value


def _positive_float(name: str, default: float) -> float:
    return _env_number(
        name, default, float,
        lambda value: math.isfinite(value) and value > 0,
        "a positive number",
    )


def _nonnegative_float(name: str, default: float) -> float:
    return _env_number(
        name, default, float,
        lambda value: math.isfinite(value) and value >= 0,
        "a non-negative number",
    )


def _positive_int(name: str, default: int) -> int:
    return _env_number(
        name, default, int, lambda value: value > 0, "a positive integer")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )
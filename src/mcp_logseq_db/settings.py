"""Environment settings for the DB-only server."""

import math
import os
from dataclasses import dataclass
from typing import Callable, TypeVar

import httpx

Number = TypeVar("Number", int, float)


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
    write_title_prefixes: tuple[str, ...]
    write_property_prefixes: tuple[str, ...]
    write_entity_uuids: frozenset[str]
    max_response_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("LOGSEQ_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("LOGSEQ_API_TOKEN is required")
        return cls(
            api_url=_validated_api_url("LOGSEQ_API_URL", "http://127.0.0.1:12315"),
            api_token=token,
            connect_timeout=_positive_float("LOGSEQ_API_CONNECT_TIMEOUT", 3.0),
            read_timeout=_positive_float("LOGSEQ_API_READ_TIMEOUT", 15.0),
            verify_ssl=os.getenv("LOGSEQ_VERIFY_SSL", "true").lower()
            not in {"0", "false", "no"},
            readback_attempts=_positive_int("LOGSEQ_READBACK_ATTEMPTS", 3),
            readback_delay=_nonnegative_float("LOGSEQ_READBACK_DELAY", 0.15),
            read_attempts=_positive_int("LOGSEQ_READ_ATTEMPTS", 2),
            write_title_prefixes=_csv("LOGSEQ_WRITE_TITLE_PREFIXES"),
            write_property_prefixes=_csv("LOGSEQ_WRITE_PROPERTY_PREFIXES"),
            write_entity_uuids=frozenset(_csv("LOGSEQ_WRITE_ENTITY_UUIDS")),
            max_response_bytes=_positive_int("LOGSEQ_MAX_RESPONSE_BYTES", 5_000_000),
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
        name, default, float, lambda value: math.isfinite(value) and value > 0,
        "a positive number",
    )


def _nonnegative_float(name: str, default: float) -> float:
    return _env_number(
        name, default, float, lambda value: math.isfinite(value) and value >= 0,
        "a non-negative number",
    )


def _positive_int(name: str, default: int) -> int:
    return _env_number(name, default, int, lambda value: value > 0, "a positive integer")


def _csv(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )
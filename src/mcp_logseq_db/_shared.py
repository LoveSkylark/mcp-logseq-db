"""Helpers shared by the verified content and mutation write paths."""

from __future__ import annotations

from typing import Any

import httpx

from .identifiers import require_uuid


class VerifiedWriteHelpers:
    """UUID validation, write-scope checks, and ambiguous-write handling."""

    _client: Any

    async def _call_ambiguous(self, method: str, args: list[Any]) -> tuple[Any, bool]:
        """
        Call a write and report whether the outcome is ambiguous.

        A timeout is not a failure -- Logseq may have applied the write before
        the connection dropped. The caller must resolve it by reading back,
        which is why the flag is returned rather than an exception raised.
        """
        try:
            return await self._client.call(method, args), False
        except httpx.TimeoutException:
            return None, True

    @staticmethod
    def _validated_uuid(value: Any, *, role: str = "UUID") -> str:
        """
        Normalise a UUID or explain what was passed instead.

        Accepts every form `uuid.UUID()` did -- braces, uppercase, missing
        separators, urn: prefix -- and rejects the wrong KIND of value with a
        message naming what it looks like. That distinction matters because
        passing a title or an ident here does not error at the API; it returns
        success and does nothing.
        """
        return require_uuid(value, role=role)

    def _require_title(self, title: Any) -> str:
        """Validate the title, then apply any configured write scope."""
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"Expected a non-empty title, got {title!r}")
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_title(title)
        return title

    def _require_entity(self, entity_uuid: str) -> str:
        """
        Apply any configured entity-UUID write scope.

        This must be called on every write target. An operator who sets
        LOGSEQ_WRITE_ENTITY_UUIDS is fail-closing deliberately; a write path
        that skips the check leaves them believing in a restriction that is not
        being applied.
        """
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_entity(entity_uuid)
        return entity_uuid
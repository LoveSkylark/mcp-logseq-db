"""Helpers shared by the verified content and mutation write paths."""

from typing import Any
from uuid import UUID

import httpx


class VerifiedWriteHelpers:
    """UUID validation, write-scope checks, and ambiguous-write handling."""

    _client: Any

    async def _call_ambiguous(self, method: str, args: list[Any]) -> tuple[Any, bool]:
        try:
            return await self._client.call(method, args), False
        except httpx.TimeoutException:
            return None, True

    @staticmethod
    def _validated_uuid(value: Any) -> str:
        try:
            return str(UUID(value))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"Expected an exact UUID, got {value!r}") from error

    def _require_title(self, title: str) -> None:
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_title(title)

    def _require_entity(self, entity_uuid: str) -> None:
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_entity(entity_uuid)

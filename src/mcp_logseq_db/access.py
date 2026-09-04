"""Optional fail-closed write scopes for non-lab deployments.

Two kinds of restriction live here and they are worth keeping distinct:

  - the PROPERTY NAMESPACE SANDBOX, which is imposed by Logseq itself. Writes
    reach only `plugin.property.<caller>/*`; anything else is read-only over
    HTTP no matter what this policy says. It is enforced here so the failure is
    a clear error rather than the silent no-op the API would otherwise return.

  - OPERATOR SCOPES, which are optional and narrow further. An empty tuple
    means "no restriction beyond the sandbox", not "deny everything".
"""

from dataclasses import dataclass


def _normalize_ident(value: str) -> str:
    """
    Compare idents without their leading colon.

    Idents arrive from the API as `:plugin.property.x/Title` but are naturally
    configured as `plugin.property.x/`. Comparing the two forms directly makes
    every write fail, so both sides are normalised before matching.
    """
    return value[1:] if value.startswith(":") else value


@dataclass(frozen=True)
class WriteAccessPolicy:
    title_prefixes: tuple[str, ...] = ()
    property_prefixes: tuple[str, ...] = ()
    entity_uuids: frozenset[str] = frozenset()

    def require_title(self, title: str) -> None:
        if self.title_prefixes and not title.startswith(self.title_prefixes):
            raise PermissionError(
                "Write denied: title must start with one of "
                f"{self.title_prefixes!r}"
            )

    def require_property(self, ident: str) -> None:
        if not self.property_prefixes:
            return
        candidate = _normalize_ident(ident)
        allowed = tuple(_normalize_ident(p) for p in self.property_prefixes)
        if not candidate.startswith(allowed):
            raise PermissionError(
                f"Write denied: property ident {ident!r} is outside the "
                f"writable prefixes {self.property_prefixes!r}. Properties "
                "created in the Logseq UI live under user.property/* and "
                "cannot be written over the HTTP API at all."
            )

    def require_entity(self, entity_uuid: str) -> None:
        if self.entity_uuids and entity_uuid not in self.entity_uuids:
            raise PermissionError(
                f"Write denied: entity UUID {entity_uuid} is outside the "
                "configured scope"
            )
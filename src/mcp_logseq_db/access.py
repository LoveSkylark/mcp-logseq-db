"""Optional fail-closed write scopes for non-lab deployments."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WriteAccessPolicy:
    title_prefixes: tuple[str, ...] = ()
    property_prefixes: tuple[str, ...] = ()
    entity_uuids: frozenset[str] = frozenset()

    def require_title(self, title: str) -> None:
        if self.title_prefixes and not title.startswith(self.title_prefixes):
            raise PermissionError(
                f"Write denied: title must start with one of {self.title_prefixes!r}"
            )

    def require_property(self, ident: str) -> None:
        if self.property_prefixes and not ident.startswith(self.property_prefixes):
            raise PermissionError(
                "Write denied: property ident is outside configured prefixes"
            )

    def require_entity(self, entity_uuid: str) -> None:
        if self.entity_uuids and entity_uuid not in self.entity_uuids:
            raise PermissionError(
                f"Write denied: entity UUID {entity_uuid} is outside configured scope"
            )
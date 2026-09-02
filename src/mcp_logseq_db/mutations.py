"""DB property mutations with exact resolution and mandatory read-back."""

from dataclasses import asdict, dataclass
import re
from typing import Any
from uuid import UUID
import httpx

from .client import LogseqDBClient, poll_readback, serialized_write


@dataclass(frozen=True)
class MutationResult:
    response: Any
    verified_state: Any
    recovered_after_timeout: bool = False
    previous_state: Any = None
    diagnostic: str | None = None
    verified: bool = True
    observed_state: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MutationVerificationError(RuntimeError):
    def __init__(self, result: MutationResult) -> None:
        super().__init__(result.diagnostic or "Mutation verification failed")
        self.result = result


class VerifiedMutations:
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client

    @serialized_write
    async def upsert_property(
        self,
        title: str,
        schema: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> MutationResult:
        if not title.strip():
            raise ValueError("Property title must not be empty")
        self._require_title(title)
        response, timed_out = await self._write(
            "logseq.DB.upsertProperty", [title, schema, options or {}]
        )
        if timed_out:
            raise RuntimeError(
                "Property upsert timed out before Logseq returned the generated ident; "
                "the result is ambiguous and must be resolved from getAllProperties"
            )
        if not isinstance(response, dict) or not response.get("ident"):
            raise RuntimeError("Property upsert did not return an exact ident")
        ident = self._validated_ident(str(response["ident"]))
        current = await poll_readback(
            self._client,
            lambda: self._client.call("logseq.DB.getProperty", [ident]),
            lambda entity: self._has_ident(entity, ident),
        )
        if not self._has_ident(current, ident):
            raise RuntimeError(f"Write verification failed for property {ident}")
        actual_title = current.get("title") if isinstance(current, dict) else None
        diagnostic = None
        if isinstance(actual_title, str) and actual_title != title:
            diagnostic = (
                f"Logseq normalized property title {title!r} to {actual_title!r}; "
                f"use exact ident {ident!r} for later operations"
            )
        return MutationResult(response, current, timed_out, diagnostic=diagnostic)

    @serialized_write
    async def remove_property(self, property_ident: str) -> MutationResult:
        ident = self._validated_ident(property_ident)
        self._require_property(ident)
        existing = await self._client.call("logseq.DB.getProperty", [ident])
        if not self._has_ident(existing, ident):
            raise LookupError(f"No property exists with exact ident {ident}")
        usage_before = await self._property_usage(ident, existing["id"])

        response, timed_out = await self._write("logseq.DB.removeProperty", [ident])
        current, usage_after = await poll_readback(
            self._client,
            lambda: self._property_removal_state(ident, existing["id"]),
            lambda state: state[0] is None and not state[1][0] and not state[1][1],
        )
        if current is not None:
            detail = " after a timeout" if timed_out else ""
            self._raise_verification(
                f"Property {ident} is still visible{detail}",
                response=response,
                previous_state={"property": existing, "usage": usage_before},
                observed_state={"property": current, "usage": usage_after},
                timed_out=timed_out,
            )
        if usage_after[0] or usage_after[1]:
            self._raise_verification(
                "Property removal left attributes or value entities behind",
                response=response,
                previous_state={"property": existing, "usage": usage_before},
                observed_state={"property": current, "usage": usage_after},
                timed_out=timed_out,
            )
        return MutationResult(
            response,
            None,
            timed_out,
            previous_state={"property": existing, "usage": usage_before},
        )

    @serialized_write
    async def create_tag(
        self, title: str, options: dict[str, Any] | None = None
    ) -> MutationResult:
        if not title.strip():
            raise ValueError("Tag title must not be empty")
        self._require_title(title)
        response, timed_out = await self._write(
            "logseq.DB.createTag", [title, options or {}]
        )
        if timed_out:
            raise RuntimeError("Tag creation timed out before returning its identity")
        if not isinstance(response, dict) or not response.get("ident"):
            raise RuntimeError("Tag creation did not return an exact ident")
        current = await poll_readback(
            self._client,
            lambda: self._client.call("logseq.DB.getTag", [response["ident"]]),
            lambda entity: (
                isinstance(entity, dict) and entity.get("uuid") == response.get("uuid")
            ),
        )
        if not isinstance(current, dict) or current.get("uuid") != response.get("uuid"):
            raise RuntimeError("Tag creation verification failed")
        return MutationResult(response, current)

    @serialized_write
    async def rename_tag(self, tag_uuid: str, new_title: str) -> MutationResult:
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        self._require_title(new_title)
        if not new_title.strip():
            raise ValueError("New tag title must not be empty")
        previous = await self._tag(tag_uuid)
        response = await self._client.call(
            "logseq.DB.renamePage", [tag_uuid, new_title]
        )
        current = await poll_readback(
            self._client,
            lambda: self._client.call("logseq.DB.getTag", [tag_uuid]),
            lambda entity: isinstance(entity, dict) and entity.get("title") == new_title,
        )
        if not isinstance(current, dict) or current.get("title") != new_title:
            self._raise_verification(
                "Tag rename verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
            )
        return MutationResult(response, current, previous_state=previous)

    @serialized_write
    async def delete_tag(self, tag_uuid: str) -> MutationResult:
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        previous = await self._tag(tag_uuid)
        response = await self._client.call("logseq.DB.deletePage", [tag_uuid])
        current = await poll_readback(
            self._client,
            lambda: self._client.call("logseq.DB.getTag", [tag_uuid]),
            lambda entity: entity is None,
        )
        if current is not None:
            self._raise_verification(
                "Tag deletion verification failed; tag is still visible",
                response=response,
                previous_state=previous,
                observed_state=current,
            )
        dangling = await poll_readback(
            self._client,
            lambda: self._tag_reference_ids(previous["id"]),
            lambda entity_ids: not entity_ids,
        )
        if dangling:
            self._raise_verification(
                f"Tag deletion left dangling references on entities {sorted(dangling)!r}",
                response=response,
                previous_state=previous,
                observed_state={"referencing_entity_ids": sorted(dangling)},
            )
        return MutationResult(response, None, previous_state=previous)

    @serialized_write
    async def add_tag_property(
        self, tag_uuid: str, property_ident: str
    ) -> MutationResult:
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        self._require_property(property_ident)
        property_entity = await self._property(property_ident)
        previous = await self._tag(tag_uuid)
        response, timed_out = await self._write(
            "logseq.DB.addTagProperty", [tag_uuid, property_ident]
        )
        current = await poll_readback(
            self._client,
            lambda: self._tag(tag_uuid),
            lambda entity: property_entity["id"]
            in entity.get(":logseq.property.class/properties", []),
        )
        if property_entity["id"] not in current.get(":logseq.property.class/properties", []):
            self._raise_verification(
                "Tag property addition verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def remove_tag_property(
        self, tag_uuid: str, property_ident: str
    ) -> MutationResult:
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        self._require_property(property_ident)
        property_entity = await self._property(property_ident)
        previous = await self._tag(tag_uuid)
        property_uuid = self._validated_uuid(str(property_entity.get("uuid")))
        response, timed_out = await self._write(
            "logseq.DB.removeTagProperty", [tag_uuid, property_uuid]
        )
        current = await poll_readback(
            self._client,
            lambda: self._tag(tag_uuid),
            lambda entity: property_entity["id"]
            not in entity.get(":logseq.property.class/properties", []),
        )
        if property_entity["id"] in current.get(":logseq.property.class/properties", []):
            self._raise_verification(
                "Tag property removal verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def set_tag_parent(
        self,
        tag_uuid: str,
        parent_tag_uuid: str,
        *,
        acknowledge_replacement: bool = False,
    ) -> MutationResult:
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        self._require_entity(self._validated_uuid(parent_tag_uuid))
        parent = await self._tag(parent_tag_uuid)
        previous = await self._tag(tag_uuid)
        previous_parent_ids = set(
            previous.get(":logseq.property.class/extends", [])
        )
        replaced_parent_ids = previous_parent_ids - {parent["id"]}
        if replaced_parent_ids and not acknowledge_replacement:
            raise ValueError(
                "Tag already has a different parent; set acknowledge_replacement=true "
                "to replace it"
            )
        response, timed_out = await self._write(
            "logseq.DB.addTagExtends", [tag_uuid, self._validated_uuid(parent_tag_uuid)]
        )
        current = await poll_readback(
            self._client,
            lambda: self._tag(tag_uuid),
            lambda entity: parent["id"] in entity.get(":logseq.property.class/extends", [])
            and not replaced_parent_ids.intersection(
                entity.get(":logseq.property.class/extends", [])
            ),
        )
        current_parent_ids = set(current.get(":logseq.property.class/extends", []))
        if parent["id"] not in current_parent_ids or replaced_parent_ids.intersection(
            current_parent_ids
        ):
            self._raise_verification(
                "Tag parent verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def remove_tag_extends(
        self, tag_uuid: str, parent_tag_uuid: str
    ) -> MutationResult:
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        self._require_entity(self._validated_uuid(parent_tag_uuid))
        parent = await self._tag(parent_tag_uuid)
        previous = await self._tag(tag_uuid)
        response, timed_out = await self._write(
            "logseq.DB.removeTagExtends", [tag_uuid, self._validated_uuid(parent_tag_uuid)]
        )
        current = await poll_readback(
            self._client,
            lambda: self._tag(tag_uuid),
            lambda entity: parent["id"]
            not in entity.get(":logseq.property.class/extends", []),
        )
        if parent["id"] in current.get(":logseq.property.class/extends", []):
            self._raise_verification(
                "Tag inheritance removal verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def upsert_block_property(
        self,
        block_uuid: str,
        property_ident: str,
        value: Any,
        options: dict[str, Any] | None = None,
    ) -> MutationResult:
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        self._require_property(property_ident)
        property_entity = await self._property(property_ident)
        previous = await self._block(block_uuid)
        response, timed_out = await self._write(
            "logseq.DB.upsertBlockProperty",
            [block_uuid, property_ident, value, options or {}],
        )
        current, matches = await poll_readback(
            self._client,
            lambda: self._block_property_state(
                block_uuid, property_ident, property_entity, value
            ),
            lambda state: state[1],
        )
        if not matches:
            raise MutationVerificationError(
                MutationResult(
                    response,
                    None,
                    timed_out,
                    previous_state=previous,
                    diagnostic="Block property value verification failed",
                    verified=False,
                    observed_state=current,
                )
            )
        return MutationResult(
            response,
            current,
            timed_out,
            previous_state=previous,
            observed_state=current,
        )

    @serialized_write
    async def remove_block_property(
        self, block_uuid: str, property_ident: str
    ) -> MutationResult:
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        self._require_property(property_ident)
        await self._property(property_ident)
        previous = await self._block(block_uuid)
        response, timed_out = await self._write(
            "logseq.DB.removeBlockProperty", [block_uuid, property_ident]
        )
        current = await poll_readback(
            self._client,
            lambda: self._entity(block_uuid),
            lambda entity: property_ident not in entity,
        )
        if property_ident in current:
            self._raise_verification(
                "Block property removal verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def add_block_tag(self, block_uuid: str, tag_uuid: str) -> MutationResult:
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        self._require_entity(self._validated_uuid(tag_uuid))
        previous = await self._block(block_uuid)
        tag = await self._tag(tag_uuid)
        cli_update = getattr(self._client, "update_block_tag_via_cli", None)
        if cli_update is None:
            response, timed_out = await self._write(
                "logseq.DB.addBlockTag", [block_uuid, self._validated_uuid(tag_uuid)]
            )
        else:
            response = await cli_update(block_uuid, tag["ident"], remove=False)
            timed_out = False
        current = await poll_readback(
            self._client,
            lambda: self._entity(block_uuid),
            lambda entity: tag["id"] in self._reference_ids(entity.get("tags", [])),
        )
        if tag["id"] not in self._reference_ids(current.get("tags", [])):
            self._raise_verification(
                "Block tag addition verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def remove_block_tag(self, block_uuid: str, tag_uuid: str) -> MutationResult:
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        self._require_entity(self._validated_uuid(tag_uuid))
        previous = await self._block(block_uuid)
        tag = await self._tag(tag_uuid)
        cli_update = getattr(self._client, "update_block_tag_via_cli", None)
        if cli_update is None:
            response, timed_out = await self._write(
                "logseq.DB.removeBlockTag", [block_uuid, self._validated_uuid(tag_uuid)]
            )
        else:
            response = await cli_update(block_uuid, tag["ident"], remove=True)
            timed_out = False
        current = await poll_readback(
            self._client,
            lambda: self._entity(block_uuid),
            lambda entity: tag["id"] not in self._reference_ids(entity.get("tags", [])),
        )
        if tag["id"] in self._reference_ids(current.get("tags", [])):
            self._raise_verification(
                "Block tag removal verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def add_page_tag(self, page_uuid: str, tag_uuid: str) -> MutationResult:
        return await self._update_page_tag(page_uuid, tag_uuid, remove=False)

    @serialized_write
    async def remove_page_tag(self, page_uuid: str, tag_uuid: str) -> MutationResult:
        return await self._update_page_tag(page_uuid, tag_uuid, remove=True)

    @serialized_write
    async def set_block_icon(
        self, block_uuid: str, icon_type: str, icon_name: str
    ) -> MutationResult:
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        if icon_type not in {"tabler-icon", "emoji"}:
            raise ValueError("icon_type must be 'tabler-icon' or 'emoji'")
        previous = await self._block(block_uuid)
        response, timed_out = await self._write(
            "logseq.DB.setBlockIcon", [block_uuid, icon_type, icon_name]
        )
        current = await poll_readback(
            self._client,
            lambda: self._entity(block_uuid),
            lambda entity: isinstance(entity.get(":logseq.property/icon"), dict),
        )
        icon = current.get(":logseq.property/icon")
        if not isinstance(icon, dict) or icon.get("type") != icon_type:
            self._raise_verification(
                "Block icon verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        if icon_type == "tabler-icon" and icon.get("id") != icon_name:
            self._raise_verification(
                "Block icon verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        if icon_type == "emoji" and icon.get("id") != self._normalized_emoji_id(icon_name):
            self._raise_verification(
                "Block emoji verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    @serialized_write
    async def remove_block_icon(self, block_uuid: str) -> MutationResult:
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        previous = await self._block(block_uuid)
        response, timed_out = await self._write(
            "logseq.DB.removeBlockIcon", [block_uuid]
        )
        current = await poll_readback(
            self._client,
            lambda: self._entity(block_uuid),
            lambda entity: ":logseq.property/icon" not in entity,
        )
        if ":logseq.property/icon" in current:
            self._raise_verification(
                "Block icon removal verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    async def _update_page_tag(
        self, page_uuid: str, tag_uuid: str, *, remove: bool
    ) -> MutationResult:
        page_uuid = self._validated_uuid(page_uuid)
        self._require_entity(page_uuid)
        tag_uuid = self._validated_uuid(tag_uuid)
        self._require_entity(tag_uuid)
        previous = await self._page(page_uuid)
        tag = await self._tag(tag_uuid)
        method = "logseq.DB.removeBlockTag" if remove else "logseq.DB.addBlockTag"
        response, timed_out = await self._write(method, [page_uuid, tag_uuid])
        current = await poll_readback(
            self._client,
            lambda: self._page(page_uuid),
            lambda entity: (
                tag["id"] not in self._reference_ids(entity.get("tags", []))
                if remove
                else tag["id"] in self._reference_ids(entity.get("tags", []))
            ),
        )
        present = tag["id"] in self._reference_ids(current.get("tags", []))
        if present == remove:
            action = "removal" if remove else "addition"
            self._raise_verification(
                f"Page tag {action} verification failed",
                response=response,
                previous_state=previous,
                observed_state=current,
                timed_out=timed_out,
            )
        return MutationResult(response, current, timed_out, previous_state=previous)

    async def _write(self, method: str, args: list[Any]) -> tuple[Any, bool]:
        try:
            return await self._client.call(method, args), False
        except httpx.TimeoutException:
            return None, True

    async def _block_property_state(
        self,
        block_uuid: str,
        property_ident: str,
        property_entity: dict[str, Any],
        expected: Any,
    ) -> tuple[dict[str, Any], bool]:
        block = await self._entity(block_uuid)
        query = (
            "[:find [?value ...] :where "
            f"[{block['id']} {property_ident} ?value]]"
        )
        raw_values = await self._client.call("logseq.DB.datascriptQuery", [query])
        if not isinstance(raw_values, list):
            return block, False
        actual = [
            await self._resolve_property_value(value, property_entity)
            for value in raw_values
        ]
        expected_values = list(expected) if isinstance(expected, (list, tuple, set)) else [expected]
        return block, actual == expected_values

    async def _tag_reference_ids(self, tag_id: int) -> set[int]:
        references: set[int] = set()
        for attribute in (":block/tags", ":block/refs"):
            query = (
                "[:find [?entity ...] :in $ ?tag :where "
                f"[?entity {attribute} ?tag]]"
            )
            result = await self._client.call(
                "logseq.DB.datascriptQuery", [query, tag_id]
            )
            if not isinstance(result, list):
                raise RuntimeError("Tag reference lookup returned an unexpected shape")
            references.update(value for value in result if isinstance(value, int))
        return references

    async def _property_removal_state(
        self, ident: str, property_id: int
    ) -> tuple[Any, tuple[list[Any], list[Any]]]:
        entity = await self._client.call("logseq.DB.getProperty", [ident])
        usage = await self._property_usage(ident, property_id)
        return entity, usage

    async def _property_usage(
        self, ident: str, property_id: int
    ) -> tuple[list[Any], list[Any]]:
        direct_query = f"[:find ?entity ?value :where [?entity {ident} ?value]]"
        value_query = (
            "[:find ?value :in $ ?property :where "
            "[?value :logseq.property/created-from-property ?property]]"
        )
        direct = await self._client.call("logseq.DB.datascriptQuery", [direct_query])
        values = await self._client.call(
            "logseq.DB.datascriptQuery", [value_query, property_id]
        )
        if not isinstance(direct, list) or not isinstance(values, list):
            raise RuntimeError("Property usage lookup returned an unexpected shape")
        return direct, values

    async def _resolve_property_value(
        self, value: Any, property_entity: dict[str, Any]
    ) -> Any:
        property_type = property_entity.get(":logseq.property/type", property_entity.get("type"))
        if isinstance(value, bool):
            return value
        if not isinstance(value, int) or property_type == "node":
            return value
        query = "[:find (pull ?entity [*]) . :in $ ?entity :where]"
        entity = await self._client.call("logseq.DB.datascriptQuery", [query, value])
        if not isinstance(entity, dict):
            return value
        for key in (":logseq.property/value", "value", "title", "content"):
            if key in entity:
                return entity[key]
        return value

    async def _property(self, ident: str) -> dict[str, Any]:
        ident = self._validated_ident(ident)
        entity = await self._client.call("logseq.DB.getProperty", [ident])
        if not self._has_ident(entity, ident):
            raise LookupError(f"No property exists with exact ident {ident}")
        return entity

    async def _tag(self, tag_uuid: str) -> dict[str, Any]:
        tag_uuid = self._validated_uuid(tag_uuid)
        entity = await self._client.call("logseq.DB.getTag", [tag_uuid])
        if not isinstance(entity, dict) or entity.get("uuid") != tag_uuid:
            raise LookupError(f"No tag exists with exact UUID {tag_uuid}")
        return entity

    async def _entity(self, entity_uuid: str) -> dict[str, Any]:
        query = (
            "[:find (pull ?entity [*]) . :where "
            f"[?entity :block/uuid #uuid \"{entity_uuid}\"]]"
        )
        entity = await self._client.call("logseq.DB.datascriptQuery", [query])
        if not isinstance(entity, dict) or entity.get("uuid") != entity_uuid:
            raise LookupError(f"No entity exists with exact UUID {entity_uuid}")
        return entity

    async def _block(self, block_uuid: str) -> dict[str, Any]:
        entity = await self._entity(block_uuid)
        if entity.get("name"):
            raise ValueError("UUID identifies a page, not a block")
        return entity

    async def _page(self, page_uuid: str) -> dict[str, Any]:
        entity = await self._entity(page_uuid)
        if not entity.get("name"):
            raise ValueError("UUID identifies a block, not a page")
        return entity

    @staticmethod
    def _normalized_emoji_id(icon_name: str) -> str:
        return re.sub(r"[\s-]+", "_", icon_name.strip().lower())

    @staticmethod
    def _raise_verification(
        diagnostic: str,
        *,
        response: Any,
        previous_state: Any,
        observed_state: Any,
        timed_out: bool = False,
    ) -> None:
        raise MutationVerificationError(
            MutationResult(
                response,
                None,
                timed_out,
                previous_state=previous_state,
                diagnostic=diagnostic,
                verified=False,
                observed_state=observed_state,
            )
        )

    @staticmethod
    def _validated_ident(value: str) -> str:
        if not isinstance(value, str) or not value.startswith(":") or "/" not in value:
            raise ValueError(
                "Expected an exact namespaced property ident such as :user.property/status"
            )
        return value

    @staticmethod
    def _validated_uuid(value: str) -> str:
        try:
            return str(UUID(value))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(f"Expected an exact UUID, got {value!r}") from error

    @staticmethod
    def _has_ident(entity: Any, expected_ident: str) -> bool:
        if not isinstance(entity, dict):
            return False
        return str(entity.get("db/ident", entity.get("ident"))) == expected_ident

    @staticmethod
    def _reference_ids(references: Any) -> set[int]:
        if not isinstance(references, list):
            return set()
        return {
            reference["id"]
            for reference in references
            if isinstance(reference, dict) and isinstance(reference.get("id"), int)
        }

    def _require_title(self, title: str) -> None:
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_title(title)

    def _require_property(self, ident: str) -> None:
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_property(ident)

    def _require_entity(self, entity_uuid: str) -> None:
        policy = getattr(self._client, "write_policy", None)
        if policy is not None:
            policy.require_entity(entity_uuid)
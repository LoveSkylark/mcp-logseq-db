"""Verified DB page and top-level block operations."""

import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

import httpx

from .client import LogseqDBClient, poll_readback, serialized_write


@dataclass(frozen=True)
class ContentResult:
    validation: Any
    response: Any
    verified_entities: tuple[dict[str, Any], ...]
    recovered_after_timeout: bool = False
    verified: bool = True
    diagnostic: str | None = None
    previous_entities: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerifiedContent:
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client

    async def get_block(self, block_uuid: str) -> dict[str, Any]:
        """Read one exact non-page block through Datascript."""
        block = await self._entity_by_uuid(block_uuid)
        if block.get("name"):
            raise ValueError("UUID identifies a page, not a block")
        return block

    async def find_block(self, block_uuid: str) -> dict[str, Any]:
        """Return an explicit found/block envelope for one exact block UUID."""
        block_uuid = self._validated_uuid(block_uuid)
        block = await self._optional_entity_by_uuid(block_uuid)
        if block is None:
            return {"found": False, "block_uuid": block_uuid, "block": None}
        if block.get("name"):
            raise ValueError("UUID identifies a page, not a block")
        return {"found": True, "block_uuid": block_uuid, "block": block}

    async def create_page(
        self, title: str, *, dry_run: bool = False
    ) -> ContentResult:
        """Create one page through the verified batch path."""
        return await self.upsert_nodes(
            [{
                "operation": "add",
                "entityType": "page",
                "data": {"title": title},
            }],
            dry_run=dry_run,
        )

    async def create_top_level_block(
        self,
        page_uuid: str,
        title: str,
        *,
        tag_uuids: list[str] | None = None,
        dry_run: bool = False,
    ) -> ContentResult:
        """Create one top-level block through the verified batch path."""
        data: dict[str, Any] = {"title": title, "page-id": page_uuid}
        if tag_uuids:
            data["tags"] = tag_uuids
        return await self.upsert_nodes(
            [{"operation": "add", "entityType": "block", "data": data}],
            dry_run=dry_run,
        )

    @serialized_write
    async def insert_block(
        self,
        target_uuid: str,
        title: str,
        *,
        placement: str = "child",
    ) -> ContentResult:
        """Experimentally insert a child or sibling with a predetermined UUID."""
        target_uuid = self._validated_uuid(target_uuid)
        self._require_entity(target_uuid)
        self._require_title(title)
        target = await self._entity_by_uuid(target_uuid)
        if not title.strip():
            raise ValueError("Block title must not be empty")
        if placement not in {"child", "before", "after"}:
            raise ValueError("placement must be child, before, or after")
        if placement != "child" and target.get("name"):
            raise ValueError("Cannot create a sibling of a page")

        block_uuid = str(uuid4())
        options: dict[str, Any] = {"customUUID": block_uuid}
        if placement == "child":
            options["sibling"] = False
        else:
            options.update({"sibling": True, "before": placement == "before"})

        response, timed_out = await self._call_ambiguous(
            "logseq.DB.insertBlock", [target_uuid, title, options]
        )
        block = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(block_uuid),
            lambda value: value is not None,
        )
        if block is None:
            return ContentResult(
                None,
                response,
                (),
                timed_out,
                False,
                "Insert was not observed at the predetermined UUID",
            )
        expected_parent_id = (
            target["id"] if placement == "child" else self._reference_id(target.get("parent"))
        )
        expected_page_id = (
            target["id"] if target.get("name") else self._reference_id(target.get("page"))
        )
        if (
            block.get("title") != title
            or self._reference_id(block.get("parent")) != expected_parent_id
            or self._reference_id(block.get("page")) != expected_page_id
        ):
            return ContentResult(
                None,
                response,
                (block,),
                timed_out,
                False,
                "Inserted block exists but its title, parent, or owning page is incorrect",
            )
        if placement in {"before", "after"}:
            self._verify_relative_order(block, target, placement)
        return ContentResult(None, response, (block,), timed_out)

    @serialized_write
    async def move_block(
        self,
        block_uuid: str,
        target_uuid: str,
        *,
        placement: str = "child",
    ) -> ContentResult:
        """Experimentally move a block beneath or before an exact target."""
        block_uuid = self._validated_uuid(block_uuid)
        target_uuid = self._validated_uuid(target_uuid)
        self._require_entity(block_uuid)
        self._require_entity(target_uuid)
        block = await self.get_block(block_uuid)
        subtree = await self._subtree(block)
        target = await self._entity_by_uuid(target_uuid)
        if block["id"] == target["id"]:
            raise ValueError("A block cannot be moved relative to itself")
        if placement not in {"child", "before"}:
            raise ValueError("placement must be child or before")
        if placement == "before" and target.get("name"):
            raise ValueError("Cannot move a block before a page")

        options = {"children": True} if placement == "child" else {"before": True}
        response, timed_out = await self._call_ambiguous(
            "logseq.DB.moveBlock", [block_uuid, target_uuid, options]
        )
        expected_parent_id = (
            target["id"] if placement == "child" else self._reference_id(target.get("parent"))
        )
        expected_page_id = (
            target["id"] if target.get("name") else self._reference_id(target.get("page"))
        )
        moved = await poll_readback(
            self._client,
            lambda: self.get_block(block_uuid),
            lambda value: (
                self._reference_id(value.get("parent")) == expected_parent_id
                and self._reference_id(value.get("page")) == expected_page_id
            ),
        )
        if (
            self._reference_id(moved.get("parent")) != expected_parent_id
            or self._reference_id(moved.get("page")) != expected_page_id
        ):
            return ContentResult(
                None,
                response,
                (moved,),
                timed_out,
                False,
                "Move was not observed with the requested parent and owning page",
            )
        if placement == "before":
            self._verify_relative_order(moved, target, placement)
        moved_subtree = await self._subtree(moved)
        before_by_uuid = {entity["uuid"]: entity for entity in subtree}
        after_by_uuid = {entity["uuid"]: entity for entity in moved_subtree}
        if before_by_uuid.keys() != after_by_uuid.keys():
            return ContentResult(
                None,
                response,
                tuple(moved_subtree),
                timed_out,
                False,
                "Move changed the subtree entity set",
            )
        for entity_uuid, previous in before_by_uuid.items():
            if entity_uuid == block_uuid:
                continue
            current = after_by_uuid[entity_uuid]
            if (
                self._reference_id(current.get("parent"))
                != self._reference_id(previous.get("parent"))
                or self._reference_id(current.get("page")) != expected_page_id
            ):
                return ContentResult(
                    None,
                    response,
                    tuple(moved_subtree),
                    timed_out,
                    False,
                    "Move did not preserve descendant parent/page relationships",
                )
        return ContentResult(None, response, tuple(moved_subtree), timed_out)

    @serialized_write
    async def delete_block(self, block_uuid: str) -> ContentResult:
        """Experimentally delete one exact block and verify its absence."""
        block_uuid = self._validated_uuid(block_uuid)
        self._require_entity(block_uuid)
        block = await self.get_block(block_uuid)
        subtree = await self._subtree(block)
        response, timed_out = await self._call_ambiguous(
            "logseq.DB.removeBlock", [block_uuid, {}]
        )
        current = await poll_readback(
            self._client,
            lambda: self._optional_entity_by_uuid(block_uuid),
            lambda value: value is None,
        )
        if current is not None:
            return ContentResult(
                None,
                response,
                (current,),
                timed_out,
                False,
                "Deletion was not observed; the block is still visible",
            )
        remaining = []
        for entity in subtree[1:]:
            descendant = await poll_readback(
                self._client,
                lambda entity_uuid=entity["uuid"]: self._optional_entity_by_uuid(
                    entity_uuid
                ),
                lambda value: value is None,
            )
            if descendant is not None:
                remaining.append(descendant)
        if remaining:
            return ContentResult(
                None,
                response,
                tuple(remaining),
                timed_out,
                False,
                "Target is absent but one or more descendants remain",
                tuple(subtree),
            )
        return ContentResult(
            None,
            response,
            (),
            timed_out,
            True,
            "Exact UUID is absent after deletion",
            tuple(subtree),
        )

    async def upsert_block(
        self,
        block_uuid: str,
        title: str,
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        """Edit one existing block title through the verified batch path."""
        operation = {
            "operation": "edit",
            "entityType": "block",
            "id": block_uuid,
            "data": {"title": title},
        }
        return await self.upsert_nodes([operation], dry_run=dry_run)

    @serialized_write
    async def upsert_nodes(
        self,
        operations: list[dict[str, Any]],
        *,
        dry_run: bool = False,
    ) -> ContentResult:
        normalized = await self._validate_operations(operations)
        validation = await self._client.call(
            "logseq.DB.upsertNodes", [normalized, {"dry-run": True}]
        )
        if dry_run:
            return ContentResult(validation, None, ())

        before_ids = {
            index: {entity["id"] for entity in await self._entities_by_title(op["data"]["title"])}
            for index, op in enumerate(normalized)
            if op["operation"] == "add"
        }
        duplicates = [
            normalized[index]["data"]["title"]
            for index, entity_ids in before_ids.items()
            if entity_ids
        ]
        if duplicates:
            raise ValueError(f"Cannot add entities with existing titles: {duplicates!r}")

        response: Any = None
        timed_out = False
        try:
            response = await self._client.call(
                "logseq.DB.upsertNodes", [normalized, {"dry-run": False}]
            )
        except httpx.TimeoutException:
            timed_out = True

        created_pages: dict[str, dict[str, Any]] = {}
        verified: list[dict[str, Any]] = []
        for index, operation in enumerate(normalized):
            if operation["operation"] == "edit":
                expected_title = operation["data"]["title"]
                entity = await poll_readback(
                    self._client,
                    lambda: self._entity_by_uuid(operation["id"]),
                    lambda value: value.get("title") == expected_title,
                )
                if entity.get("title") != operation["data"]["title"]:
                    raise RuntimeError("Block edit verification failed")
                await self._verify_title_uuid_refs(entity, operation["data"]["title"])
                verified.append(entity)
                continue

            matches = await poll_readback(
                self._client,
                lambda: self._entities_by_title(operation["data"]["title"]),
                lambda values: any(
                    entity["id"] not in before_ids[index] for entity in values
                ),
            )
            created = [entity for entity in matches if entity["id"] not in before_ids[index]]
            if len(created) != 1:
                raise RuntimeError(
                    f"Expected one new {operation['entityType']} named "
                    f"{operation['data']['title']!r}, found {len(created)}"
                )
            entity = created[0]
            if operation["entityType"] == "page":
                if not entity.get("name"):
                    raise RuntimeError("Page creation verification failed")
                if operation.get("id"):
                    created_pages[operation["id"]] = entity
            else:
                parent_ref = operation["data"]["page-id"]
                page = created_pages.get(parent_ref)
                if page is None:
                    page = await self._entity_by_uuid(parent_ref)
                parent_id = self._reference_id(entity.get("parent"))
                page_id = self._reference_id(entity.get("page"))
                if parent_id != page["id"] or page_id != page["id"]:
                    raise RuntimeError("Top-level block ownership verification failed")
                await self._verify_title_uuid_refs(entity, operation["data"]["title"])
            verified.append(entity)

        return ContentResult(validation, response, tuple(verified), timed_out)

    @serialized_write
    async def rename_page(self, page_uuid: str, new_title: str) -> ContentResult:
        page_uuid = self._validated_uuid(page_uuid)
        self._require_entity(page_uuid)
        self._require_title(new_title)
        if not new_title.strip():
            raise ValueError("New page title must not be empty")
        page = await self._page_by_uuid(page_uuid)
        if await self._entities_by_title(new_title):
            raise ValueError(f"An entity titled {new_title!r} already exists")
        response = await self._client.call("logseq.DB.renamePage", [page_uuid, new_title])
        current = await poll_readback(
            self._client,
            lambda: self._page_by_uuid(page_uuid),
            lambda value: value.get("title") == new_title,
        )
        if current.get("title") != new_title:
            raise RuntimeError("Page rename verification failed")
        return ContentResult(None, response, (current,))

    @serialized_write
    async def delete_page(self, page_uuid: str) -> ContentResult:
        page_uuid = self._validated_uuid(page_uuid)
        self._require_entity(page_uuid)
        page = await self._page_by_uuid(page_uuid)
        if page.get(":logseq.property/deleted-at") is not None:
            raise ValueError("Page is already recycled")
        response = await self._client.call("logseq.DB.deletePage", [page["title"]])
        current = await poll_readback(
            self._client,
            lambda: self._page_by_uuid(page_uuid),
            lambda value: value.get(":logseq.property/deleted-at") is not None,
        )
        if current.get(":logseq.property/deleted-at") is None:
            raise RuntimeError("Page recycle verification failed")
        return ContentResult(None, response, (current,))

    async def _validate_operations(
        self, operations: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
            raise ValueError("operations must contain between 1 and 100 items")
        temporary_pages: set[str] = set()
        add_titles: set[str] = set()
        normalized: list[dict[str, Any]] = []

        for index, operation in enumerate(operations):
            if not isinstance(operation, dict) or not isinstance(operation.get("data"), dict):
                raise ValueError(f"operation {index} must contain a data object")
            operation_type = operation.get("operation")
            entity_type = operation.get("entityType")
            data = dict(operation["data"])
            title = data.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"operation {index} requires a non-empty title")
            self._require_title(title)

            if operation_type == "add" and entity_type == "page":
                if set(data) != {"title"}:
                    raise ValueError("Page add supports only data.title")
                temporary_id = operation.get("id")
                if temporary_id is not None:
                    if not isinstance(temporary_id, str) or not temporary_id.strip():
                        raise ValueError("Temporary page id must be a non-empty string")
                    temporary_pages.add(temporary_id)
            elif operation_type == "add" and entity_type == "block":
                if not set(data) <= {"title", "page-id", "tags"}:
                    raise ValueError("Block add supports only title, page-id, and tags")
                parent = data.get("page-id")
                if not isinstance(parent, str) or not parent.strip():
                    raise ValueError("Top-level block add requires data.page-id")
                if parent not in temporary_pages:
                    self._require_entity(self._validated_uuid(parent))
                    page = await self._entity_by_uuid(parent)
                    if not page.get("name"):
                        raise ValueError("data.page-id must identify a page, not a block")
                for tag_uuid in data.get("tags", []):
                    self._require_entity(self._validated_uuid(tag_uuid))
            elif operation_type == "edit" and entity_type == "block":
                if set(data) != {"title"}:
                    raise ValueError("Block edit supports only data.title")
                block_uuid = self._validated_uuid(operation.get("id"))
                self._require_entity(block_uuid)
                block = await self._entity_by_uuid(block_uuid)
                if block.get("name"):
                    raise ValueError("Block edit id identifies a page")
            else:
                raise ValueError(
                    "Supported operations are add page, add top-level block, and edit block"
                )

            if operation_type == "add":
                if title in add_titles:
                    raise ValueError("Added entity titles must be unique within a batch")
                add_titles.add(title)
            normalized.append(dict(operation, data=data))
        return normalized

    async def _page_by_uuid(self, page_uuid: str) -> dict[str, Any]:
        page = await self._entity_by_uuid(page_uuid)
        if not page.get("name"):
            raise LookupError(f"No page exists with exact UUID {page_uuid}")
        return page

    async def _call_ambiguous(self, method: str, args: list[Any]) -> tuple[Any, bool]:
        try:
            return await self._client.call(method, args), False
        except httpx.TimeoutException:
            return None, True

    async def _optional_entity_by_uuid(self, entity_uuid: str) -> dict[str, Any] | None:
        entity_uuid = self._validated_uuid(entity_uuid)
        query = (
            "[:find (pull ?entity [*]) . :where "
            f"[?entity :block/uuid #uuid \"{entity_uuid}\"]]"
        )
        entity = await self._client.call("logseq.DB.datascriptQuery", [query])
        if entity is None:
            return None
        if not isinstance(entity, dict) or entity.get("uuid") != entity_uuid:
            raise RuntimeError("Entity lookup returned an unexpected result")
        return entity

    async def _entity_by_uuid(self, entity_uuid: str) -> dict[str, Any]:
        entity_uuid = self._validated_uuid(entity_uuid)
        entity = await self._optional_entity_by_uuid(entity_uuid)
        if entity is None:
            raise LookupError(f"No entity exists with exact UUID {entity_uuid}")
        return entity

    async def _entities_by_title(self, title: str) -> list[dict[str, Any]]:
        query = (
            "[:find [(pull ?entity [*]) ...] :where "
            f"[?entity :block/title {json.dumps(title)}]]"
        )
        entities = await self._client.call("logseq.DB.datascriptQuery", [query])
        if not isinstance(entities, list):
            raise RuntimeError("Title lookup returned an unexpected response shape")
        return [entity for entity in entities if isinstance(entity, dict)]

    async def _subtree(self, root: dict[str, Any]) -> list[dict[str, Any]]:
        """Read a bounded subtree using immediate-parent relationships."""
        result = [root]
        queue = [root]
        while queue:
            parent = queue.pop(0)
            query = (
                "[:find [(pull ?child [*]) ...] :in $ ?parent :where "
                "[?child :block/parent ?parent]]"
            )
            children = await self._client.call(
                "logseq.DB.datascriptQuery", [query, parent["id"]]
            )
            if not isinstance(children, list):
                raise RuntimeError("Child lookup returned an unexpected response shape")
            children = [child for child in children if isinstance(child, dict)]
            result.extend(children)
            queue.extend(children)
            if len(result) > 1000:
                raise RuntimeError("Subtree exceeds the 1000-node verification limit")
        return result

    @staticmethod
    def _reference_id(value: Any) -> int | None:
        return value.get("id") if isinstance(value, dict) else None

    @staticmethod
    def _verify_relative_order(
        block: dict[str, Any], target: dict[str, Any], placement: str
    ) -> None:
        block_order = block.get("order")
        target_order = target.get("order")
        if not isinstance(block_order, str) or not isinstance(target_order, str):
            raise RuntimeError("Block order verification failed")
        if placement == "before" and not block_order < target_order:
            raise RuntimeError("Block was not ordered before target")
        if placement == "after" and not block_order > target_order:
            raise RuntimeError("Block was not ordered after target")

    async def _verify_title_uuid_refs(
        self, entity: dict[str, Any], title: str
    ) -> None:
        referenced_uuids = {
            match.group(1).lower()
            for match in re.finditer(
                r"\[\[([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\]\]",
                title,
            )
        }
        if not referenced_uuids:
            return
        reference_uuids = {
            reference.get("uuid", "").lower()
            for reference in entity.get("refs", [])
            if isinstance(reference, dict) and reference.get("uuid")
        }
        reference_ids = {
            reference["id"]
            for reference in entity.get("refs", [])
            if isinstance(reference, dict) and isinstance(reference.get("id"), int)
        }
        query = "[:find ?uuid . :in $ ?entity :where [?entity :block/uuid ?uuid]]"
        for reference_id in reference_ids:
            value = await self._client.call(
                "logseq.DB.datascriptQuery", [query, reference_id]
            )
            if isinstance(value, str):
                reference_uuids.add(value.lower())
        if not referenced_uuids <= reference_uuids:
            raise RuntimeError("Block title UUID reference verification failed")

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
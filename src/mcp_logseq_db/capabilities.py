"""Runtime capability discovery for the connected Logseq DB instance."""

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from .client import LogseqDBClient, VERIFIED_WRITE_METHODS, WRITE_METHODS


@dataclass(frozen=True)
class DBCapabilities:
    db_version: str | None
    verified_against_db_version: str
    version_matches_manifest: bool
    supported_entity_types: tuple[str, ...]
    metadata_mutable_entity_types: tuple[str, ...]
    supported_content_operations: tuple[str, ...]
    supported_write_operations: tuple[str, ...]
    supported_removal_operations: tuple[str, ...]
    supported_query_features: tuple[str, ...]
    supported_read_operations: tuple[str, ...]
    candidate_write_operations: tuple[str, ...]
    unavailable_over_http: tuple[str, ...]
    rejected_operations: tuple[str, ...]
    experimental_operations: tuple[str, ...]
    experimental_writes_enabled: bool
    write_circuit_open: bool
    write_circuit_reason: str | None
    supported_mcp_write_tools: tuple[str, ...]
    experimental_mcp_write_tools: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityDiscovery:
    def __init__(self, client: LogseqDBClient) -> None:
        self._client = client

    async def discover(self) -> DBCapabilities:
        query = "[:find ?entity :where [?entity :block/uuid]]"
        methods = (
            ("properties", "logseq.DB.getAllProperties", []),
            ("tags", "logseq.DB.getAllTags", []),
            ("datascript", "logseq.DB.datascriptQuery", [query]),
        )
        try:
            async with asyncio.timeout(20):
                app_info, is_db_graph, *results = await asyncio.gather(
                    self._client.call("logseq.DB.getAppInfo", []),
                    self._client.call("logseq.DB.checkCurrentIsDbGraph", []),
                    *(self._probe(method, args) for _, method, args in methods),
                )
        except TimeoutError as error:
            raise RuntimeError(
                "Capability probes exceeded 20 seconds; the Logseq DB worker may be wedged"
            ) from error
        probed = {
            label: result for (label, _, _), result in zip(methods, results)
        }
        if not isinstance(app_info, dict) or app_info.get("supportDb") is not True:
            raise RuntimeError("Connected Logseq instance does not report DB support")
        if not is_db_graph:
            raise RuntimeError("The current Logseq graph is not a DB graph")
        db_version = (
            str(app_info.get("version"))
            if isinstance(app_info, dict) and app_info.get("version")
            else None
        )
        supported_reads = [
            method
            for label, method, _ in methods
            if probed[label] is not None
        ]
        query_features = [
            label for label, _, _ in methods[2:] if probed[label] is not None
        ]

        removals = tuple(
            sorted(
                method
                for method in VERIFIED_WRITE_METHODS
                if ".remove" in method or method.endswith("deletePage")
            )
        )

        return DBCapabilities(
            db_version=db_version,
            verified_against_db_version="2.0.1",
            version_matches_manifest=db_version == "2.0.1",
            supported_entity_types=("page", "block", "tag", "property"),
            metadata_mutable_entity_types=("page", "block", "tag", "property"),
            supported_content_operations=(
                "create-page",
                "create-top-level-block",
                "create-nested-block",
                "edit-block-title",
                "delete-block-subtree",
                "move-block-subtree",
                "rename-page",
                "recycle-page",
                "rename-tag",
                "delete-tag",
            ),
            supported_write_operations=tuple(sorted(VERIFIED_WRITE_METHODS)),
            supported_removal_operations=removals,
            supported_query_features=tuple(query_features),
            supported_read_operations=tuple(supported_reads),
            candidate_write_operations=tuple(
                sorted(WRITE_METHODS - VERIFIED_WRITE_METHODS)
            ),
            unavailable_over_http=(
                "logseq.DB.onChanged",
                "logseq.DB.onBlockChanged",
                "logseq.DB.getFavorites",
                "logseq.DB.setPropertyNodeTags",
            ),
            rejected_operations=(
                "logseq.DB.createPage",
                "logseq.DB.getBlock",
                "logseq.DB.getBlockProperties",
                "logseq.DB.getBlockProperty",
                "logseq.DB.getPageProperties",
                "logseq.DB.insertBatchBlock",
                "logseq.DB.prependBlockInPage",
                "logseq.DB.removeBlock",
                "logseq.DB.updateBlock",
            ),
            experimental_operations=(),
            experimental_writes_enabled=bool(
                getattr(self._client, "experimental_writes_enabled", False)
            ),
            write_circuit_open=bool(
                getattr(self._client, "write_circuit_open", False)
            ),
            write_circuit_reason=getattr(
                self._client, "write_circuit_reason", None
            ),
            supported_mcp_write_tools=(
                "upsert_nodes",
                "create_page",
                "create_top_level_block",
                "insert_block",
                "upsert_block",
                "delete_block",
                "move_block",
                "rename_page",
                "recycle_page",
                "upsert_property",
                "remove_property",
                "create_tag",
                "rename_tag",
                "delete_tag",
                "add_tag_property",
                "remove_tag_property",
                "set_tag_parent",
                "remove_tag_extends",
                "upsert_block_property",
                "remove_block_property",
                "upsert_page_property",
                "remove_page_property",
                "add_block_tag",
                "remove_block_tag",
                "add_page_tag",
                "remove_page_tag",
                "set_block_icon",
                "remove_block_icon",
            ),
            experimental_mcp_write_tools=(),
        )

    async def _probe(self, method: str, args: list[Any]) -> Any | None:
        try:
            return await self._client.call(method, args)
        except Exception:
            return None


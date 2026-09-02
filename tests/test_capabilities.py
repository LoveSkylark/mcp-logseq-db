from typing import Any

import pytest

from mcp_logseq_db.capabilities import CapabilityDiscovery


class ProbeClient:
    observed_methods: frozenset[str] = frozenset()

    async def call(self, method: str, args: list[Any]) -> Any:
        if method == "logseq.DB.getAppInfo":
            return {"version": "2.0.1", "supportDb": True}
        if method == "logseq.DB.checkCurrentIsDbGraph":
            return True
        return []


@pytest.mark.asyncio
async def test_fresh_capabilities_report_live_verified_writes() -> None:
    capabilities = await CapabilityDiscovery(ProbeClient()).discover()  # type: ignore[arg-type]

    assert capabilities.db_version == "2.0.1"
    assert capabilities.verified_against_db_version == "2.0.1"
    assert capabilities.version_matches_manifest is True
    assert capabilities.supported_entity_types == ("page", "block", "tag", "property")
    assert capabilities.metadata_mutable_entity_types == (
        "page",
        "block",
        "tag",
        "property",
    )
    assert capabilities.supported_content_operations == (
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
    )
    assert "logseq.DB.upsertProperty" in capabilities.supported_write_operations
    assert "logseq.DB.addBlockTag" in capabilities.supported_write_operations
    assert "logseq.DB.upsertNodes" in capabilities.supported_write_operations
    assert "logseq.DB.removeProperty" in capabilities.supported_removal_operations
    assert capabilities.supported_query_features == ("datascript",)
    assert capabilities.candidate_write_operations == (
        "logseq.DB.addPropertyValueChoices",
        "logseq.DB.setFileContent",
    )
    assert capabilities.unavailable_over_http == (
        "logseq.DB.onChanged",
        "logseq.DB.onBlockChanged",
        "logseq.DB.getFavorites",
        "logseq.DB.setPropertyNodeTags",
    )
    assert "logseq.DB.createPage" in capabilities.rejected_operations
    assert "logseq.DB.updateBlock" in capabilities.rejected_operations
    assert capabilities.experimental_operations == ()
    assert capabilities.experimental_writes_enabled is False
    assert capabilities.write_circuit_open is False
    assert capabilities.write_circuit_reason is None
    assert "rename_tag" in capabilities.supported_mcp_write_tools
    assert "delete_tag" in capabilities.supported_mcp_write_tools
    assert "delete_block" in capabilities.supported_mcp_write_tools
    assert "insert_block" in capabilities.supported_mcp_write_tools
    assert "move_block" in capabilities.supported_mcp_write_tools
    assert "add_page_tag" in capabilities.supported_mcp_write_tools
    assert "remove_page_tag" in capabilities.supported_mcp_write_tools
    assert capabilities.experimental_mcp_write_tools == ()


@pytest.mark.asyncio
async def test_capabilities_do_not_list_replaced_experimental_tools() -> None:
    client = ProbeClient()
    client.experimental_writes_enabled = True

    capabilities = await CapabilityDiscovery(client).discover()  # type: ignore[arg-type]

    assert capabilities.experimental_mcp_write_tools == ()


@pytest.mark.asyncio
async def test_capabilities_fail_when_logseq_is_unreachable() -> None:
    class UnreachableClient(ProbeClient):
        async def call(self, method: str, args: list[Any]) -> Any:
            raise RuntimeError("connection refused")

    with pytest.raises(RuntimeError, match="connection refused"):
        await CapabilityDiscovery(UnreachableClient()).discover()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_capabilities_fail_for_non_db_graph() -> None:
    class FileGraphClient(ProbeClient):
        async def call(self, method: str, args: list[Any]) -> Any:
            if method == "logseq.DB.checkCurrentIsDbGraph":
                return False
            return await super().call(method, args)

    with pytest.raises(RuntimeError, match="current Logseq graph is not a DB graph"):
        await CapabilityDiscovery(FileGraphClient()).discover()  # type: ignore[arg-type]

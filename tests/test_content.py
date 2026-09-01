from collections import deque
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from mcp_logseq_db.access import WriteAccessPolicy
from mcp_logseq_db.content import VerifiedContent


PAGE_UUID = "12345678-1234-5678-1234-567812345678"
BLOCK_UUID = "87654321-4321-8765-4321-876543218765"


class RecordingClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, list[Any]]] = []

    async def call(self, method: str, args: list[Any]) -> Any:
        self.calls.append((method, args))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_upsert_new_page_and_top_level_block_with_readback() -> None:
    client = RecordingClient([
        "validated",
        [],
        [],
        "committed",
        [{"id": 10, "uuid": PAGE_UUID, "title": "Page", "name": "page"}],
        [{"id": 11, "uuid": BLOCK_UUID, "title": "Block", "parent": {"id": 10}, "page": {"id": 10}}],
    ])
    operations = [
        {"operation": "add", "entityType": "page", "id": "temp-page", "data": {"title": "Page"}},
        {"operation": "add", "entityType": "block", "data": {"title": "Block", "page-id": "temp-page"}},
    ]

    result = await VerifiedContent(client).upsert_nodes(operations)  # type: ignore[arg-type]

    assert result.response == "committed"
    assert len(result.verified_entities) == 2
    assert client.calls[0][0] == "logseq.DB.upsertNodes"


@pytest.mark.asyncio
async def test_upsert_rejects_block_uuid_as_page_id() -> None:
    client = RecordingClient([
        {"id": 11, "uuid": BLOCK_UUID, "title": "Parent"},
    ])
    operations = [
        {"operation": "add", "entityType": "block", "data": {"title": "Child", "page-id": BLOCK_UUID}},
    ]

    with pytest.raises(ValueError, match="page, not a block"):
        await VerifiedContent(client).upsert_nodes(operations)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_upsert_dry_run_does_not_commit() -> None:
    client = RecordingClient(["validated"])

    result = await VerifiedContent(client).upsert_nodes(
        [{"operation": "add", "entityType": "page", "data": {"title": "Page"}}],
        dry_run=True,
    )  # type: ignore[arg-type]

    assert result.response is None
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_upsert_block_delegates_to_one_edit_operation() -> None:
    client = RecordingClient([
        {"id": 11, "uuid": BLOCK_UUID, "title": "Old"},
        "validated",
    ])

    result = await VerifiedContent(client).upsert_block(  # type: ignore[arg-type]
        BLOCK_UUID, "Updated", dry_run=True
    )

    assert result.validation == "validated"
    assert client.calls[1] == (
        "logseq.DB.upsertNodes",
        [[{
            "operation": "edit",
            "entityType": "block",
            "id": BLOCK_UUID,
            "data": {"title": "Updated"},
        }], {"dry-run": True}],
    )


@pytest.mark.asyncio
async def test_create_page_delegates_to_one_add_operation() -> None:
    client = RecordingClient(["validated"])

    await VerifiedContent(client).create_page("New page", dry_run=True)  # type: ignore[arg-type]

    assert client.calls == [("logseq.DB.upsertNodes", [[{
        "operation": "add",
        "entityType": "page",
        "data": {"title": "New page"},
    }], {"dry-run": True}])]


@pytest.mark.asyncio
async def test_create_top_level_block_delegates_with_tags() -> None:
    client = RecordingClient([
        {"id": 10, "uuid": PAGE_UUID, "title": "Page", "name": "page"},
        "validated",
    ])

    await VerifiedContent(client).create_top_level_block(  # type: ignore[arg-type]
        PAGE_UUID, "New block", tag_uuids=[BLOCK_UUID], dry_run=True
    )

    assert client.calls[1] == ("logseq.DB.upsertNodes", [[{
        "operation": "add",
        "entityType": "block",
        "data": {"title": "New block", "page-id": PAGE_UUID, "tags": [BLOCK_UUID]},
    }], {"dry-run": True}])


@pytest.mark.asyncio
async def test_get_block_rejects_page_uuid() -> None:
    client = RecordingClient([
        {"id": 10, "uuid": PAGE_UUID, "title": "Page", "name": "page"},
    ])

    with pytest.raises(ValueError, match="page, not a block"):
        await VerifiedContent(client).get_block(PAGE_UUID)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_find_block_returns_explicit_missing_result() -> None:
    client = RecordingClient([None])

    result = await VerifiedContent(client).find_block(BLOCK_UUID)  # type: ignore[arg-type]

    assert result == {"found": False, "block_uuid": BLOCK_UUID, "block": None}


@pytest.mark.asyncio
async def test_insert_timeout_is_accepted_only_after_exact_readback() -> None:
    client = RecordingClient([
        {"id": 10, "uuid": PAGE_UUID, "title": "Page", "name": "page"},
        httpx.ReadTimeout("ambiguous"),
        {"id": 11, "uuid": BLOCK_UUID, "title": "Child", "parent": {"id": 10}, "page": {"id": 10}},
    ])

    with patch("mcp_logseq_db.content.uuid4", return_value=BLOCK_UUID):
        result = await VerifiedContent(client).insert_block(  # type: ignore[arg-type]
            PAGE_UUID, "Child"
        )

    assert result.recovered_after_timeout is True
    assert result.verified is True
    assert result.verified_entities[0]["uuid"] == BLOCK_UUID
    assert client.calls[1] == (
        "logseq.DB.insertBlock",
        [PAGE_UUID, "Child", {"customUUID": BLOCK_UUID, "sibling": False}],
    )


@pytest.mark.asyncio
async def test_insert_timeout_without_observed_entity_returns_diagnostic() -> None:
    client = RecordingClient([
        {"id": 10, "uuid": PAGE_UUID, "title": "Page", "name": "page"},
        httpx.ReadTimeout("ambiguous"),
        None,
    ])

    with patch("mcp_logseq_db.content.uuid4", return_value=BLOCK_UUID):
        result = await VerifiedContent(client).insert_block(  # type: ignore[arg-type]
            PAGE_UUID, "Child"
        )

    assert result.verified is False
    assert result.recovered_after_timeout is True
    assert "not observed" in str(result.diagnostic)


@pytest.mark.asyncio
async def test_move_timeout_requires_parent_and_page_readback() -> None:
    client = RecordingClient([
        {"id": 11, "uuid": BLOCK_UUID, "title": "Block", "parent": {"id": 10}, "page": {"id": 10}},
        [],
        {"id": 12, "uuid": PAGE_UUID, "title": "Parent", "parent": {"id": 10}, "page": {"id": 10}},
        httpx.ReadTimeout("ambiguous"),
        {"id": 11, "uuid": BLOCK_UUID, "title": "Block", "parent": {"id": 12}, "page": {"id": 10}},
        [],
    ])

    result = await VerifiedContent(client).move_block(  # type: ignore[arg-type]
        BLOCK_UUID, PAGE_UUID
    )

    assert result.recovered_after_timeout is True
    assert result.verified_entities[0]["parent"] == {"id": 12}
    assert client.calls[3] == (
        "logseq.DB.moveBlock",
        [BLOCK_UUID, PAGE_UUID, {"children": True}],
    )


@pytest.mark.asyncio
async def test_move_preserves_descendant_relationships() -> None:
    child_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    root_before = {
        "id": 11,
        "uuid": BLOCK_UUID,
        "title": "Root",
        "parent": {"id": 10},
        "page": {"id": 10},
    }
    child_before = {
        "id": 13,
        "uuid": child_uuid,
        "title": "Child",
        "parent": {"id": 11},
        "page": {"id": 10},
    }
    target = {
        "id": 12,
        "uuid": PAGE_UUID,
        "title": "Target",
        "parent": {"id": 20},
        "page": {"id": 20},
    }
    root_after = dict(root_before, parent={"id": 12}, page={"id": 20})
    child_after = dict(child_before, page={"id": 20})
    client = RecordingClient([
        root_before,
        [child_before],
        [],
        target,
        None,
        root_after,
        [child_after],
        [],
    ])

    result = await VerifiedContent(client).move_block(  # type: ignore[arg-type]
        BLOCK_UUID, PAGE_UUID
    )

    assert result.verified is True
    assert {entity["uuid"] for entity in result.verified_entities} == {
        BLOCK_UUID,
        child_uuid,
    }


@pytest.mark.asyncio
async def test_delete_timeout_requires_exact_absence() -> None:
    client = RecordingClient([
        {"id": 11, "uuid": BLOCK_UUID, "title": "Block", "parent": {"id": 10}, "page": {"id": 10}},
        [],
        httpx.ReadTimeout("ambiguous"),
        None,
    ])

    result = await VerifiedContent(client).delete_block(BLOCK_UUID)  # type: ignore[arg-type]

    assert result.recovered_after_timeout is True
    assert result.verified is True
    assert result.verified_entities == ()
    assert result.previous_entities[0]["uuid"] == BLOCK_UUID
    assert result.diagnostic == "Exact UUID is absent after deletion"
    assert client.calls[2] == (
        "logseq.DB.removeBlock",
        [BLOCK_UUID, {}],
    )


@pytest.mark.asyncio
async def test_delete_verifies_descendant_cascade() -> None:
    child_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    root = {
        "id": 11,
        "uuid": BLOCK_UUID,
        "title": "Parent",
        "parent": {"id": 10},
        "page": {"id": 10},
    }
    child = {
        "id": 12,
        "uuid": child_uuid,
        "title": "Child",
        "parent": {"id": 11},
        "page": {"id": 10},
    }
    client = RecordingClient([
        root,
        [child],
        [],
        None,
        None,
        None,
    ])

    result = await VerifiedContent(client).delete_block(BLOCK_UUID)  # type: ignore[arg-type]

    assert result.verified is True
    assert result.verified_entities == ()
    assert {entity["uuid"] for entity in result.previous_entities} == {
        BLOCK_UUID,
        child_uuid,
    }


@pytest.mark.asyncio
async def test_title_scope_denies_creation_before_api_call() -> None:
    client = RecordingClient([])
    client.write_policy = WriteAccessPolicy(title_prefixes=("Allowed/",))

    with pytest.raises(PermissionError, match="title"):
        await VerifiedContent(client).create_page("Denied page", dry_run=True)  # type: ignore[arg-type]

    assert client.calls == []


@pytest.mark.asyncio
async def test_existing_title_stops_add_before_commit() -> None:
    client = RecordingClient([
        "validated",
        [{"id": 10, "uuid": PAGE_UUID, "title": "Existing", "name": "existing"}],
    ])

    with pytest.raises(ValueError, match="existing titles"):
        await VerifiedContent(client).create_page("Existing")  # type: ignore[arg-type]

    assert all(
        call != ("logseq.DB.upsertNodes", [[{
            "operation": "add",
            "entityType": "page",
            "data": {"title": "Existing"},
        }], {"dry-run": False}])
        for call in client.calls
    )


@pytest.mark.asyncio
async def test_uuid_title_reference_requires_matching_structural_ref() -> None:
    client = RecordingClient([])
    await VerifiedContent(client)._verify_title_uuid_refs(  # type: ignore[arg-type]
        {"refs": [{"uuid": PAGE_UUID}]}, f"See [[{PAGE_UUID}]]"
    )

    with pytest.raises(RuntimeError, match="UUID reference"):
        await VerifiedContent(client)._verify_title_uuid_refs(  # type: ignore[arg-type]
            {"refs": []}, f"See [[{PAGE_UUID}]]"
        )


@pytest.mark.asyncio
async def test_title_reference_does_not_claim_structural_verification() -> None:
    await VerifiedContent(RecordingClient([]))._verify_title_uuid_refs(  # type: ignore[arg-type]
        {"refs": []}, "See [[Page title]]"
    )


@pytest.mark.asyncio
async def test_uuid_title_reference_resolves_compact_ref_id() -> None:
    client = RecordingClient([PAGE_UUID])

    await VerifiedContent(client)._verify_title_uuid_refs(  # type: ignore[arg-type]
        {"refs": [{"id": 42}]}, f"See [[{PAGE_UUID}]]"
    )

    assert client.calls[0][0] == "logseq.DB.datascriptQuery"
import pytest

from mcp_logseq_db.access import WriteAccessPolicy


def test_unconfigured_policy_allows_writes() -> None:
    policy = WriteAccessPolicy()

    policy.require_title("Any title")
    policy.require_property(":any/property")
    policy.require_entity("any-uuid")


def test_configured_policy_denies_out_of_scope_values() -> None:
    policy = WriteAccessPolicy(
        title_prefixes=("Work/",),
        property_prefixes=(":user.property/work",),
        entity_uuids=frozenset({"allowed"}),
    )

    with pytest.raises(PermissionError, match="title"):
        policy.require_title("Personal")
    with pytest.raises(PermissionError, match="property"):
        policy.require_property(":user.property/private")
    with pytest.raises(PermissionError, match="entity UUID"):
        policy.require_entity("denied")
"""
Boundary validation: identifiers, write scopes, and environment settings.

These are the guards that turn this API's silent no-ops into loud errors, so
the tests care as much about the MESSAGE as the rejection -- a caller who
passes a title where a UUID belongs needs to be told which it was.
"""

import pytest

from mcp_logseq_db.access import WriteAccessPolicy
from mcp_logseq_db.identifiers import IdentifierError, require_uuid
from mcp_logseq_db.settings import Settings

UUID = "6a9a1a1c-cede-430f-8768-7a3609d4039b"


# ------------------------------------------------------------ identifiers

@pytest.mark.parametrize(
    "value",
    [
        UUID,
        UUID.upper(),
        f"{{{UUID}}}",
        UUID.replace("-", ""),
        f"urn:uuid:{UUID}",
        f"  {UUID}  ",
    ],
)
def test_cosmetic_variations_are_normalised(value: str) -> None:
    """Every form uuid.UUID() accepted stays accepted; rejecting these would
    be a regression, not extra safety."""
    assert require_uuid(value, role="tag_uuid") == UUID


@pytest.mark.parametrize(
    ("value", "expected_phrase"),
    [
        ("$TAG-UUID", "placeholder"),
        ("{{tagUuid}}", "placeholder"),
        (":user.class/xzy-bc0auNqC", ":db/ident"),
        ("859", ":db/id"),
        ("TAG-TEST", "title or name"),
    ],
)
def test_wrong_kind_of_value_is_diagnosed(value: str, expected_phrase: str) -> None:
    """Naming what was passed matters more than rejecting it: the caller
    almost always has the right value one lookup away."""
    with pytest.raises(IdentifierError, match=expected_phrase):
        require_uuid(value, role="tag_uuid")


def test_role_and_hint_appear_in_the_message() -> None:
    with pytest.raises(IdentifierError) as caught:
        require_uuid("TAG-TEST", role="tag_uuid", hint="getTagUUID")
    message = str(caught.value)
    assert "tag_uuid" in message
    assert "getTagUUID" in message


@pytest.mark.parametrize("value", ["", "   ", None, 859, [UUID]])
def test_empty_and_non_string_values_are_rejected(value: object) -> None:
    with pytest.raises(IdentifierError):
        require_uuid(value, role="tag_uuid")


# ---------------------------------------------------------- write scopes

def test_empty_policy_permits_everything() -> None:
    """An unset scope means 'no restriction', not 'deny all'."""
    policy = WriteAccessPolicy()
    policy.require_title("anything")
    policy.require_property(":user.property/anything")
    policy.require_entity(UUID)


@pytest.mark.parametrize(
    "ident",
    [":plugin.property._test_plugin/Effort", "plugin.property._test_plugin/Effort"],
)
def test_property_prefix_matches_with_or_without_the_leading_colon(ident: str) -> None:
    """Idents arrive from the API colon-prefixed but are configured without
    one. Comparing the raw forms would deny every write."""
    WriteAccessPolicy(
        property_prefixes=("plugin.property._test_plugin/",)
    ).require_property(ident)


@pytest.mark.parametrize(
    "ident",
    [":user.property/fun-W8dp1CaI", ":logseq.property/status",
     ":plugin.property.other_plugin/Effort"],
)
def test_property_outside_the_prefix_is_denied(ident: str) -> None:
    with pytest.raises(PermissionError, match="outside the writable prefixes"):
        WriteAccessPolicy(
            property_prefixes=("plugin.property._test_plugin/",)
        ).require_property(ident)


def test_title_and_entity_scopes_deny_out_of_scope_values() -> None:
    policy = WriteAccessPolicy(
        title_prefixes=("MCP ",), entity_uuids=frozenset({UUID}))

    policy.require_title("MCP Notes")
    policy.require_entity(UUID)

    with pytest.raises(PermissionError):
        policy.require_title("Other Notes")
    with pytest.raises(PermissionError):
        policy.require_entity("6a993c3f-a787-4ee4-9f5d-35c271fb7c0c")


# -------------------------------------------------------------- settings

@pytest.fixture
def env(monkeypatch):
    """Isolate the environment; only what a test sets is visible."""
    for key in list(__import__("os").environ):
        if key.startswith("LOGSEQ_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "token")
    return monkeypatch


def test_token_is_required(env) -> None:
    env.delenv("LOGSEQ_API_TOKEN")
    with pytest.raises(RuntimeError, match="LOGSEQ_API_TOKEN is required"):
        Settings.from_env()


def test_defaults_are_permissive_but_sandboxed(env) -> None:
    settings = Settings.from_env()

    assert settings.api_url == "http://127.0.0.1:12315"
    assert settings.probe_writes is True
    assert settings.plugin_id is None
    # Without a plugin id the guard knows only the namespace family, which is
    # wide enough to admit another plugin's properties.
    assert settings.writable_property_prefix == "plugin.property."
    assert settings.property_prefixes == ("plugin.property.",)


def test_plugin_id_narrows_the_writable_prefix(env) -> None:
    env.setenv("LOGSEQ_PLUGIN_ID", "_test_plugin")
    settings = Settings.from_env()

    assert settings.writable_property_prefix == "plugin.property._test_plugin/"
    assert settings.property_prefixes == ("plugin.property._test_plugin/",)


@pytest.mark.parametrize(
    "value",
    ["_test_plugin", "plugin.property._test_plugin",
     ":plugin.property._test_plugin/Effort"],
)
def test_plugin_id_accepts_bare_and_full_ident_forms(env, value: str) -> None:
    """Pasting a full ident out of a query result is the obvious mistake, so
    it is accepted rather than rejected."""
    env.setenv("LOGSEQ_PLUGIN_ID", value)
    assert Settings.from_env().plugin_id == "_test_plugin"


@pytest.mark.parametrize("value", ["has space", "bad/slash!", "-leading-dash"])
def test_malformed_plugin_id_is_rejected(env, value: str) -> None:
    env.setenv("LOGSEQ_PLUGIN_ID", value)
    with pytest.raises(RuntimeError, match="must be a plugin id"):
        Settings.from_env()


def test_explicit_property_prefixes_override_the_default(env) -> None:
    env.setenv("LOGSEQ_WRITE_PROPERTY_PREFIXES",
               "plugin.property._test_plugin/Effort")
    assert Settings.from_env().property_prefixes == (
        "plugin.property._test_plugin/Effort",)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("0", False), ("no", False), ("off", False),
     ("FALSE", False), ("true", True), ("anything else", True)],
)
def test_flags_treat_only_known_negatives_as_false(env, value, expected) -> None:
    env.setenv("LOGSEQ_PROBE_WRITES", value)
    assert Settings.from_env().probe_writes is expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LOGSEQ_API_CONNECT_TIMEOUT", "0"),
        ("LOGSEQ_API_CONNECT_TIMEOUT", "-1"),
        ("LOGSEQ_API_CONNECT_TIMEOUT", "nan"),
        ("LOGSEQ_API_READ_TIMEOUT", "not-a-number"),
        ("LOGSEQ_READBACK_ATTEMPTS", "0"),
        ("LOGSEQ_MAX_RESPONSE_BYTES", "-5"),
        ("LOGSEQ_API_URL", "ftp://localhost/api"),
    ],
)
def test_invalid_numeric_and_url_settings_are_rejected(env, name, value) -> None:
    env.setenv(name, value)
    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_csv_settings_ignore_blanks_and_whitespace(env) -> None:
    env.setenv("LOGSEQ_WRITE_TITLE_PREFIXES", " MCP , , Lab ,")
    assert Settings.from_env().write_title_prefixes == ("MCP", "Lab")
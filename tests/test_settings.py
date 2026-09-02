import pytest

from mcp_logseq_db.settings import Settings


def test_settings_keep_connect_and_read_timeouts_independent(monkeypatch) -> None:
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")
    monkeypatch.setenv("LOGSEQ_API_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("LOGSEQ_API_READ_TIMEOUT", "17")

    settings = Settings.from_env()

    assert settings.connect_timeout == 2.5
    assert settings.read_timeout == 17


def test_settings_require_token(monkeypatch) -> None:
    monkeypatch.delenv("LOGSEQ_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="LOGSEQ_API_TOKEN is required"):
        Settings.from_env()


@pytest.mark.parametrize("value", ["not-a-url", "ftp://example.com", "http://"])
def test_settings_reject_invalid_api_url(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")
    monkeypatch.setenv("LOGSEQ_API_URL", value)

    with pytest.raises(RuntimeError, match="LOGSEQ_API_URL"):
        Settings.from_env()


def test_settings_parse_reliability_and_write_scope(monkeypatch) -> None:
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")
    monkeypatch.setenv("LOGSEQ_READ_ATTEMPTS", "2")
    monkeypatch.setenv("LOGSEQ_READBACK_ATTEMPTS", "4")
    monkeypatch.setenv("LOGSEQ_READBACK_DELAY", "0.25")
    monkeypatch.setenv("LOGSEQ_WRITE_TITLE_PREFIXES", "Work/, Lab/")
    monkeypatch.setenv("LOGSEQ_WRITE_PROPERTY_PREFIXES", ":user.property/work")
    monkeypatch.setenv("LOGSEQ_WRITE_ENTITY_UUIDS", "one,two")

    settings = Settings.from_env()

    assert settings.read_attempts == 2
    assert settings.readback_attempts == 4
    assert settings.readback_delay == 0.25
    assert settings.write_title_prefixes == ("Work/", "Lab/")
    assert settings.write_property_prefixes == (":user.property/work",)
    assert settings.write_entity_uuids == frozenset({"one", "two"})


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_settings_reject_non_finite_timeouts(monkeypatch, value: str) -> None:
    monkeypatch.setenv("LOGSEQ_API_TOKEN", "test-token")
    monkeypatch.setenv("LOGSEQ_API_READ_TIMEOUT", value)

    with pytest.raises(RuntimeError, match="must be a positive number"):
        Settings.from_env()

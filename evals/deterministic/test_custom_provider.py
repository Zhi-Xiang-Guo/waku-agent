"""Custom API provider behavior, without network calls or real credentials."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import anthropic

from waku import integrations
from waku.config import Settings
from waku.loop import models
from waku.ops import settings_api

CUSTOM_ENV = (
    "WAKU_CUSTOM_API_KEY",
    "WAKU_CUSTOM_BASE_URL",
    "WAKU_CUSTOM_PROTOCOL",
    "WAKU_CUSTOM_MODEL",
    "WAKU_CUSTOM_SMALL_MODEL",
)


def _isolate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WAKU_HOME", str(tmp_path / ".waku"))
    for name in (*CUSTOM_ENV, "WAKU_PROVIDER", "WAKU_MODEL", "WAKU_SMALL_MODEL"):
        monkeypatch.setenv(name, "")


def test_custom_provider_exposes_the_fields_needed_by_compatible_apis(monkeypatch, tmp_path):
    """Removing any field makes the dashboard unable to configure that part of the API."""
    _isolate(monkeypatch, tmp_path)

    view = next(item for item in integrations.list_providers() if item.key == "custom")
    fields = {field.name: field for field in view.fields}

    assert view.name == "Custom API"
    assert fields["WAKU_CUSTOM_API_KEY"].secret is True
    assert fields["WAKU_CUSTOM_PROTOCOL"].options == ("openai", "anthropic")
    assert fields["WAKU_CUSTOM_BASE_URL"].required is True
    assert fields["WAKU_CUSTOM_BASE_URL"].kind is integrations.FieldKind.TEXT
    assert fields["WAKU_CUSTOM_MODEL"].required is True
    assert fields["WAKU_CUSTOM_SMALL_MODEL"].required is False


def test_applying_custom_provider_persists_scoped_configuration(monkeypatch, tmp_path):
    """A custom endpoint must survive restart without replacing built-in provider settings."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(integrations, "_provider_probe", lambda values: None)

    result = integrations.apply_provider(
        "custom",
        key="test-key",
        protocol="openai",
        base_url="https://proxy.example/v1/",
        model="acme-main",
        small_model="",
    )

    assert result.ok, result.error
    assert os.environ["WAKU_PROVIDER"] == "custom"
    assert os.environ["WAKU_CUSTOM_API_KEY"] == "test-key"
    assert os.environ["WAKU_CUSTOM_PROTOCOL"] == "openai"
    assert os.environ["WAKU_CUSTOM_BASE_URL"] == "https://proxy.example/v1"
    assert os.environ["WAKU_CUSTOM_MODEL"] == "acme-main"
    assert os.environ["WAKU_CUSTOM_SMALL_MODEL"] == "acme-main"
    assert os.environ["WAKU_MODEL"] == "acme-main"
    assert os.environ["WAKU_SMALL_MODEL"] == "acme-main"
    contents = (tmp_path / ".env").read_text(encoding="utf-8")
    for name in CUSTOM_ENV:
        assert f"{name}=" in contents


def test_editing_active_custom_provider_updates_current_models(monkeypatch, tmp_path):
    """Saving the active card must rebuild against the newly selected model pair."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_PROVIDER", "custom")
    monkeypatch.setenv("WAKU_MODEL", "acme-old")
    monkeypatch.setenv("WAKU_SMALL_MODEL", "acme-old-gate")
    monkeypatch.setenv("WAKU_CUSTOM_API_KEY", "test-key")
    monkeypatch.setenv("WAKU_CUSTOM_PROTOCOL", "openai")
    monkeypatch.setenv("WAKU_CUSTOM_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("WAKU_CUSTOM_MODEL", "acme-old")
    monkeypatch.setenv("WAKU_CUSTOM_SMALL_MODEL", "acme-old-gate")

    result = integrations.apply_provider(
        "custom", model="acme-new", small_model="", activate=False
    )

    assert result.ok, result.error
    assert os.environ["WAKU_CUSTOM_MODEL"] == "acme-new"
    assert os.environ["WAKU_CUSTOM_SMALL_MODEL"] == "acme-new"
    assert os.environ["WAKU_MODEL"] == "acme-new"
    assert os.environ["WAKU_SMALL_MODEL"] == "acme-new"


def test_custom_gate_defaults_to_main_for_direct_env_configuration(monkeypatch, tmp_path):
    """A hand-edited .env gets the same optional gate behavior as the Models UI."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_PROVIDER", "custom")
    monkeypatch.setenv("WAKU_CUSTOM_MODEL", "acme-main")

    provider = models.PROVIDERS["custom"]

    assert provider.configured_small_model() == "acme-main"


def test_settings_info_reports_custom_scoped_models(monkeypatch, tmp_path):
    """The nav and Models page show the effective scoped pair after a restart."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_PROVIDER", "custom")
    monkeypatch.setenv("WAKU_CUSTOM_MODEL", "acme-main")

    info = settings_api.settings_info()

    assert info["model"] == "acme-main"
    assert info["small_model"] == "acme-main"


def test_provider_modal_offers_force_save_after_probe_failure():
    """Compatible chat endpoints without GET /models remain configurable."""
    source = Path("waku/ops/static/js/models.js").read_text(encoding="utf-8")

    assert "r.can_force" in source
    assert "pm-force-save" in source
    assert "Save anyway" in source


def test_custom_provider_can_force_save_without_a_models_endpoint(monkeypatch, tmp_path):
    """Chat-compatible proxies may intentionally omit the optional model catalog."""
    _isolate(monkeypatch, tmp_path)

    def unavailable(_values):
        raise RuntimeError("model catalog unavailable")

    monkeypatch.setattr(integrations, "_provider_probe", unavailable)
    values = {
        "provider": "custom",
        "key": "test-key",
        "protocol": "openai",
        "base_url": "https://proxy.example/v1",
        "model": "acme-main",
    }

    failed = integrations.apply_provider(**values)
    saved = integrations.apply_provider(**values, force=True)

    assert not failed.ok
    assert failed.can_force
    assert saved.ok
    assert os.environ["WAKU_CUSTOM_MODEL"] == "acme-main"


def test_custom_provider_rejects_an_invalid_protocol_before_writing(monkeypatch, tmp_path):
    """An unsupported wire format must not create a configuration the loop cannot use."""
    _isolate(monkeypatch, tmp_path)

    result = integrations.apply_provider(
        "custom",
        key="test-key",
        protocol="responses",
        base_url="https://proxy.example/v1",
        model="acme-main",
    )

    assert not result.ok
    assert "protocol" in result.error.lower()
    assert not (tmp_path / ".env").exists()


def test_custom_provider_rejects_an_invalid_base_url_before_writing(monkeypatch, tmp_path):
    """A malformed endpoint must fail before it can break every later model call."""
    _isolate(monkeypatch, tmp_path)

    result = integrations.apply_provider(
        "custom",
        key="test-key",
        protocol="openai",
        base_url="proxy.example/v1",
        model="acme-main",
    )

    assert not result.ok
    assert "base url" in result.error.lower()
    assert not (tmp_path / ".env").exists()


def test_custom_provider_requires_a_key_before_activation(monkeypatch, tmp_path):
    """The card must not report success for a provider that cannot authenticate."""
    _isolate(monkeypatch, tmp_path)

    result = integrations.apply_provider(
        "custom",
        protocol="openai",
        base_url="https://proxy.example/v1",
        model="acme-main",
    )

    assert not result.ok
    assert "api key" in result.error.lower()
    assert not (tmp_path / ".env").exists()


def test_custom_openai_provider_builds_client_from_scoped_settings(monkeypatch, tmp_path):
    """Changing the protocol env must select a different real adapter path."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_CUSTOM_API_KEY", "test-key")
    monkeypatch.setenv("WAKU_CUSTOM_PROTOCOL", "openai")
    monkeypatch.setenv("WAKU_CUSTOM_BASE_URL", "https://proxy.example/v1")
    captured = {}

    class StubOpenAICompatClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)

    monkeypatch.setattr(models, "OpenAICompatClient", StubOpenAICompatClient)
    settings = Settings(
        provider="custom", model="acme-main", small_model="acme-gate", home=tmp_path
    )

    client = models.get_client(settings)

    assert isinstance(client, StubOpenAICompatClient)
    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://proxy.example/v1"


def test_custom_anthropic_provider_builds_client_from_scoped_settings(monkeypatch, tmp_path):
    """The same card can target an Anthropic Messages-compatible proxy."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("WAKU_CUSTOM_API_KEY", "test-key")
    monkeypatch.setenv("WAKU_CUSTOM_PROTOCOL", "anthropic")
    monkeypatch.setenv("WAKU_CUSTOM_BASE_URL", "https://proxy.example/anthropic")
    captured = {}

    def fake_anthropic(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)
    settings = Settings(
        provider="custom", model="acme-main", small_model="acme-gate", home=tmp_path
    )

    client = models.get_client(settings)

    assert isinstance(client, SimpleNamespace)
    assert captured["api_key"] == "test-key"
    assert captured["base_url"] == "https://proxy.example/anthropic"

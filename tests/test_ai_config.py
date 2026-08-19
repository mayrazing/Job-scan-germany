from __future__ import annotations

import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_scan.ai_config import (
    AiConfigError,
    AiProviderDraft,
    AiProviderNotFound,
    AiProviderStore,
)


def provider(
    name: str,
    *,
    api_key: str | None = "sk-test",
    model: str = "deepseek-v4-flash",
    base_url: str = "https://api.example.com/anthropic",
) -> AiProviderDraft:
    return AiProviderDraft(
        display_name=name,
        base_url=base_url,
        api_key=api_key,
        model=model,
        reasoning_effort="low",
    )


def test_store_masks_key_and_persists_private_file(tmp_path: Path) -> None:
    path = tmp_path / "ai-config.toml"
    store = AiProviderStore(path)

    created = store.create(provider("DeepSeek"))

    assert created.model_dump() == {
        "id": "deepseek",
        "display_name": "DeepSeek",
        "base_url": "https://api.example.com/anthropic",
        "model": "deepseek-v4-flash",
        "reasoning_effort": "low",
        "api_key_configured": True,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert AiProviderStore(path).require("deepseek").api_key == "sk-test"


def test_update_without_key_preserves_saved_secret(tmp_path: Path) -> None:
    store = AiProviderStore(tmp_path / "ai-config.toml")
    store.create(provider("DeepSeek"))

    updated = store.update(
        "deepseek",
        provider("DeepSeek EU", api_key=None, model="deepseek-chat"),
    )

    assert updated.display_name == "DeepSeek EU"
    assert updated.model == "deepseek-chat"
    assert store.require("deepseek").api_key == "sk-test"


def test_delete_removes_saved_provider(tmp_path: Path) -> None:
    store = AiProviderStore(tmp_path / "ai-config.toml")
    store.create(provider("DeepSeek"))
    store.create(provider("Open Router"))

    store.delete("deepseek")

    assert [saved.id for saved in store.list()] == ["open-router"]
    with pytest.raises(AiProviderNotFound):
        store.require("deepseek")


def test_update_changed_url_requires_replacement_key_and_keeps_original(
    tmp_path: Path,
) -> None:
    store = AiProviderStore(tmp_path / "ai-config.toml")
    original = store.create(provider("DeepSeek"))

    with pytest.raises(AiConfigError):
        store.update(
            original.id,
            provider(
                "DeepSeek",
                api_key=None,
                base_url="https://other.example.com/anthropic",
            ),
        )

    saved = store.require(original.id)
    assert saved.base_url == "https://api.example.com/anthropic"
    assert saved.api_key == "sk-test"


@pytest.mark.parametrize(
    "base_url",
    ["https://api.example.com:0/anthropic", "https://api.example.com:99999/anthropic"],
)
def test_provider_rejects_invalid_ports(base_url: str) -> None:
    with pytest.raises(ValidationError):
        provider("DeepSeek", base_url=base_url)


@pytest.mark.parametrize("api_key", ["sk-\u2603", "sk-line\nbreak", "sk-tab\tkey"])
def test_provider_rejects_keys_that_are_not_safe_http_headers(api_key: str) -> None:
    with pytest.raises(ValidationError):
        provider("DeepSeek", api_key=api_key)


def test_store_ignores_legacy_active_flag_when_reading(tmp_path: Path) -> None:
    path = tmp_path / "ai-config.toml"
    path.write_text(
        """\
[[providers]]
id = "deepseek"
display_name = "DeepSeek"
base_url = "https://api.example.com/anthropic"
api_key = "sk-test"
model = "deepseek-chat"
reasoning_effort = "low"
active = true
""",
        encoding="utf-8",
    )

    saved = AiProviderStore(path).list()[0]

    assert saved.model_dump() == {
        "id": "deepseek",
        "display_name": "DeepSeek",
        "base_url": "https://api.example.com/anthropic",
        "model": "deepseek-chat",
        "reasoning_effort": "low",
        "api_key_configured": True,
    }


def test_store_drops_legacy_active_flag_when_resaving(tmp_path: Path) -> None:
    path = tmp_path / "ai-config.toml"
    path.write_text(
        """\
[[providers]]
id = "deepseek"
display_name = "DeepSeek"
base_url = "https://api.example.com/anthropic"
api_key = "sk-test"
model = "deepseek-chat"
reasoning_effort = "low"
active = true
""",
        encoding="utf-8",
    )
    store = AiProviderStore(path)

    store.update("deepseek", provider("DeepSeek", api_key=None))

    assert "active" not in path.read_text(encoding="utf-8")

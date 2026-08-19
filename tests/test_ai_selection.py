from __future__ import annotations

import stat
from pathlib import Path

from job_scan.ai_config import AiProviderDraft, AiProviderStore
from job_scan.ai_selection import (
    AiRuntimeSelection,
    AiSelectionStore,
    ClaudeRuntimeSelection,
    ai_selection_from_config,
)
from job_scan.config import AppConfig, ClaudeSettings, SchedulerSettings
from job_scan.paths import AppPaths


def config(paths: AppPaths, *, ai_runtime: str = "claude-code") -> AppConfig:
    return AppConfig(
        candidate_name="Ada",
        ai_runtime=ai_runtime,
        resume_path=paths.root / "resume.pdf",
        resume_sha256="sha256:" + "a" * 64,
        profile_sha256="sha256:" + "b" * 64,
        search_terms=["backend"],
        locations=["Berlin"],
        german_level="B1",
        claude=ClaudeSettings(
            model="opus",
            effort="high",
            thinking_enabled=False,
        ),
        scheduler=SchedulerSettings(),
    )


def test_selection_store_uses_migration_fallback_then_persists_private_file(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    store = AiSelectionStore(paths.ai_selection_toml)
    fallback = AiRuntimeSelection(
        claude=ClaudeRuntimeSelection(model="opus", effort="high")
    )

    assert store.load(fallback) == fallback

    saved = store.save(
        AiRuntimeSelection(
            claude=ClaudeRuntimeSelection(
                model="haiku",
                effort="low",
                thinking_enabled=False,
            )
        )
    )

    assert store.load() == saved
    assert stat.S_IMODE(paths.ai_selection_toml.stat().st_mode) == 0o600


def test_config_migration_keeps_valid_api_and_falls_back_from_missing_provider(
    tmp_path: Path,
) -> None:
    paths = AppPaths.from_root(tmp_path / "home")
    providers = AiProviderStore(paths.ai_config_toml)
    provider = providers.create(
        AiProviderDraft(
            display_name="DeepSeek",
            base_url="https://api.example.com/anthropic",
            api_key="secret",
            model="deepseek-chat",
            reasoning_effort="low",
        )
    )

    valid = ai_selection_from_config(
        config(paths, ai_runtime=f"api:{provider.id}"),
        providers,
    )
    missing = ai_selection_from_config(
        config(paths, ai_runtime="api:missing"),
        providers,
    )

    assert valid.ai_runtime == f"api:{provider.id}"
    assert valid.claude.model == "opus"
    assert missing.ai_runtime == "claude-code"
    assert missing.claude.model == "opus"

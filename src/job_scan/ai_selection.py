from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from threading import RLock
from typing import Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_scan.ai_config import AiConfigError, AiProviderStore
from job_scan.config import AppConfig, ClaudeSettings


class AiSelectionError(RuntimeError):
    """Report one safe global AI-selection persistence failure."""


class ClaudeRuntimeSelection(BaseModel):
    """Store the global Claude Code controls shown in AI configuration."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="sonnet", min_length=1, max_length=200)
    effort: Literal["low", "medium", "high"] = "medium"
    thinking_enabled: bool = True


class AiRuntimeSelection(BaseModel):
    """Store the one runtime and model selection used by new AI work."""

    model_config = ConfigDict(extra="forbid")

    ai_runtime: str = Field(
        default="claude-code",
        pattern=r"^(?:claude-code|api:[a-z0-9]+(?:-[a-z0-9]+)*)$",
    )
    claude: ClaudeRuntimeSelection = Field(default_factory=ClaudeRuntimeSelection)


class AiSelectionStore:
    """Persist the global AI runtime independently from complete scan setup."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def load(
        self,
        fallback: AiRuntimeSelection | None = None,
    ) -> AiRuntimeSelection:
        """Return the saved selection or the supplied migration fallback."""
        with self._lock:
            if not self._path.exists():
                return (fallback or AiRuntimeSelection()).model_copy(deep=True)
            try:
                with self._path.open("rb") as input_file:
                    return AiRuntimeSelection.model_validate(tomllib.load(input_file))
            except (OSError, ValueError, ValidationError, tomllib.TOMLDecodeError):
                raise AiSelectionError("Could not read the saved AI selection.") from None

    def save(self, selection: AiRuntimeSelection) -> AiRuntimeSelection:
        """Atomically replace the saved global AI runtime selection."""
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(
                    dir=self._path.parent,
                    prefix=f".{self._path.name}.",
                    suffix=".tmp",
                )
                temporary = Path(name)
                try:
                    os.fchmod(descriptor, 0o600)
                    serialized = tomli_w.dumps(
                        selection.model_dump(mode="json", warnings=False)
                    )
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        output.write(serialized)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, self._path)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            except OSError:
                raise AiSelectionError("Could not save the AI selection.") from None
            return selection.model_copy(deep=True)


def apply_ai_selection_to_claude(
    current: ClaudeSettings,
    selection: AiRuntimeSelection,
) -> ClaudeSettings:
    """Overlay global model controls while retaining non-model scan limits."""
    return current.model_copy(
        update={
            "model": selection.claude.model,
            "effort": selection.claude.effort,
            "thinking_enabled": selection.claude.thinking_enabled,
        }
    )


def resolve_ai_selection(
    selection: AiRuntimeSelection,
    providers: AiProviderStore,
) -> AiRuntimeSelection:
    """Fall back to Claude Code when a selected API provider no longer exists."""
    if not selection.ai_runtime.startswith("api:"):
        return selection.model_copy(deep=True)
    try:
        providers.require(selection.ai_runtime.removeprefix("api:"))
    except AiConfigError:
        return selection.model_copy(update={"ai_runtime": "claude-code"}, deep=True)
    return selection.model_copy(deep=True)


def ai_selection_from_config(
    current: AppConfig,
    providers: AiProviderStore,
) -> AiRuntimeSelection:
    """Migrate the current valid setup model into the global selection shape."""
    return resolve_ai_selection(
        AiRuntimeSelection(
            ai_runtime=current.ai_runtime,
            claude=ClaudeRuntimeSelection(
                model=current.claude.model,
                effort=current.claude.effort,
                thinking_enabled=current.claude.thinking_enabled,
            ),
        ),
        providers,
    )


def apply_ai_selection_to_config(
    current: AppConfig,
    selection: AiRuntimeSelection,
    providers: AiProviderStore,
) -> AppConfig:
    """Return one immutable operation config using the global current AI model."""
    selection = resolve_ai_selection(selection, providers)
    ai_model = None
    if selection.ai_runtime.startswith("api:"):
        ai_model = providers.require(selection.ai_runtime.removeprefix("api:")).model
    return current.model_copy(
        update={
            "ai_runtime": selection.ai_runtime,
            "ai_model": ai_model,
            "claude": apply_ai_selection_to_claude(current.claude, selection),
        },
        deep=True,
    )

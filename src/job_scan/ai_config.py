from __future__ import annotations

import os
import re
import tempfile
import tomllib
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit

import tomli_w
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


class AiConfigError(RuntimeError):
    """Report one safe AI provider configuration failure."""


class AiProviderNotFound(AiConfigError):
    """Report that a requested AI provider does not exist."""


class AiProviderDraft(BaseModel):
    """Validate fields accepted when a local API provider is created or edited."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort

    @field_validator("display_name", "model")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("api_key")
    @classmethod
    def strip_optional_key(cls, value: str | None) -> str | None:
        return _normalized_api_key(value)

    @field_validator("base_url")
    @classmethod
    def require_public_https_shape(cls, value: str) -> str:
        return _normalized_base_url(value)


class AiProviderView(BaseModel):
    """Expose provider metadata without returning its API key."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    base_url: str
    model: str
    reasoning_effort: ReasoningEffort
    api_key_configured: bool


class StoredAiProvider(BaseModel):
    """Store one complete local provider, including its private API key."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    display_name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(min_length=1, max_length=4096)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_active_flag(cls, value: object) -> object:
        """Accept configuration files written before runtime selection moved to setup."""
        if isinstance(value, dict) and "active" in value:
            value = value.copy()
            value.pop("active")
        return value

    @field_validator("base_url")
    @classmethod
    def require_public_https_shape(cls, value: str) -> str:
        return _normalized_base_url(value)

    @field_validator("api_key")
    @classmethod
    def require_safe_http_header(cls, value: str) -> str:
        normalized = _normalized_api_key(value)
        if normalized is None:
            raise ValueError("API key must not be blank")
        return normalized

    def public(self) -> AiProviderView:
        return AiProviderView(
            id=self.id,
            display_name=self.display_name,
            base_url=self.base_url,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            api_key_configured=True,
        )


class _AiProviderFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[StoredAiProvider] = Field(default_factory=list)


class AiProviderStore:
    """Persist Anthropic-compatible provider secrets in one private local file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def list(self) -> list[AiProviderView]:
        """Return all saved providers without API keys."""
        with self._lock:
            return [provider.public() for provider in self._load().providers]

    def require(self, provider_id: str) -> StoredAiProvider:
        """Return one complete provider for an internal model call."""
        with self._lock:
            provider = self._find(self._load(), provider_id)
            return provider.model_copy(deep=True)

    def create(self, draft: AiProviderDraft) -> AiProviderView:
        """Create a provider with a stable unique local id."""
        if draft.api_key is None:
            raise AiConfigError("API key is required for a new AI configuration.")
        with self._lock:
            data = self._load()
            provider_id = _unique_provider_id(draft.display_name, data.providers)
            provider = StoredAiProvider(
                id=provider_id,
                display_name=draft.display_name,
                base_url=draft.base_url,
                api_key=draft.api_key,
                model=draft.model,
                reasoning_effort=draft.reasoning_effort,
            )
            data.providers.append(provider)
            self._save(data)
            return provider.public()

    def update(self, provider_id: str, draft: AiProviderDraft) -> AiProviderView:
        """Replace provider metadata while preserving an omitted saved API key."""
        with self._lock:
            data = self._load()
            provider = self._find(data, provider_id)
            if draft.base_url != provider.base_url and draft.api_key is None:
                raise AiConfigError(
                    "A new API key is required after changing the provider URL."
                )
            provider.display_name = draft.display_name
            provider.base_url = draft.base_url
            provider.model = draft.model
            provider.reasoning_effort = draft.reasoning_effort
            if draft.api_key is not None:
                provider.api_key = draft.api_key
            self._save(data)
            return provider.public()

    def delete(self, provider_id: str) -> None:
        """Delete one saved provider and its API key."""
        with self._lock:
            data = self._load()
            provider = self._find(data, provider_id)
            data.providers.remove(provider)
            self._save(data)

    @staticmethod
    def _find(data: _AiProviderFile, provider_id: str) -> StoredAiProvider:
        provider = next(
            (item for item in data.providers if item.id == provider_id),
            None,
        )
        if provider is None:
            raise AiProviderNotFound("AI configuration was not found.")
        return provider

    def _load(self) -> _AiProviderFile:
        if not self._path.exists():
            return _AiProviderFile()
        try:
            with self._path.open("rb") as input_file:
                return _AiProviderFile.model_validate(tomllib.load(input_file))
        except (OSError, ValueError, ValidationError, tomllib.TOMLDecodeError):
            raise AiConfigError("Could not read saved AI configurations.") from None

    def _save(self, data: _AiProviderFile) -> None:
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
                    data.model_dump(mode="json", warnings=False)
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
            raise AiConfigError("Could not save AI configurations.") from None


def _unique_provider_id(
    display_name: str,
    providers: list[StoredAiProvider],
) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-") or "provider"
    existing = {provider.id for provider in providers}
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _normalized_api_key(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        stripped.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("API key must contain printable ASCII only") from None
    if any(not 0x21 <= ord(character) <= 0x7E for character in stripped):
        raise ValueError("API key must contain printable ASCII only")
    return stripped


def _normalized_base_url(value: str) -> str:
    stripped = value.strip().rstrip("/")
    parsed = urlsplit(stripped)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("base_url contains an invalid port") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("base_url must be a plain HTTPS origin or path")
    return stripped

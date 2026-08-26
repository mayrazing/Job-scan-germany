from __future__ import annotations

import copy
import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from job_scan.claude_process import (
    ClaudeInputError,
    ClaudeInvocation,
    ClaudeInvocationInterrupted,
    ClaudeNotInstalled,
    ClaudeOutputLimitExceeded,
    ClaudeProcess,
    ClaudeProcessError,
    ClaudeRequest,
    ClaudeSpawnError,
    ClaudeTimeout,
    _ProcessResult,
)

_HEALTH_TIMEOUT_SECONDS = 10.0
_HEALTH_MAX_OUTPUT_BYTES = 64 * 1024
_MODEL_CATALOG_TIMEOUT_SECONDS = 15.0
_MODEL_CATALOG_MAX_OUTPUT_BYTES = 1024 * 1024
CODEX_FILE_CREDENTIAL_STORE_CONFIG = 'cli_auth_credentials_store="file"'

CodexReasoningEffort = Literal[
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
]


class CodexProcessError(ClaudeProcessError):
    """Report one safe, actionable Codex CLI process failure."""


class CodexNotInstalled(CodexProcessError):
    """Report that the Codex CLI executable does not exist."""


class CodexSpawnError(CodexProcessError):
    """Report that Codex CLI could not be started or cleaned up."""


class CodexNotAuthenticated(CodexProcessError):
    """Report that Codex CLI has no authenticated account."""


class CodexTimeout(ClaudeTimeout, CodexProcessError):
    """Report that a Codex CLI process exceeded its runtime."""


class CodexOutputLimitExceeded(ClaudeOutputLimitExceeded, CodexProcessError):
    """Report that either Codex CLI output stream exceeded its byte cap."""


class CodexInvocationInterrupted(CodexProcessError):
    """Report an interrupted Codex CLI invocation after cleanup."""


class CodexInputError(CodexProcessError):
    """Report that private invocation input could not be sent safely."""


class CodexHealthCommandError(CodexProcessError):
    """Report a failed or empty Codex CLI health command."""


class CodexModelCatalogError(CodexProcessError):
    """Report a failed or malformed Codex CLI model catalog."""


class CodexAuthStatus(BaseModel):
    authenticated: bool


class CodexModelOption(BaseModel):
    """Expose one selectable Codex CLI model and its supported efforts."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    default_reasoning_effort: CodexReasoningEffort
    supported_reasoning_efforts: list[CodexReasoningEffort] = Field(min_length=1)


class _CodexCatalogReasoningLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    effort: CodexReasoningEffort


class _CodexCatalogModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    slug: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    default_reasoning_level: CodexReasoningEffort
    supported_reasoning_levels: list[_CodexCatalogReasoningLevel] = Field(
        min_length=1
    )
    visibility: str


class _CodexModelCatalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: list[_CodexCatalogModel]


class CodexProcess:
    """Run bounded Codex CLI requests with project and shell access disabled."""

    def __init__(self, binary: str = "codex", *, home: Path) -> None:
        self._binary = binary
        self._home = home
        self._supervisor = ClaudeProcess(binary)

    def version(self) -> str:
        """Return the non-empty installed Codex CLI version string."""
        result = self._execute_codex(
            [self._binary, "--version"],
            stdin_bytes=None,
            timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            max_output_bytes=_HEALTH_MAX_OUTPUT_BYTES,
        )
        if result.exit_code != 0:
            raise CodexHealthCommandError(
                "Codex version check failed; run `codex --version` manually."
            )
        version = result.stdout.decode("utf-8", errors="replace").strip()
        if not version:
            raise CodexHealthCommandError(
                "Codex version check returned no version; reinstall Codex CLI."
            )
        return version

    def auth_status(self) -> CodexAuthStatus:
        """Verify that the saved Codex CLI login can be reused."""
        result = self._execute_codex(
            [
                self._binary,
                "-c",
                CODEX_FILE_CREDENTIAL_STORE_CONFIG,
                "login",
                "status",
            ],
            stdin_bytes=None,
            timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            max_output_bytes=_HEALTH_MAX_OUTPUT_BYTES,
        )
        if result.exit_code != 0 or not (result.stdout.strip() or result.stderr.strip()):
            login_command = (
                f"CODEX_HOME={shlex.quote(str(self._home))} codex -c "
                f"{shlex.quote(CODEX_FILE_CREDENTIAL_STORE_CONFIG)} login --device-auth"
            )
            raise CodexNotAuthenticated(
                "Codex CLI is not authenticated for Job Scan; "
                f"run `{login_command}` and retry."
            )
        return CodexAuthStatus(authenticated=True)

    def models(self) -> list[CodexModelOption]:
        """Return the visible model catalog reported by the installed Codex CLI."""
        result = self._execute_codex(
            [
                self._binary,
                "-c",
                CODEX_FILE_CREDENTIAL_STORE_CONFIG,
                "debug",
                "models",
            ],
            stdin_bytes=None,
            timeout_seconds=_MODEL_CATALOG_TIMEOUT_SECONDS,
            max_output_bytes=_MODEL_CATALOG_MAX_OUTPUT_BYTES,
        )
        if result.exit_code != 0:
            raise CodexModelCatalogError(
                "Codex model discovery failed; run `codex debug models` manually."
            )
        try:
            catalog = _CodexModelCatalog.model_validate_json(result.stdout)
        except (ValidationError, ValueError):
            raise CodexModelCatalogError(
                "Codex CLI returned an invalid model catalog; update Codex CLI and retry."
            ) from None
        return [
            CodexModelOption(
                id=model.slug,
                name=model.display_name,
                default_reasoning_effort=model.default_reasoning_level,
                supported_reasoning_efforts=[
                    level.effort for level in model.supported_reasoning_levels
                ],
            )
            for model in catalog.models
            if model.visibility == "list"
        ]

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        """Invoke `codex exec` in an empty temporary directory and normalize its JSON."""
        try:
            prompt = request.prompt.encode("utf-8")
        except UnicodeEncodeError:
            raise CodexInputError(
                "Codex prompt contains invalid Unicode; correct it and retry."
            ) from None

        try:
            with tempfile.TemporaryDirectory(prefix="job-scan-codex-") as directory:
                schema_path = Path(directory) / "schema.json"
                schema_path.write_text(
                    json.dumps(
                        _codex_output_schema(request.json_schema),
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                argv = self._invocation_argv(request, schema_path)
                result = self._execute_codex(
                    argv,
                    stdin_bytes=prompt,
                    timeout_seconds=float(request.timeout_seconds),
                    max_output_bytes=request.max_output_bytes,
                    cwd=directory,
                )
        except OSError:
            raise CodexSpawnError(
                "Codex request files could not be prepared; check temporary storage."
            ) from None

        stdout = _structured_stdout(result.stdout, result.exit_code)
        return ClaudeInvocation(
            argv=argv,
            stdout=stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
        )

    def _invocation_argv(self, request: ClaudeRequest, schema_path: Path) -> list[str]:
        """Build one deterministic non-interactive Codex CLI command."""
        web_search = "live" if request.allow_web_search else "disabled"
        return [
            self._binary,
            "--ask-for-approval",
            "never",
            "--disable",
            "shell_tool",
            "--disable",
            "multi_agent",
            "--disable",
            "view_image",
            "-c",
            f'web_search="{web_search}"',
            "-c",
            f'model_reasoning_effort="{request.effort}"',
            "-c",
            CODEX_FILE_CREDENTIAL_STORE_CONFIG,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            request.model,
            "--output-schema",
            str(schema_path),
            "-",
        ]

    def _execute_codex(
        self,
        argv: list[str],
        *,
        stdin_bytes: bytes | None,
        timeout_seconds: float,
        max_output_bytes: int,
        cwd: str | None = None,
    ) -> _ProcessResult:
        """Run the shared bounded supervisor and translate its public errors."""
        environment = codex_environment(self._home)
        try:
            return self._supervisor._execute(
                argv,
                stdin_bytes=stdin_bytes,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                env=environment,
                cwd=cwd,
            )
        except ClaudeNotInstalled:
            raise CodexNotInstalled(
                "Codex CLI is not installed; install it and retry."
            ) from None
        except ClaudeTimeout as error:
            raise CodexTimeout(str(error).replace("Claude", "Codex CLI")) from None
        except ClaudeOutputLimitExceeded as error:
            raise CodexOutputLimitExceeded(
                str(error).replace("Claude", "Codex CLI")
            ) from None
        except ClaudeInvocationInterrupted:
            raise CodexInvocationInterrupted(
                "Codex CLI invocation was interrupted; its process group was terminated."
            ) from None
        except ClaudeInputError:
            raise CodexInputError(
                "Codex prompt could not be sent completely; retry the command."
            ) from None
        except ClaudeSpawnError:
            raise CodexSpawnError(
                "Codex CLI could not be started or cleaned up; retry the command."
            ) from None


def _structured_stdout(stdout: bytes, exit_code: int) -> bytes:
    """Wrap successful raw Codex JSON in the envelope used by existing consumers."""
    if exit_code != 0:
        return stdout
    try:
        payload = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return stdout
    return json.dumps(
        {"structured_output": payload},
        separators=(",", ":"),
    ).encode("utf-8")


def codex_environment(home: Path) -> dict[str, str]:
    """Return one child environment rooted in private Job Scan Codex storage."""
    try:
        home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            home.chmod(0o700)
    except OSError:
        raise CodexSpawnError(
            "Codex CLI data directory could not be prepared; check Job Scan storage."
        ) from None
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    return environment


def _codex_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a Codex-compatible schema without changing the shared request."""
    normalized = copy.deepcopy(schema)
    _make_object_schemas_strict(normalized)
    return normalized


def _make_object_schemas_strict(value: Any) -> None:
    """Require every object property and reject unspecified properties."""
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
            value["additionalProperties"] = False
        for child in value.values():
            _make_object_schemas_strict(child)
    elif isinstance(value, list):
        for child in value:
            _make_object_schemas_strict(child)

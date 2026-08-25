from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import IO, Any, Literal

from pydantic import BaseModel, Field

_HEALTH_TIMEOUT_SECONDS = 10.0
_HEALTH_MAX_OUTPUT_BYTES = 64 * 1024
_TERMINATE_GRACE_SECONDS = 2.0
_READ_CHUNK_BYTES = 16 * 1024
_POLL_INTERVAL_SECONDS = 0.01


class ClaudeProcessError(RuntimeError):
    """Report one safe, actionable Claude process failure."""


class ClaudeNotInstalled(ClaudeProcessError):
    """Report that the configured Claude executable does not exist."""


class ClaudeSpawnError(ClaudeProcessError):
    """Report that the configured Claude executable could not be started."""


class ClaudeNotAuthenticated(ClaudeProcessError):
    """Report that Claude Code has no authenticated account."""


class ClaudeTimeout(ClaudeProcessError):
    """Report that a Claude process exceeded its total runtime."""


class ClaudeOutputLimitExceeded(ClaudeProcessError):
    """Report that either Claude output stream exceeded its byte cap."""


class ClaudeHealthCommandError(ClaudeProcessError):
    """Report a nonzero or empty Claude health command result."""


class ClaudeAuthResponseMalformed(ClaudeProcessError):
    """Report a Claude authentication response with an invalid shape."""


class ClaudeInvocationInterrupted(ClaudeProcessError):
    """Report a cancelled or interrupted invocation after process cleanup."""


class ClaudeInputError(ClaudeProcessError):
    """Report that private invocation input could not be sent safely."""


class ClaudeRequest(BaseModel):
    runtime: str = Field(
        default="claude-code",
        pattern=r"^(?:claude-code|codex-cli|api:[a-z0-9]+(?:-[a-z0-9]+)*)$",
    )
    prompt: str
    json_schema: dict[str, Any]
    model: str
    runtime_model: str | None = Field(default=None, min_length=1, max_length=200)
    effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"]
    thinking_enabled: bool = True
    timeout_seconds: int = Field(gt=0)
    max_output_bytes: int = Field(gt=0)
    allow_web_search: bool = False


class ClaudeInvocation(BaseModel):
    argv: list[str]
    stdout: bytes
    stderr: bytes
    exit_code: int
    duration_seconds: float
    budget_usd: Decimal | None = None


class ClaudeAuthStatus(BaseModel):
    authenticated: bool
    account_label: str | None = None


@dataclass
class _StreamCapture:
    limit: int
    chunks: list[bytes] = field(default_factory=list)
    size: int = 0
    exceeded: threading.Event = field(default_factory=threading.Event)
    failed: threading.Event = field(default_factory=threading.Event)

    def append(self, chunk: bytes) -> bool:
        """Store bytes up to the cap and report whether the reader may continue."""
        remaining = self.limit - self.size
        if len(chunk) > remaining:
            if remaining > 0:
                self.chunks.append(chunk[:remaining])
                self.size += remaining
            self.exceeded.set()
            return False
        self.chunks.append(chunk)
        self.size += len(chunk)
        return True

    def value(self) -> bytes:
        """Return the bounded bytes collected by one stream reader."""
        return b"".join(self.chunks)


@dataclass(frozen=True)
class _ProcessResult:
    stdout: bytes
    stderr: bytes
    exit_code: int
    duration_seconds: float


@dataclass
class _WriterState:
    failed: threading.Event = field(default_factory=threading.Event)


class ClaudeProcess:
    """Run bounded, tool-disabled Claude Code processes without exposing prompts."""

    def __init__(self, binary: str = "claude") -> None:
        self._binary = binary

    def version(self) -> str:
        """Return the non-empty installed Claude Code version string."""
        result = self._execute(
            [self._binary, "--version"],
            stdin_bytes=None,
            timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            max_output_bytes=_HEALTH_MAX_OUTPUT_BYTES,
        )
        if result.exit_code != 0:
            raise ClaudeHealthCommandError(
                "Claude version check failed; run `claude --version` manually."
            )
        version = result.stdout.decode("utf-8", errors="replace").strip()
        if not version:
            raise ClaudeHealthCommandError(
                "Claude version check returned no version; reinstall Claude Code."
            )
        return version

    def auth_status(self) -> ClaudeAuthStatus:
        """Return authenticated Claude account status from bounded JSON output."""
        result = self._execute(
            [self._binary, "auth", "status", "--json"],
            stdin_bytes=None,
            timeout_seconds=_HEALTH_TIMEOUT_SECONDS,
            max_output_bytes=_HEALTH_MAX_OUTPUT_BYTES,
        )
        if result.exit_code != 0:
            raise ClaudeHealthCommandError(
                "Claude authentication check failed; run `claude auth status` manually."
            )
        payload = _bounded_json_object(result.stdout)
        if payload is None or type(payload.get("loggedIn")) is not bool:
            raise ClaudeAuthResponseMalformed(
                "Claude authentication status was malformed; update Claude Code and retry."
            )
        if not payload["loggedIn"]:
            raise ClaudeNotAuthenticated(
                "Claude Code is not authenticated; run `claude auth login` and retry."
            )
        account_label = _auth_account_label(payload)
        return ClaudeAuthStatus(authenticated=True, account_label=account_label)

    def invoke(self, request: ClaudeRequest) -> ClaudeInvocation:
        """Invoke Claude with a bounded tool allowlist and a UTF-8 stdin prompt."""
        tool_argv = (
            [
                "--tools",
                "WebSearch,WebFetch",
                "--allowedTools",
                "WebSearch,WebFetch",
            ]
            if request.allow_web_search
            else ["--tools", ""]
        )
        argv = [
            self._binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(request.json_schema, separators=(",", ":")),
            *tool_argv,
            "--safe-mode",
            "--no-session-persistence",
            "--model",
            request.model,
            "--effort",
            request.effort,
        ]
        stdin_bytes = _encode_prompt(request.prompt)
        if stdin_bytes is None:
            raise ClaudeInputError(
                "Claude prompt contains invalid Unicode; correct it and retry."
            ) from None
        result = self._execute(
            argv,
            stdin_bytes=stdin_bytes,
            timeout_seconds=float(request.timeout_seconds),
            max_output_bytes=request.max_output_bytes,
            env=_claude_environment(request.thinking_enabled),
        )
        return ClaudeInvocation(
            argv=argv,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            budget_usd=_budget_from_stdout(result.stdout, result.exit_code),
        )

    def _execute(
        self,
        argv: list[str],
        *,
        stdin_bytes: bytes | None,
        timeout_seconds: float,
        max_output_bytes: int,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> _ProcessResult:
        """Start one process, bound both pipes concurrently, and always reap it."""
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        process: subprocess.Popen[bytes] | None = None
        process_group: int | None = None
        threads: list[threading.Thread] = []
        writer_state: _WriterState | None = None
        stdout_capture = _StreamCapture(max_output_bytes)
        stderr_capture = _StreamCapture(max_output_bytes)
        terminate_group = False
        exit_code: int | None = None

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                bufsize=0,
                env=env,
                cwd=cwd,
            )
            process_group = process.pid
            terminate_group = True
            if process.stdout is None or process.stderr is None:
                raise ClaudeSpawnError(
                    "Claude process pipes were unavailable; retry the command."
                )

            threads.extend(
                [
                    _start_reader(process.stdout, stdout_capture, "stdout"),
                    _start_reader(process.stderr, stderr_capture, "stderr"),
                ]
            )
            if stdin_bytes is not None:
                if process.stdin is None:
                    raise ClaudeSpawnError(
                        "Claude process input was unavailable; retry the command."
                    )
                writer_state = _WriterState()
                threads.append(_start_writer(process.stdin, stdin_bytes, writer_state))

            while True:
                _check_io_state(
                    stdout_capture,
                    stderr_capture,
                    writer_state,
                    max_output_bytes,
                )

                exit_code = process.poll()
                readers_finished = all(not thread.is_alive() for thread in threads[:2])
                writer_finished = len(threads) == 2 or not threads[2].is_alive()
                group_finished = process_group is not None and not _process_group_exists(
                    process_group
                )
                if (
                    exit_code is not None
                    and readers_finished
                    and writer_finished
                    and group_finished
                ):
                    _check_io_state(
                        stdout_capture,
                        stderr_capture,
                        writer_state,
                        max_output_bytes,
                    )
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClaudeTimeout(
                        f"Claude process timed out after {timeout_seconds:g} seconds."
                    )
                time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

            try:
                exit_code = process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                raise ClaudeSpawnError(
                    "Claude process exit status was unavailable; retry the command."
                ) from None
            terminate_group = False
        except FileNotFoundError:
            raise ClaudeNotInstalled(
                "Claude Code is not installed; install it and retry."
            ) from None
        except OSError:
            raise ClaudeSpawnError(
                "Claude Code could not be started; check the configured executable."
            ) from None
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise ClaudeInvocationInterrupted(
                "Claude invocation was interrupted; its process group was terminated."
            ) from None
        finally:
            if process is not None:
                cleanup_deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
                cleanup_error: ClaudeSpawnError | None = None
                if terminate_group and process_group is not None:
                    try:
                        _terminate_process_group(
                            process, process_group, cleanup_deadline
                        )
                    except ClaudeSpawnError as error:
                        cleanup_error = error
                else:
                    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                        process.wait(timeout=0)
                _close_process_pipes(process)
                for thread in threads:
                    thread.join(max(0.0, cleanup_deadline - time.monotonic()))
                if any(thread.is_alive() for thread in threads):
                    raise ClaudeSpawnError(
                        "Claude process cleanup did not complete; retry the command."
                    ) from None
                if cleanup_error is not None:
                    raise cleanup_error from None

        if exit_code is None:
            raise ClaudeSpawnError("Claude process ended without an exit status.")
        return _ProcessResult(
            stdout=stdout_capture.value(),
            stderr=stderr_capture.value(),
            exit_code=exit_code,
            duration_seconds=max(0.0, time.monotonic() - started_at),
        )


def _claude_environment(thinking_enabled: bool) -> dict[str, str] | None:
    """Disable Claude thinking for one child process without changing the parent."""
    if thinking_enabled:
        return None
    environment = os.environ.copy()
    environment["MAX_THINKING_TOKENS"] = "0"
    return environment


def _start_reader(
    stream: IO[bytes], capture: _StreamCapture, stream_name: str
) -> threading.Thread:
    """Start one bounded pipe reader for stdout or stderr."""

    def read_stream() -> None:
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk or not capture.append(chunk):
                    return
        except (OSError, ValueError):
            capture.failed.set()

    thread = threading.Thread(
        target=read_stream,
        name=f"job-scan-claude-{stream_name}",
        daemon=True,
    )
    thread.start()
    return thread


def _start_writer(
    stream: IO[bytes], payload: bytes, state: _WriterState
) -> threading.Thread:
    """Write the private prompt through stdin without blocking pipe supervision."""

    def write_stdin() -> None:
        payload_view = memoryview(payload)
        offset = 0
        try:
            while offset < len(payload_view):
                written = stream.write(payload_view[offset:])
                remaining = len(payload_view) - offset
                if type(written) is not int or written <= 0 or written > remaining:
                    state.failed.set()
                    return
                offset += written
        except Exception:  # noqa: BLE001 - thread boundary must report every write failure
            state.failed.set()
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - close failures also make input incomplete
                state.failed.set()

    thread = threading.Thread(
        target=write_stdin,
        name="job-scan-claude-stdin",
        daemon=True,
    )
    thread.start()
    return thread


def _check_io_state(
    stdout_capture: _StreamCapture,
    stderr_capture: _StreamCapture,
    writer_state: _WriterState | None,
    max_output_bytes: int,
) -> None:
    """Raise the safe error represented by current terminal I/O state."""
    if stdout_capture.exceeded.is_set() or stderr_capture.exceeded.is_set():
        raise ClaudeOutputLimitExceeded(
            "Claude process output exceeded limit of "
            f"{max_output_bytes} bytes per stream."
        )
    if stdout_capture.failed.is_set() or stderr_capture.failed.is_set():
        raise ClaudeSpawnError(
            "Claude process output could not be read; retry the command."
        )
    if writer_state is not None and writer_state.failed.is_set():
        raise ClaudeInputError(
            "Claude prompt could not be sent completely; retry the command."
        )


def _encode_prompt(prompt: str) -> bytes | None:
    """Encode a prompt without allowing invalid input into an exception payload."""
    try:
        return prompt.encode("utf-8")
    except UnicodeEncodeError:
        return None


def _terminate_process_group(
    process: subprocess.Popen[bytes], group_id: int, deadline: float
) -> None:
    """Terminate one owned POSIX process group, escalate, and reap its parent."""
    _signal_process_group(group_id, signal.SIGTERM)
    started_at = time.monotonic()
    term_deadline = started_at + max(0.0, deadline - started_at) / 2
    while time.monotonic() < term_deadline:
        process.poll()
        if not _process_group_exists(group_id):
            break
        time.sleep(
            min(_POLL_INTERVAL_SECONDS, max(0.0, term_deadline - time.monotonic()))
        )
    if _process_group_exists(group_id):
        _signal_process_group(group_id, signal.SIGKILL)
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        raise ClaudeSpawnError(
            "Claude process cleanup did not complete; retry the command."
        ) from None

    while _process_group_exists(group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ClaudeSpawnError(
                "Claude process cleanup did not complete; retry the command."
            ) from None
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _signal_process_group(group_id: int, signal_number: signal.Signals) -> None:
    """Send one signal to the process group when it still exists."""
    try:
        os.killpg(group_id, signal_number)
    except ProcessLookupError:
        pass


def _process_group_exists(group_id: int) -> bool:
    """Return whether any process remains in the owned process group."""
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    """Close every parent-side process pipe after termination or completion."""
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _bounded_json_object(payload: bytes) -> dict[str, Any] | None:
    """Parse a previously bounded JSON object without exposing parse details."""
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _auth_account_label(payload: dict[str, Any]) -> str | None:
    """Return the first non-empty public account label in Claude auth JSON."""
    for key in ("email", "accountLabel", "orgName"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _budget_from_stdout(stdout: bytes, exit_code: int) -> Decimal | None:
    """Parse optional finite non-negative cost metadata from successful JSON."""
    if exit_code != 0:
        return None
    payload = _bounded_json_object(stdout)
    if payload is None:
        return None
    raw_budget = payload.get("total_cost_usd")
    if isinstance(raw_budget, bool) or not isinstance(raw_budget, (str, int, float)):
        return None
    try:
        budget = Decimal(str(raw_budget))
    except InvalidOperation:
        return None
    if not budget.is_finite() or budget < 0:
        return None
    return budget

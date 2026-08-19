from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import threading
import time
import traceback
from decimal import Decimal
from pathlib import Path

import pytest

from job_scan import claude_process
from job_scan.claude_process import (
    ClaudeAuthResponseMalformed,
    ClaudeHealthCommandError,
    ClaudeInvocationInterrupted,
    ClaudeNotAuthenticated,
    ClaudeNotInstalled,
    ClaudeOutputLimitExceeded,
    ClaudeProcess,
    ClaudeRequest,
    ClaudeSpawnError,
    ClaudeTimeout,
)

FAKE_CLAUDE = Path(__file__).parent / "fakes" / "claude"
PRIVATE_PROMPT = "  Lebenslauf: Grüße 🔒\nJD: Python engineer\n  "


def request(**overrides: object) -> ClaudeRequest:
    values: dict[str, object] = {
        "prompt": PRIVATE_PROMPT,
        "json_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        "model": "sonnet",
        "effort": "medium",
        "timeout_seconds": 5,
        "max_output_bytes": 200_000,
    }
    values.update(overrides)
    return ClaudeRequest.model_validate(values)


def configure_fake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> tuple[Path, Path]:
    argv_path = tmp_path / "argv.json"
    stdin_path = tmp_path / "stdin.bin"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("FAKE_CLAUDE_ARGV_PATH", str(argv_path))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_PATH", str(stdin_path))
    return argv_path, stdin_path


def process_identity(pid: int) -> tuple[int, int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[1]), int(fields[2])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def wait_for_processes_to_exit(pids: set[int], timeout_seconds: float = 3) -> set[int]:
    deadline = time.monotonic() + timeout_seconds
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if process_identity(pid) is not None}
        time.sleep(0.01)
    return remaining


def exception_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(error))


class ControlledWriteStream:
    def __init__(self, outcomes: list[int | None | BaseException]) -> None:
        self.outcomes = outcomes
        self.accepted = bytearray()
        self.closed = False

    def write(self, payload: bytes | memoryview) -> int | None:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, int) and 0 < outcome <= len(payload):
            self.accepted.extend(payload[:outcome])
        return outcome

    def close(self) -> None:
        self.closed = True


class RaceWriteStream:
    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.closed_event = threading.Event()
        self.writer: threading.Thread | None = None

    def write(self, _payload: bytes | memoryview) -> int:
        self.writer = threading.current_thread()
        assert self.release.wait(1)
        raise OSError("controlled terminal write failure")

    def close(self) -> None:
        self.closed_event.set()


class TerminalRaceProcess:
    def __init__(self, terminal_state: str) -> None:
        self.pid = 12345
        self.release = threading.Event()
        self.stdin = (
            RaceWriteStream(self.release) if terminal_state == "writer_failed" else None
        )
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.reader_threads: list[threading.Thread] = []

    def poll(self) -> int:
        self.release.set()
        for thread in self.reader_threads:
            thread.join(1)
        if self.stdin is not None:
            assert self.stdin.closed_event.wait(1)
            assert self.stdin.writer is not None
            self.stdin.writer.join(1)
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


class BlockingWaitProcess:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if timeout is None:
            self.release.wait(1)
            return 0
        raise subprocess.TimeoutExpired("controlled", timeout)


def assert_no_claude_io_resources(fd_count_before: int) -> None:
    fd_dir = Path("/proc/self/fd")
    assert [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("job-scan-claude-")
    ] == []
    if fd_dir.is_dir():
        assert len(list(fd_dir.iterdir())) <= fd_count_before


def test_invoke_uses_exact_safe_argv_and_prompt_only_on_utf8_stdin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    argv_path, stdin_path = configure_fake(monkeypatch, tmp_path, "success")
    schema = request().json_schema

    invocation = ClaudeProcess(str(FAKE_CLAUDE)).invoke(request())

    assert invocation.argv == [
        str(FAKE_CLAUDE),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--tools",
        "",
        "--safe-mode",
        "--no-session-persistence",
        "--model",
        "sonnet",
        "--effort",
        "medium",
    ]
    assert json.loads(argv_path.read_text()) == invocation.argv
    assert stdin_path.read_bytes() == PRIVATE_PROMPT.encode("utf-8")
    assert PRIVATE_PROMPT not in "\0".join(invocation.argv)
    assert "--dangerously-skip-permissions" not in invocation.argv


def test_invoke_disables_thinking_for_one_request_without_mutating_parent_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")
    thinking_path = tmp_path / "thinking.txt"
    monkeypatch.setenv("FAKE_CLAUDE_THINKING_PATH", str(thinking_path))
    monkeypatch.setenv("MAX_THINKING_TOKENS", "8192")

    ClaudeProcess(str(FAKE_CLAUDE)).invoke(request(thinking_enabled=False))

    assert thinking_path.read_text() == "0"
    assert os.environ["MAX_THINKING_TOKENS"] == "8192"


def test_web_search_request_exposes_only_claude_web_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")

    invocation = ClaudeProcess(str(FAKE_CLAUDE)).invoke(
        request(allow_web_search=True)
    )

    tools_index = invocation.argv.index("--tools")
    allowed_index = invocation.argv.index("--allowedTools")
    assert invocation.argv[tools_index + 1] == "WebSearch,WebFetch"
    assert invocation.argv[allowed_index + 1] == "WebSearch,WebFetch"
    assert "Bash" not in invocation.argv
    assert "Read" not in invocation.argv
    assert "Edit" not in invocation.argv


def test_nonzero_invocation_preserves_separate_streams_and_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "nonzero")

    invocation = ClaudeProcess(str(FAKE_CLAUDE)).invoke(request())

    assert invocation.exit_code == 17
    assert invocation.stdout == b"fake stdout"
    assert invocation.stderr == b"fake failure"
    assert invocation.budget_usd is None


def test_success_parses_bounded_budget_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")

    invocation = ClaudeProcess(str(FAKE_CLAUDE)).invoke(request())

    assert invocation.exit_code == 0
    assert invocation.budget_usd == Decimal("0.0125")
    assert json.loads(invocation.stdout)["structured_output"] == {"result": "ok"}
    assert invocation.stderr == b"fake stderr"
    assert invocation.duration_seconds >= 0


@pytest.mark.parametrize(
    ("mode", "stdout"),
    [("invalid-json", b"{not-json"), ("nonzero", b"fake stdout")],
)
def test_invalid_or_nonzero_json_leaves_budget_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    stdout: bytes,
) -> None:
    configure_fake(monkeypatch, tmp_path, mode)

    invocation = ClaudeProcess(str(FAKE_CLAUDE)).invoke(request())

    assert invocation.stdout == stdout
    assert invocation.budget_usd is None


def test_concurrent_readers_drain_both_large_streams_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "success")
    monkeypatch.setenv("FAKE_CLAUDE_PAD_BYTES", "100000")

    invocation = ClaudeProcess(str(FAKE_CLAUDE)).invoke(
        request(max_output_bytes=150_000)
    )

    assert len(invocation.stdout) > 100_000
    assert len(invocation.stderr) > 100_000
    assert invocation.exit_code == 0


def test_version_and_authenticated_status_use_exact_health_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    argv_path, _ = configure_fake(monkeypatch, tmp_path, "version")
    process = ClaudeProcess(str(FAKE_CLAUDE))

    assert process.version() == "2.1.7 (Claude Code)"
    assert json.loads(argv_path.read_text()) == [str(FAKE_CLAUDE), "--version"]

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    status = process.auth_status()

    assert status.authenticated is True
    assert status.account_label == "fake@example.test"
    assert json.loads(argv_path.read_text()) == [
        str(FAKE_CLAUDE),
        "auth",
        "status",
        "--json",
    ]


@pytest.mark.parametrize("method_name", ["version", "auth_status"])
def test_missing_binary_is_distinct_from_other_spawn_failures(
    tmp_path: Path,
    method_name: str,
) -> None:
    missing = tmp_path / "missing-claude"

    with pytest.raises(ClaudeNotInstalled) as captured:
        getattr(ClaudeProcess(str(missing)), method_name)()

    assert PRIVATE_PROMPT not in exception_text(captured.value)


def test_non_executable_binary_is_spawn_failure(tmp_path: Path) -> None:
    binary = tmp_path / "claude-directory"
    binary.mkdir()

    with pytest.raises(ClaudeSpawnError, match="could not be started"):
        ClaudeProcess(str(binary)).version()


def test_unauthenticated_status_has_actionable_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configure_fake(monkeypatch, tmp_path, "unauthenticated")

    with pytest.raises(ClaudeNotAuthenticated, match="claude auth login"):
        ClaudeProcess(str(FAKE_CLAUDE)).auth_status()


@pytest.mark.parametrize("mode", ["malformed-auth", "invalid-json"])
def test_malformed_auth_response_has_safe_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    configure_fake(monkeypatch, tmp_path, mode)

    with pytest.raises(ClaudeAuthResponseMalformed) as captured:
        ClaudeProcess(str(FAKE_CLAUDE)).auth_status()

    assert "not-json" not in exception_text(captured.value)
    assert "loggedIn" not in exception_text(captured.value)


@pytest.mark.parametrize("method_name", ["version", "auth_status"])
def test_nonzero_health_command_has_safe_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
) -> None:
    configure_fake(monkeypatch, tmp_path, "nonzero")

    with pytest.raises(ClaudeHealthCommandError) as captured:
        getattr(ClaudeProcess(str(FAKE_CLAUDE)), method_name)()

    assert "fake failure" not in exception_text(captured.value)
    assert "fake stdout" not in exception_text(captured.value)


@pytest.mark.parametrize("method_name", ["version", "auth_status"])
def test_health_command_timeout_is_bounded_and_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
) -> None:
    configure_fake(monkeypatch, tmp_path, "timeout")
    monkeypatch.setattr("job_scan.claude_process._HEALTH_TIMEOUT_SECONDS", 0.1)

    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        getattr(ClaudeProcess(str(FAKE_CLAUDE)), method_name)()

    assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    ("method_name", "stream"),
    [("version", "stdout"), ("auth_status", "stderr")],
)
def test_health_command_enforces_64_kib_per_stream_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    method_name: str,
    stream: str,
) -> None:
    configure_fake(monkeypatch, tmp_path, "oversized")
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT_BYTES", str((64 * 1024) + 1))
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", stream)

    with pytest.raises(ClaudeOutputLimitExceeded, match="65536"):
        getattr(ClaudeProcess(str(FAKE_CLAUDE)), method_name)()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_invoke_aborts_while_either_output_stream_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream: str,
) -> None:
    configure_fake(monkeypatch, tmp_path, "oversized")
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT_BYTES", "200000")
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", stream)

    with pytest.raises(ClaudeOutputLimitExceeded, match="1024"):
        ClaudeProcess(str(FAKE_CLAUDE)).invoke(
            request(max_output_bytes=1024)
        )


def test_invoke_timeout_kills_and_reaps_parent_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_path = tmp_path / "pids.txt"
    configure_fake(monkeypatch, tmp_path, "spawn-child")
    monkeypatch.setenv("FAKE_CLAUDE_PID_PATH", str(pid_path))

    with pytest.raises(ClaudeTimeout):
        ClaudeProcess(str(FAKE_CLAUDE)).invoke(request(timeout_seconds=1))

    parent_pid, child_pid = map(int, pid_path.read_text().split())
    assert wait_for_processes_to_exit({parent_pid, child_pid}) == set()


def test_keyboard_interrupt_is_typed_and_cleans_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if not hasattr(signal, "setitimer"):
        pytest.skip("interval timers require POSIX")
    pid_path = tmp_path / "pids.txt"
    configure_fake(monkeypatch, tmp_path, "spawn-child")
    monkeypatch.setenv("FAKE_CLAUDE_PID_PATH", str(pid_path))
    previous_handler = signal.getsignal(signal.SIGALRM)

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 0.2)
    try:
        with pytest.raises(ClaudeInvocationInterrupted):
            ClaudeProcess(str(FAKE_CLAUDE)).invoke(request(timeout_seconds=5))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

    parent_pid, child_pid = map(int, pid_path.read_text().split())
    assert wait_for_processes_to_exit({parent_pid, child_pid}) == set()


@pytest.mark.parametrize("mode", ["orphan-closed-stdio", "orphan-holds-stdin"])
def test_parent_exit_cannot_bypass_deadline_or_leave_owned_group_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
) -> None:
    pid_path = tmp_path / "pids.txt"
    fd_dir = Path("/proc/self/fd")
    fd_count_before = len(list(fd_dir.iterdir())) if fd_dir.is_dir() else 0
    configure_fake(monkeypatch, tmp_path, mode)
    monkeypatch.setenv("FAKE_CLAUDE_PID_PATH", str(pid_path))
    prompt = "x" * 20_000_000 if mode == "orphan-holds-stdin" else PRIVATE_PROMPT

    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        ClaudeProcess(str(FAKE_CLAUDE)).invoke(
            request(prompt=prompt, timeout_seconds=1)
        )
    elapsed = time.monotonic() - started

    parent_pid, child_pid, parent_group, child_group = map(
        int, pid_path.read_text().split()
    )
    assert parent_group == child_group == parent_pid
    assert elapsed >= 0.8
    assert elapsed < 3.25
    assert wait_for_processes_to_exit({parent_pid, child_pid}) == set()
    assert_no_claude_io_resources(fd_count_before)


def test_stdin_writer_retries_short_writes_until_payload_is_complete() -> None:
    stream = ControlledWriteStream([3, 2, 5])
    state = claude_process._WriterState()

    writer = claude_process._start_writer(stream, b"0123456789", state)
    writer.join(1)

    assert not writer.is_alive()
    assert stream.accepted == b"0123456789"
    assert stream.closed
    assert not state.failed.is_set()


@pytest.mark.parametrize(
    "outcomes",
    [
        [None],
        [0],
        [-1],
        [11],
        [OSError("controlled write failure")],
        [3, 0],
    ],
)
def test_stdin_writer_marks_invalid_or_incomplete_writes_as_failed(
    outcomes: list[int | None | BaseException],
) -> None:
    stream = ControlledWriteStream(outcomes)
    state = claude_process._WriterState()

    writer = claude_process._start_writer(stream, b"0123456789", state)
    writer.join(1)

    assert not writer.is_alive()
    assert stream.closed
    assert state.failed.is_set()


@pytest.mark.parametrize(
    ("terminal_state", "expected_error"),
    [
        ("writer_failed", claude_process.ClaudeInputError),
        ("stdout_exceeded", ClaudeOutputLimitExceeded),
        ("stdout_failed", ClaudeSpawnError),
        ("stderr_exceeded", ClaudeOutputLimitExceeded),
        ("stderr_failed", ClaudeSpawnError),
    ],
)
def test_execute_rechecks_terminal_io_state_before_success(
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: str,
    expected_error: type[claude_process.ClaudeProcessError],
) -> None:
    process = TerminalRaceProcess(terminal_state)
    target_stream, target_outcome = terminal_state.split("_", 1)

    def start_reader(
        _stream: object,
        capture: claude_process._StreamCapture,
        stream_name: str,
    ) -> threading.Thread:
        def finish_reader() -> None:
            if stream_name == target_stream:
                assert process.release.wait(1)
                getattr(capture, target_outcome).set()

        thread = threading.Thread(target=finish_reader, daemon=True)
        thread.start()
        process.reader_threads.append(thread)
        return thread

    monkeypatch.setattr(claude_process.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(claude_process, "_start_reader", start_reader)
    monkeypatch.setattr(claude_process, "_process_group_exists", lambda _group: False)

    with pytest.raises(expected_error):
        ClaudeProcess("controlled")._execute(
            ["controlled"],
            stdin_bytes=b"x" if terminal_state == "writer_failed" else None,
            timeout_seconds=1,
            max_output_bytes=1,
        )


def test_process_group_cleanup_reap_uses_expired_deadline_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = BlockingWaitProcess()
    signals: list[signal.Signals] = []
    result: list[ClaudeSpawnError] = []
    finished = threading.Event()

    monkeypatch.setattr(claude_process, "_process_group_exists", lambda _group: True)
    monkeypatch.setattr(
        claude_process,
        "_signal_process_group",
        lambda _group, signal_number: signals.append(signal_number),
    )

    def cleanup() -> None:
        try:
            claude_process._terminate_process_group(
                process, 12345, time.monotonic() - 1
            )
        except ClaudeSpawnError as error:
            result.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=cleanup, daemon=True)
    thread.start()
    try:
        assert finished.wait(0.25)
    finally:
        process.release.set()
        thread.join(1)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_timeouts == [0]
    assert len(result) == 1
    assert isinstance(result[0], ClaudeSpawnError)


def test_process_group_cleanup_reserves_deadline_for_sigkill_and_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = BlockingWaitProcess()
    kill_sent = threading.Event()
    finished = threading.Event()

    monkeypatch.setattr(claude_process, "_process_group_exists", lambda _group: True)

    def record_signal(_group: int, signal_number: signal.Signals) -> None:
        if signal_number == signal.SIGKILL:
            kill_sent.set()

    monkeypatch.setattr(claude_process, "_signal_process_group", record_signal)

    def cleanup() -> None:
        try:
            claude_process._terminate_process_group(
                process, 12345, time.monotonic() + 0.5
            )
        except ClaudeSpawnError:
            pass
        finally:
            finished.set()

    thread = threading.Thread(target=cleanup, daemon=True)
    thread.start()
    try:
        assert kill_sent.wait(0.4)
        assert finished.wait(0.1)
    finally:
        process.release.set()
        thread.join(1)

    assert process.wait_timeouts
    assert process.wait_timeouts[0] is not None
    assert process.wait_timeouts[0] > 0


def test_invalid_utf8_prompt_raises_safe_domain_error_without_prompt_exposure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    argv_path, stdin_path = configure_fake(monkeypatch, tmp_path, "success")
    invalid_prompt = PRIVATE_PROMPT + "\ud800"

    with caplog.at_level(logging.DEBUG), pytest.raises(
        claude_process.ClaudeProcessError
    ) as captured:
        ClaudeProcess(str(FAKE_CLAUDE)).invoke(request(prompt=invalid_prompt))

    exposed = "\n".join(
        [
            str(captured.value),
            repr(captured.value),
            exception_text(captured.value),
            caplog.text,
            *(str(arg) for arg in captured.value.args),
        ]
    )
    assert PRIVATE_PROMPT not in exposed
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert not argv_path.exists()
    assert not stdin_path.exists()


def test_failure_never_exposes_prompt_in_error_logs_argv_or_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_fake(monkeypatch, tmp_path, "oversized")
    environment_before = dict(os.environ)

    with caplog.at_level(logging.DEBUG), pytest.raises(
        ClaudeOutputLimitExceeded
    ) as captured:
        ClaudeProcess(str(FAKE_CLAUDE)).invoke(
            request(prompt=PRIVATE_PROMPT, max_output_bytes=10)
        )

    exposed = "\n".join(
        [
            str(captured.value),
            repr(captured.value),
            exception_text(captured.value),
            caplog.text,
            *captured.value.args,
        ]
    )
    assert PRIVATE_PROMPT not in exposed
    assert all(PRIVATE_PROMPT not in value for value in environment_before.values())
    assert all(PRIVATE_PROMPT not in value for value in os.environ.values())


def test_repeated_invocations_leave_no_reader_threads_or_file_descriptors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        pytest.skip("file descriptor accounting requires procfs")
    configure_fake(monkeypatch, tmp_path, "success")
    before = len(list(fd_dir.iterdir()))

    for _ in range(10):
        ClaudeProcess(str(FAKE_CLAUDE)).invoke(request())

    after = len(list(fd_dir.iterdir()))
    leaking_threads = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("job-scan-claude-")
    ]
    assert leaking_threads == []
    assert after <= before

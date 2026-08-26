from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from job_scan.codex_process import (
    CODEX_FILE_CREDENTIAL_STORE_CONFIG,
    CodexProcessError,
    codex_environment,
)

CodexLoginState = Literal[
    "idle",
    "starting",
    "pending",
    "succeeded",
    "failed",
    "cancelled",
]

_ACTIVE_STATES = frozenset({"starting", "pending"})
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_VERIFICATION_URL = re.compile(r"https://auth\.openai\.com/[^\s\x1b]+")
_USER_CODE = re.compile(r"\b[A-Z0-9]{4,8}-[A-Z0-9]{4,8}\b")


class CodexLoginSnapshot(BaseModel):
    """Expose only safe device-login state to the local browser."""

    model_config = ConfigDict(extra="forbid")

    state: CodexLoginState = "idle"
    verification_url: str | None = None
    user_code: str | None = None
    error: str | None = None


class CodexLoginWorkflow:
    """Own one isolated Codex device-login process and publish safe state."""

    def __init__(self, home: Path, *, binary: str = "codex") -> None:
        self._home = home
        self._binary = binary
        self._lock = threading.Lock()
        self._state = CodexLoginSnapshot()
        self._process: subprocess.Popen[str] | None = None

    def snapshot(self) -> CodexLoginSnapshot:
        """Return a detached login-state snapshot."""
        with self._lock:
            return self._state.model_copy(deep=True)

    def start(self) -> CodexLoginSnapshot:
        """Start device login once and return its initial visible state."""
        with self._lock:
            if self._state.state in _ACTIVE_STATES:
                return self._state.model_copy(deep=True)
            try:
                process = subprocess.Popen(
                    [
                        self._binary,
                        "-c",
                        CODEX_FILE_CREDENTIAL_STORE_CONFIG,
                        "login",
                        "--device-auth",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    shell=False,
                    start_new_session=True,
                    cwd=self._home,
                    env=codex_environment(self._home),
                )
            except (OSError, CodexProcessError):
                self._state = CodexLoginSnapshot(
                    state="failed",
                    error="Codex login could not be started.",
                )
                return self._state.model_copy(deep=True)
            self._process = process
            self._state = CodexLoginSnapshot(state="starting")
            thread = threading.Thread(
                target=self._watch,
                args=(process,),
                name="job-scan-codex-login",
                daemon=True,
            )
            thread.start()
            return self._state.model_copy(deep=True)

    def cancel(self) -> CodexLoginSnapshot:
        """Cancel the active login process and publish a terminal state."""
        with self._lock:
            process = self._process
            if self._state.state not in _ACTIVE_STATES or process is None:
                return self._state.model_copy(deep=True)
            self._state = CodexLoginSnapshot(state="cancelled")
            snapshot = self._state.model_copy(deep=True)
        _terminate(process)
        return snapshot

    def close(self) -> None:
        """Release an active login process during server shutdown."""
        self.cancel()

    def _watch(self, process: subprocess.Popen[str]) -> None:
        """Parse safe device-login fields and settle the process state."""
        verification_url: str | None = None
        user_code: str | None = None
        stream = process.stdout
        if stream is not None:
            for raw_line in stream:
                line = _ANSI_ESCAPE.sub("", raw_line)
                url_match = _VERIFICATION_URL.search(line)
                code_match = _USER_CODE.search(line)
                if url_match is not None:
                    verification_url = url_match.group(0)
                if code_match is not None:
                    user_code = code_match.group(0)
                if verification_url is not None and user_code is not None:
                    with self._lock:
                        if self._process is not process:
                            return
                        if self._state.state in _ACTIVE_STATES:
                            self._state = CodexLoginSnapshot(
                                state="pending",
                                verification_url=verification_url,
                                user_code=user_code,
                            )
        exit_code = process.wait()
        with self._lock:
            if self._process is not process:
                return
            self._process = None
            if self._state.state == "cancelled":
                return
            self._state = CodexLoginSnapshot(
                state="succeeded" if exit_code == 0 else "failed",
                error=None if exit_code == 0 else "Codex login failed or expired.",
            )


def _terminate(process: subprocess.Popen[str]) -> None:
    """Terminate only the owned device-login process group."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

import pytest

from job_scan import mdns as mdns_module
from job_scan.mdns import MdnsError, MdnsPublisher, lan_ipv4_from_route


class RecordingProcess:
    def __init__(self, *, exit_code: int | None = None) -> None:
        self.exit_code = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("avahi-publish-address", timeout)
        return self.exit_code

    def kill(self) -> None:
        self.terminated = True
        self.exit_code = -9


def _wait_until(predicate: Callable[[], bool], timeout: float = 1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached before timeout")


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        (
            "multicast 224.0.0.251 dev wlo1 src 192.168.3.28 uid 1000",
            "192.168.3.28",
        ),
        (
            "multicast 224.0.0.251 dev enp2s0 src 10.20.30.40 metric 100",
            "10.20.30.40",
        ),
    ],
)
def test_lan_ipv4_from_route_returns_active_multicast_source(
    route: str, expected: str
) -> None:
    assert lan_ipv4_from_route(route) == expected


@pytest.mark.parametrize(
    "route",
    [
        "multicast 224.0.0.251 dev wlo1",
        "local 224.0.0.251 dev lo src 127.0.0.1",
        "multicast 224.0.0.251 dev wlo1 src not-an-ip",
    ],
)
def test_lan_ipv4_from_route_rejects_missing_or_non_lan_source(route: str) -> None:
    with pytest.raises(MdnsError):
        lan_ipv4_from_route(route)


def test_mdns_publisher_republishes_changed_ip_and_stops_owned_processes() -> None:
    current_ip = "192.168.3.28"
    publications: list[tuple[str, str, RecordingProcess]] = []

    def resolve_ipv4() -> str:
        return current_ip

    def publish_address(hostname: str, ip_address: str) -> RecordingProcess:
        process = RecordingProcess()
        publications.append((hostname, ip_address, process))
        return process

    publisher = MdnsPublisher(
        "job-scan-germany.local",
        check_interval_seconds=0.01,
        resolve_ipv4=resolve_ipv4,
        publish_address=publish_address,
    )

    try:
        assert publisher.start() == "192.168.3.28"
        _wait_until(lambda: len(publications) == 1)

        current_ip = "192.168.3.29"
        _wait_until(lambda: len(publications) == 2)

        assert [(host, ip) for host, ip, _process in publications] == [
            ("job-scan-germany.local", "192.168.3.28"),
            ("job-scan-germany.local", "192.168.3.29"),
        ]
        assert publications[0][2].terminated is True
    finally:
        publisher.stop()

    assert publications[1][2].terminated is True


def test_mdns_publisher_restarts_publisher_that_exits_unexpectedly() -> None:
    publications: list[RecordingProcess] = []

    def publish_address(_hostname: str, _ip_address: str) -> RecordingProcess:
        process = RecordingProcess()
        publications.append(process)
        return process

    publisher = MdnsPublisher(
        "job-scan-germany.local",
        check_interval_seconds=0.01,
        resolve_ipv4=lambda: "192.168.3.28",
        publish_address=publish_address,
    )

    try:
        publisher.start()
        publications[0].exit_code = 1
        _wait_until(lambda: len(publications) == 2)
    finally:
        publisher.stop()


def test_mdns_publisher_cleans_process_when_monitor_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = RecordingProcess()
    publisher = MdnsPublisher(
        resolve_ipv4=lambda: "192.168.3.28",
        publish_address=lambda _hostname, _ip_address: process,
    )

    def fail_start(_thread: object) -> None:
        raise RuntimeError("thread start failed")

    monkeypatch.setattr(mdns_module.Thread, "start", fail_start)

    with pytest.raises(RuntimeError, match="thread start failed"):
        publisher.start()

    assert process.terminated is True


def test_avahi_process_is_cleaned_when_startup_probe_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = RecordingProcess()

    monkeypatch.setattr(mdns_module.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def interrupt_probe(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(mdns_module.time, "sleep", interrupt_probe)

    with pytest.raises(KeyboardInterrupt):
        mdns_module._publish_address("job-scan-germany.local", "192.168.3.28")

    assert process.terminated is True

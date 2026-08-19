from __future__ import annotations

import ipaddress
import subprocess
import time
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread
from typing import Protocol

MDNS_HOSTNAME = "job-scan-germany.local"


class MdnsError(RuntimeError):
    """Report that the project hostname cannot be published on the LAN."""


class _PublisherProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def lan_ipv4_from_route(route: str) -> str:
    """Return the non-loopback IPv4 source selected for mDNS multicast."""
    fields = route.split()
    try:
        candidate = fields[fields.index("src") + 1]
        address = ipaddress.ip_address(candidate)
    except (ValueError, IndexError) as error:
        raise MdnsError("No active LAN IPv4 address was found.") from error
    if (
        not isinstance(address, ipaddress.IPv4Address)
        or address.is_loopback
        or address.is_multicast
        or address.is_unspecified
    ):
        raise MdnsError("No active LAN IPv4 address was found.")
    return str(address)


def _resolve_lan_ipv4() -> str:
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "get", "224.0.0.251"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise MdnsError("Could not inspect the active LAN IPv4 route.") from error
    return lan_ipv4_from_route(result.stdout)


def _publish_address(hostname: str, ip_address: str) -> _PublisherProcess:
    process: _PublisherProcess | None = None
    try:
        process = subprocess.Popen(
            [
                "avahi-publish-address",
                "--no-reverse",
                hostname,
                ip_address,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.1)
    except BaseException:
        MdnsPublisher._stop_process(process)
        raise
    if process.poll() is not None:
        raise MdnsError(f"Could not publish {hostname} through Avahi.")
    return process


def _start_publisher(hostname: str, ip_address: str) -> _PublisherProcess:
    try:
        return _publish_address(hostname, ip_address)
    except OSError as error:
        raise MdnsError("Could not start avahi-publish-address.") from error


class MdnsPublisher:
    """Keep one project hostname published at the active LAN IPv4 address."""

    def __init__(
        self,
        hostname: str = MDNS_HOSTNAME,
        *,
        check_interval_seconds: float = 2,
        resolve_ipv4: Callable[[], str] | None = None,
        publish_address: Callable[[str, str], _PublisherProcess] | None = None,
    ) -> None:
        self.hostname = hostname
        self._check_interval_seconds = check_interval_seconds
        self._resolve_ipv4 = resolve_ipv4 or _resolve_lan_ipv4
        self._publish_address = publish_address or _start_publisher
        self._stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._process: _PublisherProcess | None = None
        self._published_ip: str | None = None

    def start(self) -> str:
        """Publish immediately, then monitor for address or process changes."""
        if self._thread is not None:
            raise MdnsError(f"{self.hostname} is already being monitored.")
        self._stop_event.clear()
        try:
            current_ip = self._resolve_ipv4()
            self._replace_publisher(current_ip)
            self._thread = Thread(
                target=self._monitor,
                name="job-scan-mdns",
                daemon=True,
            )
            self._thread.start()
        except BaseException as error:
            self.stop()
            if isinstance(error, (OSError, subprocess.SubprocessError)):
                raise MdnsError(f"Could not publish {self.hostname}.") from error
            raise
        return current_ip

    @property
    def current_ip(self) -> str | None:
        """Return the IPv4 address currently owned by the Avahi publisher."""
        with self._lock:
            return self._published_ip

    def stop(self) -> None:
        """Stop monitoring and terminate only this instance's Avahi process."""
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread is not current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=max(1, self._check_interval_seconds + 1))
        with self._lock:
            self._stop_process(self._process)
            self._process = None
            self._published_ip = None
        self._thread = None

    def _monitor(self) -> None:
        while not self._stop_event.wait(self._check_interval_seconds):
            try:
                current_ip = self._resolve_ipv4()
                with self._lock:
                    process_stopped = (
                        self._process is None or self._process.poll() is not None
                    )
                    address_changed = current_ip != self._published_ip
                if process_stopped or address_changed:
                    self._replace_publisher(current_ip)
            except (MdnsError, OSError, subprocess.SubprocessError):
                continue

    def _replace_publisher(self, ip_address: str) -> None:
        with self._lock:
            self._stop_process(self._process)
            self._process = None
            if self._stop_event.is_set():
                return
            process = self._publish_address(self.hostname, ip_address)
            if process.poll() is not None:
                raise MdnsError(f"Could not publish {self.hostname} through Avahi.")
            self._process = process
            self._published_ip = ip_address

    @staticmethod
    def _stop_process(process: _PublisherProcess | None) -> None:
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kill = getattr(process, "kill", None)
            if kill is not None:
                kill()
                process.wait(timeout=2)

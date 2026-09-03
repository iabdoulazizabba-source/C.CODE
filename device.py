"""Talk to a ZKTeco-compatible attendance terminal over TCP/IP.

The device stores *punches* (a device user id + a timestamp each time
someone scans). We poll the terminal, pull those punches, and hand them
back as plain ``Punch`` records. Pairing punches into work sessions and
computing hours happens in :mod:`models`.

Two connectors implement the same tiny interface so the rest of the app
never imports ``pyzk`` directly:

* :class:`ZKConnector`   - the real device, via the ``pyzk`` library.
* :class:`FakeConnector` - an in-memory stand-in for tests / demos.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Protocol


@dataclass
class Punch:
    """One scan read from the device."""

    device_uid: str
    timestamp: datetime
    status: int = 0  # raw punch/status code from the device


@dataclass
class DeviceUser:
    """A user enrolled on the device."""

    device_uid: str
    name: str


class Connector(Protocol):
    """Minimal interface the app depends on."""

    def fetch_punches(self) -> List[Punch]: ...
    def fetch_users(self) -> List[DeviceUser]: ...
    def ping(self) -> bool: ...


class DeviceError(RuntimeError):
    """Raised when the terminal can't be reached or read."""


class ZKConnector:
    """Real ZKTeco terminal accessed with ``pyzk``."""

    def __init__(self, host: str, port: int = 4370, password: int = 0,
                 timeout: int = 5, force_udp: bool = False):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.force_udp = force_udp

    def _zk(self):
        # Imported lazily so the app (and its tests) don't require pyzk
        # unless a real device is actually used.
        from zk import ZK

        return ZK(
            self.host,
            port=self.port,
            timeout=self.timeout,
            password=self.password,
            force_udp=self.force_udp,
            ommit_ping=False,
        )

    def ping(self) -> bool:
        try:
            conn = self._zk().connect()
            conn.disconnect()
            return True
        except Exception:
            return False

    def fetch_punches(self) -> List[Punch]:
        conn = None
        try:
            conn = self._zk().connect()
            conn.disable_device()  # freeze the terminal while we read
            records = conn.get_attendance() or []
            return [
                Punch(
                    device_uid=str(r.user_id),
                    timestamp=r.timestamp,
                    status=int(getattr(r, "punch", 0) or 0),
                )
                for r in records
            ]
        except Exception as exc:  # pragma: no cover - hardware path
            raise DeviceError(f"Could not read punches from {self.host}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.enable_device()
                    conn.disconnect()
                except Exception:
                    pass

    def fetch_users(self) -> List[DeviceUser]:
        conn = None
        try:
            conn = self._zk().connect()
            users = conn.get_users() or []
            return [
                DeviceUser(device_uid=str(u.user_id), name=u.name or str(u.user_id))
                for u in users
            ]
        except Exception as exc:  # pragma: no cover - hardware path
            raise DeviceError(f"Could not read users from {self.host}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass


class FakeConnector:
    """In-memory device for tests and demos (no hardware needed)."""

    def __init__(self, punches: Optional[List[Punch]] = None,
                 users: Optional[List[DeviceUser]] = None,
                 reachable: bool = True):
        self._punches = list(punches or [])
        self._users = list(users or [])
        self.reachable = reachable

    def ping(self) -> bool:
        return self.reachable

    def fetch_punches(self) -> List[Punch]:
        if not self.reachable:
            raise DeviceError("Fake device is unreachable")
        return list(self._punches)

    def fetch_users(self) -> List[DeviceUser]:
        if not self.reachable:
            raise DeviceError("Fake device is unreachable")
        return list(self._users)

    # Helpers for tests to simulate new activity on the terminal.
    def add_punch(self, punch: Punch) -> None:
        self._punches.append(punch)


class ZKDiscoveryConnector:
    """Find a ZKTeco terminal by its serial number, wherever it is on the LAN.

    Scans ``subnet``.1-254 for an open device port, connects to each and reads
    the serial, and uses the host whose serial matches ``serial``. The found
    host is cached (and re-verified by serial) so routine polling is fast; if
    the device moves, the next lookup re-discovers it. This makes the app
    immune to IP changes and address conflicts.
    """

    def __init__(self, serial, subnet, port=4370, password=0,
                 force_udp=False, timeout=5, hint_host=None):
        self.serial = str(serial).strip()
        self.subnet = subnet  # e.g. "192.168.10"
        self.port = port
        self.password = password
        self.force_udp = force_udp
        self.timeout = timeout
        self.hint_host = hint_host  # try this address first (last known IP)
        self._host = None  # cached discovered host

    # --- low-level helpers (overridable in tests) ---
    def _port_open(self, host):
        import socket

        try:
            with socket.create_connection((host, self.port), timeout=1.0):
                return True
        except OSError:
            return False

    def _serial_of(self, host):
        conn = None
        try:
            conn = ZKConnector(host, port=self.port, password=self.password,
                               force_udp=self.force_udp, timeout=self.timeout)._zk().connect()
            return (conn.get_serialnumber() or "").strip()
        except Exception:
            return None
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass

    def _scan_hosts(self):
        import concurrent.futures as cf

        hosts = [f"{self.subnet}.{i}" for i in range(1, 255)]
        found = []
        with cf.ThreadPoolExecutor(max_workers=128) as ex:
            for host, is_open in zip(hosts, ex.map(self._port_open, hosts)):
                if is_open:
                    found.append(host)
        return found

    def resolve(self, force=False):
        """Return the host running our serial, or None. Caches the result."""
        candidates = []
        if not force and self._host:
            candidates.append(self._host)
        if self.hint_host and self.hint_host not in candidates:
            candidates.append(self.hint_host)
        for host in candidates:
            if self._port_open(host) and self._serial_of(host) == self.serial:
                self._host = host
                return host
        for host in self._scan_hosts():
            if self._serial_of(host) == self.serial:
                self._host = host
                return host
        self._host = None
        return None

    @property
    def host(self):
        return self._host

    def _delegate(self, method):
        host = self.resolve()
        if host is None:
            raise DeviceError(
                f"No terminal with serial {self.serial} found on "
                f"{self.subnet}.0/24"
            )
        conn = ZKConnector(host, port=self.port, password=self.password,
                           force_udp=self.force_udp, timeout=self.timeout)
        try:
            return getattr(conn, method)()
        except DeviceError:
            # Cached host may be stale/hijacked -> rediscover once and retry.
            host = self.resolve(force=True)
            if host is None:
                raise
            conn = ZKConnector(host, port=self.port, password=self.password,
                               force_udp=self.force_udp, timeout=self.timeout)
            return getattr(conn, method)()

    def ping(self):
        return self.resolve() is not None

    def fetch_punches(self):
        return self._delegate("fetch_punches")

    def fetch_users(self):
        return self._delegate("fetch_users")


def connector_from_config(config) -> Connector:
    """Build the connector named by the app config.

    ``DEVICE_DRIVER`` selects ``zk`` (real) or ``fake``. If ``DEVICE_SERIAL``
    is set, the terminal is auto-discovered by serial across ``DEVICE_SUBNET``
    (derived from ``DEVICE_HOST`` when not given); otherwise a fixed
    ``DEVICE_HOST`` is used.
    """
    driver = (config.get("DEVICE_DRIVER") or "zk").lower()
    if driver == "fake":
        return FakeConnector()

    port = int(config.get("DEVICE_PORT", 4370))
    password = int(config.get("DEVICE_PASSWORD", 0))
    force_udp = bool(config.get("DEVICE_FORCE_UDP", False))
    host = config.get("DEVICE_HOST")
    serial = config.get("DEVICE_SERIAL")

    if serial:
        subnet = config.get("DEVICE_SUBNET")
        if not subnet and host:
            subnet = host.rsplit(".", 1)[0]
        return ZKDiscoveryConnector(
            serial=serial, subnet=subnet or "192.168.10", port=port,
            password=password, force_udp=force_udp, hint_host=host,
        )

    return ZKConnector(host=host, port=port, password=password,
                       force_udp=force_udp)

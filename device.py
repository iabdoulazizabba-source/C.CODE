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


def connector_from_config(config) -> Connector:
    """Build the connector named by the app config.

    ``DEVICE_DRIVER`` selects ``zk`` (real) or ``fake``. For ``zk`` the
    host/port come from ``DEVICE_HOST`` / ``DEVICE_PORT``.
    """
    driver = (config.get("DEVICE_DRIVER") or "zk").lower()
    if driver == "fake":
        return FakeConnector()
    return ZKConnector(
        host=config["DEVICE_HOST"],
        port=int(config.get("DEVICE_PORT", 4370)),
        password=int(config.get("DEVICE_PASSWORD", 0)),
        force_udp=bool(config.get("DEVICE_FORCE_UDP", False)),
    )

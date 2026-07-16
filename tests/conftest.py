from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@dataclass
class FakeController:
    name: str = "fake"
    clients: list[Any] = field(default_factory=list)
    aps: list[Any] = field(default_factory=list)
    ap_stats: list[Any] = field(default_factory=list)
    radio_stats: dict[str, list[Any]] = field(default_factory=dict)
    force_reconnect_calls: list[str] = field(default_factory=list)
    btm_calls: list[tuple[str, str | None]] = field(default_factory=list)
    closed: bool = False

    async def list_wireless_clients(self) -> list[Any]:
        return list(self.clients)

    async def list_aps(self) -> list[Any]:
        return list(self.aps)

    async def list_ap_stats(self) -> list[Any]:
        return list(self.ap_stats)

    async def get_ap_radio_stats(self, ap_id: str) -> list[Any]:
        return list(self.radio_stats.get(ap_id, []))

    async def force_reconnect_client(self, mac: str) -> None:
        self.force_reconnect_calls.append(mac)

    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None:
        self.btm_calls.append((mac, target_bssid))

    async def login(self) -> None:
        # No-op: FakeController holds no session. Present because login() is part
        # of the Controller lifecycle contract (controllers/base.py), which
        # main.Daemon.run() now calls directly rather than via getattr.
        return None

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeHANotifier:
    posts: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    async def notify(self, mac: str, severity: str, message: str = "", **extra: Any) -> None:
        self.posts.append({"mac": mac, "severity": severity, "message": message, **extra})

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeRebooter:
    """Stand-in for the reboot backend the proactive scheduler invokes (ADR-0006).
    Records each RebootTarget passed to reboot() so tests can assert a reboot did
    (or did not) fire — the reboot analogue of FakeController.force_reconnect_calls.

    Intentionally type-agnostic (stores whatever target object the scheduler
    resolved), so the shared conftest stays collectable without importing the
    reboot package's RebootTarget type.
    """

    calls: list[Any] = field(default_factory=list)

    async def reboot(self, target: Any) -> None:
        self.calls.append(target)


@dataclass
class FakeHARegistry:
    """Stand-in for the HA device-registry transport the reboot resolver consumes
    (ADR-0005). Maps a MAC to the entity list of the HA device whose registry
    connections include that MAC; returns None when no device matches. Records
    each lookup so tests can assert the resolver did (or did not) consult HA.

    Intentionally type-agnostic: it stores whatever entity objects the test passes
    in, so importing the resolver's HAEntity type here is unnecessary (keeps the
    shared conftest collectable even before the reboot package exists).
    """

    entities_by_mac: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    async def entities_for_mac(self, mac: str) -> list[Any] | None:
        self.calls.append(mac)
        return self.entities_by_mac.get(mac)


def make_client(
    *,
    mac: str = "aa:bb:cc:dd:ee:ff",
    signal: int = -75,
    tx_rate_kbps: int = 6000,
    tx_retries: int = 50,
    wifi_tx_attempts: int = 100,
    radio: str = "ng",
    ap_id: str = "ap1",
    ap_cu_total: int = 70,
    name: str | None = None,
    tx_bytes: int | None = None,
    rx_bytes: int | None = None,
    ip: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        mac=mac,
        signal=signal,
        tx_rate_kbps=tx_rate_kbps,
        tx_retries=tx_retries,
        wifi_tx_attempts=wifi_tx_attempts,
        radio=radio,
        ap_id=ap_id,
        ap_cu_total=ap_cu_total,
        name=name,
        tx_bytes=tx_bytes,
        rx_bytes=rx_bytes,
        ip=ip,
    )


@dataclass
class FakeDnsSource:
    """Stand-in DnsSource (ADR-0011). Returns whatever ``queries`` is set to on each
    ``queries_since`` (tests reassign it between polls to control what's "new"), and
    can be flipped to ``fail`` to exercise the fetch-failure fail-soft path.

    Type-agnostic on the query objects — stores whatever DnsQuery-shaped items the
    test passes, so the shared conftest doesn't import the dns_sources package.
    """

    queries: list[Any] = field(default_factory=list)
    fail: bool = False
    since_calls: list[float] = field(default_factory=list)
    login_calls: int = 0
    closed: bool = False

    async def login(self) -> None:
        self.login_calls += 1

    async def queries_since(self, since: float) -> list[Any]:
        self.since_calls.append(since)
        if self.fail:
            raise RuntimeError("dns source unavailable")
        return list(self.queries)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def fake_controller() -> FakeController:
    return FakeController()


@pytest.fixture
def fake_ha() -> FakeHANotifier:
    return FakeHANotifier()

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
    radio_stats: dict[str, list[Any]] = field(default_factory=dict)
    force_reconnect_calls: list[str] = field(default_factory=list)
    closed: bool = False

    async def list_wireless_clients(self) -> list[Any]:
        return list(self.clients)

    async def list_aps(self) -> list[Any]:
        return list(self.aps)

    async def get_ap_radio_stats(self, ap_id: str) -> list[Any]:
        return list(self.radio_stats.get(ap_id, []))

    async def force_reconnect_client(self, mac: str) -> None:
        self.force_reconnect_calls.append(mac)

    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None:
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
    )


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def fake_controller() -> FakeController:
    return FakeController()


@pytest.fixture
def fake_ha() -> FakeHANotifier:
    return FakeHANotifier()

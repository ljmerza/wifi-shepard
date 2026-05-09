from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ClientSnapshot:
    mac: str
    signal: int
    tx_rate_kbps: int
    tx_retries: int
    wifi_tx_attempts: int
    radio: str
    ap_id: str
    ap_cu_total: int


@dataclass(frozen=True)
class APSnapshot:
    id: str
    name: str
    mac: str


@dataclass(frozen=True)
class RadioStats:
    radio: str
    cu_total: int
    bssid: str


@runtime_checkable
class Controller(Protocol):
    async def list_wireless_clients(self) -> list[ClientSnapshot]: ...

    async def list_aps(self) -> list[APSnapshot]: ...

    async def get_ap_radio_stats(self, ap_id: str) -> list[RadioStats]: ...

    async def force_reconnect_client(self, mac: str) -> None: ...

    async def close(self) -> None: ...

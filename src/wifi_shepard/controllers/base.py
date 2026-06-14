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
    name: str | None = None


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
    channel: int = 0


@dataclass(frozen=True)
class APStats:
    """AP-level health snapshot: device identity + CPU/memory load + per-radio
    channel utilization. Surfaced for the read-only UI's "noisy APs" view.

    ``cpu_pct`` / ``mem_pct`` are ``None`` when the controller doesn't report
    them (fail-soft for display, unlike the fail-closed detection path).
    """

    id: str
    name: str
    mac: str
    cpu_pct: float | None
    mem_pct: float | None
    radios: tuple[RadioStats, ...]


@runtime_checkable
class Controller(Protocol):
    """Brand-agnostic AP controller surface.

    Identifier convention: ``ClientSnapshot.ap_id``, ``APSnapshot.id``, and the ``ap_id``
    argument to ``get_ap_radio_stats`` must all use the same per-backend identifier scheme,
    so that callers can round-trip ``client.ap_id`` -> ``get_ap_radio_stats(ap_id=...)``
    without an intermediate lookup. The choice of scheme (MAC, vendor _id, hostname, ...)
    is up to each backend.
    """

    async def login(self) -> None:
        """Establish the controller session. Called once per controller at startup,
        before any list/action method, and paired with ``close()`` on shutdown
        (``main.Daemon.run()`` does this). Backends that need no session step may
        implement it as a no-op, but the method must exist — the lifecycle is part
        of the contract, not duck-typed.
        """
        ...

    async def list_wireless_clients(self) -> list[ClientSnapshot]: ...

    async def list_aps(self) -> list[APSnapshot]: ...

    async def get_ap_radio_stats(self, ap_id: str) -> list[RadioStats]: ...

    async def list_ap_stats(self) -> list[APStats]:
        """AP-level health snapshots (identity + CPU/mem + per-radio CU) for the UI.

        Single-pass over the controller's device list, so callers don't pay the
        per-AP cost of ``get_ap_radio_stats``. Backends that can't report CPU/mem
        set those fields to ``None``.
        """
        ...

    async def force_reconnect_client(self, mac: str) -> None: ...

    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None:
        """Optional 802.11v BTM transition. Backends without BTM support raise NotImplementedError.

        Per ADR-0001 §Decision, no MVP backend implements this; the method exists on the
        Protocol so a future BTM-capable backend slots in without changing the Protocol shape.
        """
        ...

    async def close(self) -> None: ...

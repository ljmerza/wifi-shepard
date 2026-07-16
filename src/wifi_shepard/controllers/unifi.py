from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp
import aiounifi
from aiounifi.models.api import ApiRequest
from aiounifi.models.configuration import Configuration

from .base import APSnapshot, APStats, ClientSnapshot, RadioStats


@dataclass
class _BTMRequest(ApiRequest):
    """Raw 802.11v BSS Transition request for UniFi controllers.

    aiounifi==85 exposes only deauth (cmd: kick-sta on /cmd/stamgr); BTM is
    not modelled in the typed library. The path /cmd/devmgr with
    cmd=bss-transition matches the call the UniFi web UI issues for
    "BSS Transition Roaming". Per ADR-0003 §Risks, this payload is
    fixture-pinned and Phase 8 integration may invalidate it.
    """

    @classmethod
    def create(cls, mac: str, target_bssid: str | None) -> _BTMRequest:
        data: dict[str, Any] = {"cmd": "bss-transition", "mac": mac.lower()}
        if target_bssid is not None:
            data["target_bssid"] = target_bssid.lower()
        return cls(method="post", path="/cmd/devmgr", data=data)


class UniFiSchemaError(RuntimeError):
    """Raised when a UniFi API response is missing a required field or has the wrong type.

    Fail-closed posture per ADR-0001 §Risks: rather than silently coercing or zero-filling,
    we surface schema drift so it can be diagnosed against a recorded fixture.
    """


def _parse_pct(value: Any) -> float | None:
    """Coerce a UniFi system-stats percentage to a float, fail-soft.

    UniFi reports ``system-stats`` cpu/mem as strings (e.g. ``"5.2"`` — and
    possibly ``"5.2%"``; the exact format isn't guaranteed across firmware).
    Strip a trailing ``%`` and whitespace; return ``None`` for missing/blank/
    unparseable values so the UI shows "—" rather than a bogus 0.
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("%").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _optional_int(raw: dict[str, Any], key: str) -> int | None:
    """Read an optional integer field fail-soft (ADR-0010 byte counters).

    Unlike ``_require`` (fail-closed for detection-critical fields), a missing or
    wrong-typed value yields ``None`` so its absence never breaks scanning. ``bool``
    is rejected explicitly — it is an ``int`` subclass, and a stray ``true`` must not
    coerce to ``1`` byte of traffic.
    """
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _require(raw: dict[str, Any], key: str, expected_type: type, *, owner: str) -> Any:
    if key not in raw:
        raise UniFiSchemaError(f"{owner}.{key} missing")
    value = raw[key]
    if not isinstance(value, expected_type):
        raise UniFiSchemaError(
            f"{owner}.{key} expected {expected_type.__name__}, got {type(value).__name__}"
        )
    return value


class UniFiController:
    """UniFi backend wrapping aiounifi 85.

    Lazily owns its aiohttp session — the session is created in ``login()`` and torn down
    in ``close()``. Callers should always pair the two; ``main.Daemon.run()`` already does.

    Identifier convention: ``ClientSnapshot.ap_id``, ``APSnapshot.id``, and the ``ap_id``
    argument to ``get_ap_radio_stats`` are all the AP's MAC. UniFi exposes ``ap_mac`` on
    each client without an extra lookup, so MAC is the cheapest stable join key.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        verify_ssl: bool = True,
        port: int = 8443,
        name: str = "unifi",
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.site = site
        self.verify_ssl = verify_ssl
        self.port = port
        self.name = name
        self._session: aiohttp.ClientSession | None = None
        self._unifi: aiounifi.Controller | None = None

    async def login(self) -> None:
        if self._unifi is not None:
            return
        ssl_context: ssl.SSLContext | bool = (
            ssl.create_default_context() if self.verify_ssl else False
        )
        self._session = aiohttp.ClientSession()
        config = Configuration(
            session=self._session,
            host=self.host,
            username=self.username,
            password=self.password,
            port=self.port,
            site=self.site,
            ssl_context=ssl_context,
        )
        self._unifi = aiounifi.Controller(config)
        await self._unifi.login()

    async def close(self) -> None:
        session = self._session
        self._session = None
        self._unifi = None
        if session is not None:
            await session.close()

    def _controller(self) -> aiounifi.Controller:
        if self._unifi is None:
            raise RuntimeError("UniFiController.login() must be called before use")
        return self._unifi

    async def list_wireless_clients(self) -> list[ClientSnapshot]:
        unifi = self._controller()
        await unifi.clients.update()
        await unifi.devices.update()
        cu_lookup = self._build_cu_lookup(unifi)
        out: list[ClientSnapshot] = []
        for client in unifi.clients.values():
            raw = client.raw
            if raw.get("is_wired", False):
                continue
            mac = _require(raw, "mac", str, owner="client")
            ap_mac = _require(raw, "ap_mac", str, owner="client")
            radio = _require(raw, "radio", str, owner="client")
            cu_total = cu_lookup.get((ap_mac, radio), 0)
            # Friendly label for the UI: operator-assigned `name` first, then the
            # device-reported `hostname`; None when neither is present.
            name = raw.get("name") or raw.get("hostname") or None
            out.append(
                ClientSnapshot(
                    mac=mac,
                    signal=_require(raw, "signal", int, owner="client"),
                    tx_rate_kbps=_require(raw, "tx_rate", int, owner="client"),
                    tx_retries=_require(raw, "tx_retries", int, owner="client"),
                    wifi_tx_attempts=_require(raw, "wifi_tx_attempts", int, owner="client"),
                    radio=radio,
                    ap_id=ap_mac,
                    ap_cu_total=cu_total,
                    name=name,
                    tx_bytes=_optional_int(raw, "tx_bytes"),
                    rx_bytes=_optional_int(raw, "rx_bytes"),
                )
            )
        return out

    async def list_aps(self) -> list[APSnapshot]:
        unifi = self._controller()
        await unifi.devices.update()
        out: list[APSnapshot] = []
        for device in unifi.devices.values():
            raw = device.raw
            if raw.get("type") != "uap":
                continue
            mac = _require(raw, "mac", str, owner="device")
            out.append(
                APSnapshot(
                    id=mac,
                    name=raw.get("name", ""),
                    mac=mac,
                )
            )
        return out

    async def get_ap_radio_stats(self, ap_id: str) -> list[RadioStats]:
        unifi = self._controller()
        await unifi.devices.update()
        for device in unifi.devices.values():
            raw = device.raw
            if raw.get("mac") != ap_id:
                continue
            stats = raw.get("radio_table_stats") or []
            return [
                RadioStats(
                    radio=_require(entry, "radio", str, owner="radio_table_stats"),
                    cu_total=_require(entry, "cu_total", int, owner="radio_table_stats"),
                    bssid=entry.get("bssid", ""),
                    channel=entry.get("channel", 0),
                )
                for entry in stats
            ]
        return []

    async def list_ap_stats(self) -> list[APStats]:
        unifi = self._controller()
        await unifi.devices.update()
        out: list[APStats] = []
        for device in unifi.devices.values():
            raw = device.raw
            if raw.get("type") != "uap":
                continue
            mac = _require(raw, "mac", str, owner="device")
            system_stats = raw.get("system-stats") or {}
            radios = tuple(
                RadioStats(
                    radio=_require(entry, "radio", str, owner="radio_table_stats"),
                    cu_total=_require(entry, "cu_total", int, owner="radio_table_stats"),
                    bssid=entry.get("bssid", ""),
                    channel=entry.get("channel", 0),
                )
                for entry in raw.get("radio_table_stats") or []
            )
            out.append(
                APStats(
                    id=mac,
                    name=raw.get("name", ""),
                    mac=mac,
                    cpu_pct=_parse_pct(system_stats.get("cpu")),
                    mem_pct=_parse_pct(system_stats.get("mem")),
                    radios=radios,
                )
            )
        return out

    async def force_reconnect_client(self, mac: str) -> None:
        unifi = self._controller()
        await unifi.clients.reconnect(mac)

    async def send_btm_request(self, mac: str, target_bssid: str | None = None) -> None:
        """Send a raw 802.11v BSS Transition Management request to the controller.

        ADR-0003 §Decision Fork D: aiounifi has no typed BTM call, so we issue
        a raw API request via the same controller object that handles deauth.
        Failures (non-2xx, schema drift) propagate — Actor catches and records
        a fail-closed audit row; the next scan cycle's deauth-fallback path
        (AC-4) keeps kicks working when the BTM endpoint is unavailable.
        """
        unifi = self._controller()
        await unifi.request(_BTMRequest.create(mac, target_bssid))

    @staticmethod
    def _build_cu_lookup(unifi: aiounifi.Controller) -> dict[tuple[str, str], int]:
        lookup: dict[tuple[str, str], int] = {}
        for device in unifi.devices.values():
            raw = device.raw
            if raw.get("type") != "uap":
                continue
            ap_mac = _require(raw, "mac", str, owner="device")
            for entry in raw.get("radio_table_stats") or []:
                radio = _require(entry, "radio", str, owner="radio_table_stats")
                cu_total = _require(entry, "cu_total", int, owner="radio_table_stats")
                lookup[(ap_mac, radio)] = cu_total
        return lookup

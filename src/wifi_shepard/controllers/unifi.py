from __future__ import annotations

import ssl
from typing import Any

import aiohttp
import aiounifi
from aiounifi.models.configuration import Configuration

from .base import APSnapshot, ClientSnapshot, RadioStats


class UniFiSchemaError(RuntimeError):
    """Raised when a UniFi API response is missing a required field or has the wrong type.

    Fail-closed posture per ADR-0001 §Risks: rather than silently coercing or zero-filling,
    we surface schema drift so it can be diagnosed against a recorded fixture.
    """


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
    """UniFi backend wrapping aiounifi 90+.

    Lazily owns its aiohttp session — the session is created in ``login()`` and torn down
    in ``close()``. Callers should always pair the two; ``main.Daemon.run()`` already does.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        site: str = "default",
        verify_ssl: bool = False,
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
        if self._session is not None:
            await self._session.close()
        self._session = None
        self._unifi = None

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
            out.append(
                APSnapshot(
                    id=_require(raw, "_id", str, owner="device"),
                    name=raw.get("name", ""),
                    mac=_require(raw, "mac", str, owner="device"),
                )
            )
        return out

    async def get_ap_radio_stats(self, ap_id: str) -> list[RadioStats]:
        unifi = self._controller()
        await unifi.devices.update()
        for device in unifi.devices.values():
            raw = device.raw
            if raw.get("_id") != ap_id and raw.get("mac") != ap_id:
                continue
            stats = raw.get("radio_table_stats") or []
            return [
                RadioStats(
                    radio=_require(entry, "radio", str, owner="radio_table_stats"),
                    cu_total=_require(entry, "cu_total", int, owner="radio_table_stats"),
                    bssid=entry.get("bssid", ""),
                )
                for entry in stats
            ]
        return []

    async def force_reconnect_client(self, mac: str) -> None:
        unifi = self._controller()
        await unifi.clients.reconnect(mac)

    @staticmethod
    def _build_cu_lookup(unifi: aiounifi.Controller) -> dict[tuple[str, str], int]:
        lookup: dict[tuple[str, str], int] = {}
        for device in unifi.devices.values():
            raw = device.raw
            if raw.get("type") != "uap":
                continue
            ap_mac = raw.get("mac", "")
            for entry in raw.get("radio_table_stats") or []:
                radio = entry.get("radio")
                cu_total = entry.get("cu_total")
                if isinstance(radio, str) and isinstance(cu_total, int):
                    lookup[(ap_mac, radio)] = cu_total
        return lookup

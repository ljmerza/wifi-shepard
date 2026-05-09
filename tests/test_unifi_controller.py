from __future__ import annotations

import json
import ssl
from pathlib import Path

import pytest
from aioresponses import aioresponses

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "192.168.1.1"
PORT = 8443
BASE = f"https://{HOST}:{PORT}"
SITE_PREFIX = "/proxy/network/api/s/default"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _stub_login(m: aioresponses, base: str = BASE) -> None:
    m.get(f"{base}/", status=200, content_type="application/json", body="{}")
    m.post(
        f"{base}/api/auth/login",
        status=200,
        content_type="application/json",
        body=json.dumps({"meta": {"rc": "ok"}, "data": []}),
    )


@pytest.mark.asyncio
async def test_list_wireless_clients_maps_fixture_to_snapshots():
    from wifi_shepard.controllers import UniFiController

    clients_fixture = _load_fixture("unifi_clients.json")
    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        site="default",
        verify_ssl=False,
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/sta",
                status=200,
                content_type="application/json",
                body=json.dumps(clients_fixture),
            )
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
            )
            await controller.login()
            snapshots = await controller.list_wireless_clients()

        macs = {s.mac for s in snapshots}
        assert macs == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}, (
            "wired client must be filtered out"
        )

        wled = next(s for s in snapshots if s.mac == "aa:bb:cc:dd:ee:01")
        assert wled.signal == -78
        assert wled.tx_rate_kbps == 6000
        assert wled.tx_retries == 60
        assert wled.wifi_tx_attempts == 100
        assert wled.radio == "ng"
        assert wled.ap_id == "ff:ee:dd:cc:bb:aa"
        assert wled.ap_cu_total == 72, "ng radio cu_total should map from radio_table_stats"

        phone = next(s for s in snapshots if s.mac == "aa:bb:cc:dd:ee:02")
        assert phone.radio == "na"
        assert phone.ap_cu_total == 35, "na radio cu_total should map independently"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_list_wireless_clients_fail_closed_on_missing_required_field():
    from wifi_shepard.controllers import UniFiController, UniFiSchemaError

    clients_fixture = _load_fixture("unifi_clients.json")
    devices_fixture = _load_fixture("unifi_devices.json")
    del clients_fixture["data"][0]["signal"]

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/sta",
                status=200,
                content_type="application/json",
                body=json.dumps(clients_fixture),
            )
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
            )
            await controller.login()
            with pytest.raises(UniFiSchemaError, match="client.signal"):
                await controller.list_wireless_clients()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_force_reconnect_client_posts_kick_sta():
    from wifi_shepard.controllers import UniFiController

    captured: dict = {}

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.post(
                f"{BASE}{SITE_PREFIX}/cmd/stamgr",
                status=200,
                content_type="application/json",
                body=json.dumps({"meta": {"rc": "ok"}, "data": []}),
                callback=lambda url, **kw: captured.update(
                    {"url": str(url), "json": kw.get("json")}
                ),
            )
            await controller.login()
            await controller.force_reconnect_client("aa:bb:cc:dd:ee:01")

        assert captured["json"] == {"cmd": "kick-sta", "mac": "aa:bb:cc:dd:ee:01"}
        assert captured["url"].endswith("/cmd/stamgr")
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_list_aps_returns_only_uap_devices_with_mac_as_id():
    from wifi_shepard.controllers import UniFiController

    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
            )
            await controller.login()
            aps = await controller.list_aps()

        assert len(aps) == 1, "UDM (type=udm) must be filtered out"
        ap = aps[0]
        assert ap.mac == "ff:ee:dd:cc:bb:aa"
        assert ap.id == ap.mac, "id and mac must match (Protocol identifier convention)"
        assert ap.name == "Front Porch"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_get_ap_radio_stats_matches_on_mac_and_returns_per_radio_rows():
    from wifi_shepard.controllers import UniFiController

    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
            )
            await controller.login()
            stats = await controller.get_ap_radio_stats("ff:ee:dd:cc:bb:aa")

        by_radio = {s.radio: s for s in stats}
        assert set(by_radio) == {"ng", "na"}
        assert by_radio["ng"].cu_total == 72
        assert by_radio["na"].cu_total == 35
        assert by_radio["ng"].bssid == "ff:ee:dd:cc:bb:01"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_get_ap_radio_stats_unknown_mac_returns_empty():
    from wifi_shepard.controllers import UniFiController

    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
            )
            await controller.login()
            stats = await controller.get_ap_radio_stats("00:00:00:00:00:00")

        assert stats == []
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_cu_lookup_fails_closed_on_drifted_radio_table_entry():
    from wifi_shepard.controllers import UniFiController, UniFiSchemaError

    clients_fixture = _load_fixture("unifi_clients.json")
    devices_fixture = _load_fixture("unifi_devices.json")
    del devices_fixture["data"][0]["radio_table_stats"][0]["cu_total"]

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/sta",
                status=200,
                content_type="application/json",
                body=json.dumps(clients_fixture),
            )
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
            )
            await controller.login()
            with pytest.raises(UniFiSchemaError, match="radio_table_stats.cu_total"):
                await controller.list_wireless_clients()
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_verify_ssl_true_uses_default_ssl_context(monkeypatch):
    from wifi_shepard.controllers import UniFiController

    sentinel = ssl.create_default_context()
    calls: list[int] = []

    def fake_default() -> ssl.SSLContext:
        calls.append(1)
        return sentinel

    monkeypatch.setattr(ssl, "create_default_context", fake_default)

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        verify_ssl=True,
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            await controller.login()
        assert calls == [1], "verify_ssl=True must build a default SSLContext exactly once"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_verify_ssl_false_does_not_build_ssl_context(monkeypatch):
    from wifi_shepard.controllers import UniFiController

    calls: list[int] = []

    def fake_default() -> ssl.SSLContext:
        calls.append(1)
        return ssl.create_default_context()

    monkeypatch.setattr(ssl, "create_default_context", fake_default)

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        verify_ssl=False,
        port=PORT,
    )
    try:
        with aioresponses() as m:
            _stub_login(m)
            await controller.login()
        assert calls == [], "verify_ssl=False must not build an SSLContext"
    finally:
        await controller.close()


def test_verify_ssl_defaults_to_true_when_kwarg_omitted():
    from wifi_shepard.controllers import UniFiController

    controller = UniFiController(host=HOST, username="shepard", password="secret")
    assert controller.verify_ssl is True, (
        "secure-by-default: omitting verify_ssl must give True, not False"
    )


@pytest.mark.asyncio
async def test_send_btm_request_raises_not_implemented_in_mvp():
    from wifi_shepard.controllers import UniFiController

    controller = UniFiController(
        host=HOST, username="shepard", password="secret", verify_ssl=False, port=PORT
    )
    with pytest.raises(NotImplementedError, match="BTM"):
        await controller.send_btm_request("aa:bb:cc:dd:ee:01")


@pytest.mark.asyncio
async def test_port_kwarg_is_used_in_request_url():
    from wifi_shepard.controllers import UniFiController

    custom_port = 9999
    custom_base = f"https://{HOST}:{custom_port}"

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        port=custom_port,
    )
    try:
        with aioresponses() as m:
            _stub_login(m, base=custom_base)
            # If port=8443 (or anything other than 9999) were used, aioresponses
            # would raise ConnectionError on the unstubbed URL.
            await controller.login()
    finally:
        await controller.close()

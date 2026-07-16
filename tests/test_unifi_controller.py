from __future__ import annotations

import json
import logging
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
async def test_send_btm_request_posts_to_cmd_devmgr_with_bss_transition_payload():
    """ADR-0003 Phase 2: send_btm_request issues a raw POST against the UniFi
    cmd/devmgr endpoint with cmd=bss-transition. Phase 8 integration may
    invalidate the exact payload shape against a real controller — this test
    pins the contract so a future change can't silently revert."""
    from wifi_shepard.controllers import UniFiController

    controller = UniFiController(
        host=HOST,
        username="shepard",
        password="secret",
        site="default",
        verify_ssl=False,
        port=PORT,
    )
    target_url = f"{BASE}{SITE_PREFIX}/cmd/devmgr"
    try:
        with aioresponses() as m:
            _stub_login(m)
            m.post(
                target_url,
                status=200,
                content_type="application/json",
                body=json.dumps({"meta": {"rc": "ok"}, "data": []}),
            )
            await controller.login()
            await controller.send_btm_request("AA:BB:CC:DD:EE:01")

        # aioresponses stores requests keyed by (method, URL); look up the POST.
        post_url = next(
            (k for k in m.requests if k[0].lower() == "post" and "cmd/devmgr" in str(k[1])),
            None,
        )
        assert post_url is not None, (
            f"expected a POST to a /cmd/devmgr URL; got {[(k[0], str(k[1])) for k in m.requests]}"
        )
        calls = m.requests[post_url]
        assert len(calls) == 1, f"expected one BTM POST, got {len(calls)}"
        payload = calls[0].kwargs.get("json") or calls[0].kwargs.get("data") or {}
        assert payload.get("cmd") == "bss-transition", (
            f"BTM payload must carry cmd=bss-transition; got {payload!r}"
        )
        assert payload.get("mac") == "aa:bb:cc:dd:ee:01", (
            f"BTM payload must lowercase the MAC (matches deauth path); got {payload!r}"
        )
        assert "target_bssid" not in payload, (
            "no target_bssid was passed; the field must not be present in the payload"
        )
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_list_wireless_clients_maps_name_from_raw():
    from wifi_shepard.controllers import UniFiController

    clients_fixture = _load_fixture("unifi_clients.json")
    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(host=HOST, username="shepard", password="secret", port=PORT)
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

        by_mac = {s.mac: s for s in snapshots}
        assert by_mac["aa:bb:cc:dd:ee:01"].name == "wled-kitchen"
        assert by_mac["aa:bb:cc:dd:ee:02"].name == "phone"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_list_wireless_clients_maps_ip_fail_soft():
    """ADR-0011: ClientSnapshot.ip is fail-soft (raw.get("ip")) — present when the
    controller reports it, None when absent/blank. Never _require'd."""
    from wifi_shepard.controllers import UniFiController

    clients_fixture = _load_fixture("unifi_clients.json")
    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(host=HOST, username="shepard", password="secret", port=PORT)
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

        by_mac = {s.mac: s for s in snapshots}
        assert by_mac["aa:bb:cc:dd:ee:01"].ip == "192.168.1.50", "IP maps from raw.ip"
        assert by_mac["aa:bb:cc:dd:ee:02"].ip is None, "absent raw.ip is fail-soft None"
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_list_ap_stats_maps_cpu_mem_and_per_radio_channel():
    from wifi_shepard.controllers import UniFiController

    devices_fixture = _load_fixture("unifi_devices.json")

    controller = UniFiController(host=HOST, username="shepard", password="secret", port=PORT)
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
            aps = await controller.list_ap_stats()

        assert len(aps) == 1, "UDM (type=udm) must be filtered out"
        ap = aps[0]
        assert ap.mac == "ff:ee:dd:cc:bb:aa"
        assert ap.id == ap.mac, "id and mac must match (Protocol identifier convention)"
        assert ap.name == "Front Porch"
        assert ap.cpu_pct == pytest.approx(6.4)
        assert ap.mem_pct == pytest.approx(42.1)
        by_radio = {r.radio: r for r in ap.radios}
        assert set(by_radio) == {"ng", "na"}
        assert by_radio["ng"].channel == 6
        assert by_radio["ng"].cu_total == 72
        assert by_radio["na"].channel == 36
        assert by_radio["na"].cu_total == 35
    finally:
        await controller.close()


@pytest.mark.asyncio
async def test_list_ap_stats_missing_system_stats_yields_none_cpu_mem():
    """CPU/mem are fail-soft (None when absent), unlike the fail-closed cu_total."""
    from wifi_shepard.controllers import UniFiController

    devices_fixture = _load_fixture("unifi_devices.json")
    del devices_fixture["data"][0]["system-stats"]

    controller = UniFiController(host=HOST, username="shepard", password="secret", port=PORT)
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
            aps = await controller.list_ap_stats()

        assert aps[0].cpu_pct is None
        assert aps[0].mem_pct is None
        # Radio stats are independent of system-stats and must still map.
        assert {r.radio for r in aps[0].radios} == {"ng", "na"}
    finally:
        await controller.close()


def test_parse_pct_handles_plain_percent_and_blank():
    from wifi_shepard.controllers.unifi import _parse_pct

    assert _parse_pct("5.2") == pytest.approx(5.2)
    assert _parse_pct("5.2%") == pytest.approx(5.2)
    assert _parse_pct(" 12 ") == pytest.approx(12.0)
    assert _parse_pct("") is None
    assert _parse_pct(None) is None
    assert _parse_pct("n/a") is None


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


@pytest.mark.asyncio
async def test_statless_wireless_client_is_skipped_not_fatal(caplog):
    from wifi_shepard.controllers import UniFiController

    clients_fixture = _load_fixture("unifi_clients.json")
    # Mirror of a live observation: a sleeping iDevice on a private MAC stays in
    # /stat/sta with is_wired: false and none of its radio stat fields. One such
    # entry must not abort the whole scan cycle.
    clients_fixture["data"].append(
        {"mac": "26:c1:84:25:2d:1e", "hostname": "iPad", "is_wired": False, "ip": "192.168.1.130"}
    )
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
                repeat=True,
            )
            m.get(
                f"{BASE}{SITE_PREFIX}/stat/device",
                status=200,
                content_type="application/json",
                body=json.dumps(devices_fixture),
                repeat=True,
            )
            await controller.login()
            with caplog.at_level(logging.WARNING, logger="wifi_shepard.controllers.unifi"):
                first = await controller.list_wireless_clients()
                second = await controller.list_wireless_clients()
    finally:
        await controller.close()

    assert {s.mac for s in first} == {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}
    assert {s.mac for s in second} == {"aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:01"}
    warnings = [r for r in caplog.records if r.msg == "client_snapshot_incomplete"]
    assert len(warnings) == 1, "a persistently stat-less client warns once, not once per poll"
    assert warnings[0].client == "26:c1:84:25:2d:1e"
    assert "ap_mac" in warnings[0].missing


@pytest.mark.asyncio
async def test_wrong_typed_client_field_still_fails_closed():
    from wifi_shepard.controllers import UniFiController
    from wifi_shepard.controllers.unifi import UniFiSchemaError

    clients_fixture = _load_fixture("unifi_clients.json")
    for entry in clients_fixture["data"]:
        if entry.get("mac") == "aa:bb:cc:dd:ee:01":
            # Present but mistyped is schema drift, not a stat-less sleeper —
            # the fail-closed posture must be unchanged for this case.
            entry["ap_mac"] = 12345
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
            with pytest.raises(UniFiSchemaError):
                await controller.list_wireless_clients()
    finally:
        await controller.close()

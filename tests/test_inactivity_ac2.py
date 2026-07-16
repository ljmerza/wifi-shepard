"""ADR-0010 AC-2: the UniFi backend surfaces tx_bytes/rx_bytes when present and
yields None when absent or malformed (incl. bool), without raising — fail-soft,
unlike the fail-closed _require path for detection-critical fields.
"""

from __future__ import annotations

import json
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


def _stub_login(m: aioresponses) -> None:
    m.get(f"{BASE}/", status=200, content_type="application/json", body="{}")
    m.post(
        f"{BASE}/api/auth/login",
        status=200,
        content_type="application/json",
        body=json.dumps({"meta": {"rc": "ok"}, "data": []}),
    )


def test_unit_optional_int_helper():
    from wifi_shepard.controllers.unifi import _optional_int

    assert _optional_int({"tx_bytes": 12345}, "tx_bytes") == 12345
    assert _optional_int({"tx_bytes": 0}, "tx_bytes") == 0
    assert _optional_int({}, "tx_bytes") is None  # absent
    assert _optional_int({"tx_bytes": "12345"}, "tx_bytes") is None  # wrong type
    assert _optional_int({"tx_bytes": True}, "tx_bytes") is None  # bool is not accepted
    assert _optional_int({"tx_bytes": None}, "tx_bytes") is None


@pytest.mark.asyncio
async def test_list_wireless_clients_surfaces_byte_counters_fail_soft():
    from wifi_shepard.controllers import UniFiController

    clients_fixture = _load_fixture("unifi_clients.json")
    devices_fixture = _load_fixture("unifi_devices.json")
    # client[0] (wled): valid counters -> surfaced.
    clients_fixture["data"][0]["tx_bytes"] = 111111
    clients_fixture["data"][0]["rx_bytes"] = 222222
    # client[1] (phone): tx_bytes present but a bool (malformed) -> None; rx_bytes
    # absent -> None. Neither must raise.
    clients_fixture["data"][1]["tx_bytes"] = True

    controller = UniFiController(
        host=HOST, username="shepard", password="secret", verify_ssl=False, port=PORT
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

        by_mac = {s.mac: s for s in snapshots}
        wled = by_mac["aa:bb:cc:dd:ee:01"]
        assert wled.tx_bytes == 111111
        assert wled.rx_bytes == 222222

        phone = by_mac["aa:bb:cc:dd:ee:02"]
        assert phone.tx_bytes is None, "bool tx_bytes must be rejected fail-soft"
        assert phone.rx_bytes is None, "absent rx_bytes must be None, not a raise"
    finally:
        await controller.close()

"""ADR-0011 AC-7: allowlisted MACs are never flagged/kicked by the DNS path. With
dry_run off, the allowlist is the only thing standing between a sustained-thrash MAC
and a kick — so a spared allowlisted MAC alongside a kicked non-allowlisted one proves
the filter (and proves the detector really did flag both)."""

from __future__ import annotations

from tests.conftest import FakeController, FakeDnsSource, FakeHANotifier, make_client
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.dns_sources import DnsQuery
from wifi_shepard.dns_thrash import DnsThrashDetector
from wifi_shepard.scanner import Scanner

_ALLOWED = "aa:bb:cc:dd:ee:01"
_NORMAL = "aa:bb:cc:dd:ee:02"
_ALLOWED_IP = "10.0.0.5"
_NORMAL_IP = "10.0.0.6"
_DOMAIN = "mqtt-us-4.meross.com"
_T0 = 1_000_000.0


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _good_client(mac: str, ip: str):
    return make_client(mac=mac, ip=ip, signal=-50, tx_rate_kbps=500000, tx_retries=0, ap_cu_total=5)


async def test_allowlisted_mac_is_never_kicked_by_dns_path(temp_db_path):
    db = Database(temp_db_path)
    await db.connect()
    try:
        config = build_config(
            dry_run=False,
            window_samples=1,
            allowlist=[_ALLOWED],
            dns_thrash={
                "same_domain_queries_max": 20,
                "window_minutes": 10,
                "sustain_windows": 2,
            },
            dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
        )
        controller = FakeController(
            clients=[_good_client(_ALLOWED, _ALLOWED_IP), _good_client(_NORMAL, _NORMAL_IP)]
        )
        clock = Clock(_T0)
        src = FakeDnsSource()
        detector = DnsThrashDetector(config, src, now_fn=clock)
        scanner = Scanner(
            controller=controller, db=db, config=config, ha=FakeHANotifier(), dns_detector=detector
        )

        # Both MACs thrash identically and reach the sustain threshold.
        for step in (0, 600, 1200):
            clock.t = _T0 + step
            src.queries = [
                DnsQuery(ts=clock.t - i, client_ip=_ALLOWED_IP, domain=_DOMAIN) for i in range(25)
            ] + [DnsQuery(ts=clock.t - i, client_ip=_NORMAL_IP, domain=_DOMAIN) for i in range(25)]
            await scanner.run_once()

        assert _ALLOWED not in controller.force_reconnect_calls, "allowlisted MAC must be spared"
        assert _NORMAL in controller.force_reconnect_calls, (
            "the non-allowlisted MAC must be kicked — proving the detector flagged both "
            "and the allowlist is what spared the other"
        )
    finally:
        await db.close()

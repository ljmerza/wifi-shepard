"""ADR-0011 AC-10: a source fetch failure logs a warning and the scan cycle completes
normally (no crash, no flag); and a SIGHUP-style ``update_config`` picks up changed
dns_thrash thresholds in place (mirrors the test_sighup_ac7 propagation pattern)."""

from __future__ import annotations

import logging
from typing import Any

from tests.conftest import FakeController, FakeDnsSource, make_client
from wifi_shepard.config import build_config
from wifi_shepard.dns_sources import DnsQuery
from wifi_shepard.dns_thrash import DnsThrashDetector
from wifi_shepard.scanner import Scanner

_MAC = "aa:bb:cc:dd:ee:01"
_IP = "10.0.0.5"
_DOMAIN = "mqtt-us-4.meross.com"
_T0 = 1_000_000.0


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _NoopStore:
    async def insert_sample(self, client: Any) -> None: ...
    async def insert_ap_stats(self, ap: Any) -> None: ...
    async def insert_kick(self, **kwargs: Any) -> None: ...
    async def recent_kick_timestamps(self, mac: str, *, since: float) -> list[float]:
        return []


def _config(threshold: int):
    return build_config(
        dns_thrash={
            "same_domain_queries_max": threshold,
            "window_minutes": 10,
            "sustain_windows": 1,
        },
        dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
    )


async def test_source_failure_logs_warning_and_yields_no_flags(caplog):
    clock = Clock(_T0)
    detector = DnsThrashDetector(_config(20), FakeDnsSource(fail=True), now_fn=clock)

    with caplog.at_level(logging.WARNING, logger="wifi_shepard.dns_thrash"):
        flagged = await detector.observe([make_client(mac=_MAC, ip=_IP)])  # must not raise

    assert flagged == []
    assert any(r.message == "dns_source_unavailable" for r in caplog.records)


async def test_scan_cycle_completes_when_source_fails(temp_db_path):
    from wifi_shepard.db import Database

    db = Database(temp_db_path)
    await db.connect()
    try:
        config = _config(20)
        controller = FakeController(clients=[make_client(mac=_MAC, ip=_IP)])
        detector = DnsThrashDetector(config, FakeDnsSource(fail=True), now_fn=Clock(_T0))
        scanner = Scanner(controller=controller, db=db, config=config, dns_detector=detector)

        await scanner.run_once()  # must complete normally despite the DNS source failing

        assert controller.force_reconnect_calls == []
    finally:
        await db.close()


async def test_update_config_picks_up_new_threshold_in_place():
    clock = Clock(_T0)
    src = FakeDnsSource()
    detector = DnsThrashDetector(_config(20), src, now_fn=clock)
    clients = [make_client(mac=_MAC, ip=_IP)]

    def feed(t: float, n: int = 10):
        clock.t = t
        src.queries = [DnsQuery(ts=t - i, client_ip=_IP, domain=_DOMAIN) for i in range(n)]

    # threshold 20: 10 queries never crosses it.
    feed(_T0)
    assert await detector.observe(clients) == []
    feed(_T0 + 600)
    assert await detector.observe(clients) == []

    # SIGHUP retune to threshold 5 (sustain_windows=1 -> 600s).
    config_lo = _config(5)
    detector.update_config(config_lo)
    assert detector.config is config_lo

    feed(_T0 + 900)
    assert await detector.observe(clients) == [], "fresh streak just started under new threshold"
    feed(_T0 + 1500)
    assert await detector.observe(clients) == [_MAC], "new threshold now flags the same traffic"


async def test_scanner_update_config_propagates_to_detector():
    detector = DnsThrashDetector(_config(20), FakeDnsSource(), now_fn=Clock(_T0))
    config_hi = _config(20)
    scanner = Scanner(
        controller=FakeController(), db=_NoopStore(), config=config_hi, dns_detector=detector
    )

    config_lo = _config(5)
    scanner.update_config(config_lo)
    assert detector.config is config_lo, (
        "SIGHUP scanner.update_config must retune the detector's thresholds in place"
    )

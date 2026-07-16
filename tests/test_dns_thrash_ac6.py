"""ADR-0011 AC-6: a DNS-flagged MAC routes through Actor.handle — the *same* path a
scorer flag takes. Under dry_run the would-kick path fires and no controller call is
made; with dry_run off the real kick fires (deauth + kick_event + notify), proving the
backoff/rate-limit/notify machinery is exercised, not bypassed."""

from __future__ import annotations

import logging

import aiosqlite

from tests.conftest import FakeController, FakeHANotifier, make_client
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
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


def _config(*, dry_run: bool):
    # Good-signal client values so the scorer never flags — isolating the DNS path.
    return build_config(
        dry_run=dry_run,
        window_samples=1,
        signal_dbm_max=-70,
        dns_thrash={"same_domain_queries_max": 20, "window_minutes": 10, "sustain_windows": 2},
        dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
    )


def _good_client():
    # Strong signal + fast + no retries: scorer.is_bad_state is False for this client.
    return make_client(
        mac=_MAC, ip=_IP, signal=-50, tx_rate_kbps=500000, tx_retries=0, ap_cu_total=5
    )


async def _drive_to_sustain(scanner, clock, src):
    for step in (0, 600, 1200):
        clock.t = _T0 + step
        src.queries = [DnsQuery(ts=clock.t - i, client_ip=_IP, domain=_DOMAIN) for i in range(25)]
        await scanner.run_once()


async def test_dry_run_fires_would_kick_and_makes_no_controller_call(temp_db_path, caplog):
    from tests.conftest import FakeDnsSource

    db = Database(temp_db_path)
    await db.connect()
    try:
        config = _config(dry_run=True)
        controller = FakeController(clients=[_good_client()])
        clock = Clock(_T0)
        src = FakeDnsSource()
        detector = DnsThrashDetector(config, src, now_fn=clock)
        scanner = Scanner(
            controller=controller, db=db, config=config, ha=FakeHANotifier(), dns_detector=detector
        )

        with caplog.at_level(logging.INFO, logger="wifi_shepard.actor"):
            await _drive_to_sustain(scanner, clock, src)

        would_kicks = [r for r in caplog.records if r.message == "would_kick"]
        assert would_kicks, "the DNS-thrash flag must reach Actor.handle's would_kick path"
        assert any(getattr(r, "thresholds", {}).get("trigger") == "dns_thrash" for r in would_kicks)
        assert controller.force_reconnect_calls == [], "dry_run must make no controller call"
        assert controller.btm_calls == []
    finally:
        await db.close()


async def test_real_kick_goes_through_the_actor(temp_db_path):
    from tests.conftest import FakeDnsSource

    db = Database(temp_db_path)
    await db.connect()
    try:
        config = _config(dry_run=False)
        controller = FakeController(clients=[_good_client()])
        ha = FakeHANotifier()
        clock = Clock(_T0)
        src = FakeDnsSource()
        detector = DnsThrashDetector(config, src, now_fn=clock)
        scanner = Scanner(controller=controller, db=db, config=config, ha=ha, dns_detector=detector)

        await _drive_to_sustain(scanner, clock, src)

        # The DNS path went through the real Actor: a deauth wire call, a kick_event
        # row, and an HA notification — none of which a bypass would produce.
        assert _MAC in controller.force_reconnect_calls
        assert any(p["severity"] == "kick" for p in ha.posts)
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM kick_events WHERE mac = ? AND dry_run = 0 "
                "AND mechanism = 'deauth'",
                (_MAC,),
            )
            (count,) = await cur.fetchone()
        assert count >= 1, "a real DNS-triggered kick must be recorded in kick_events"
    finally:
        await db.close()

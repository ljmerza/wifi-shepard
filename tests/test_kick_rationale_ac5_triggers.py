"""ADR-0015 AC-5: inactivity and DNS-thrash kicks carry their own evidence.

An inactivity kick records window_bytes / min_bytes_per_window / window_samples;
a DNS-thrash kick records the offending domain / query_count / resolved threshold
(enriched by the scanner from the detector's standings). Both ride the same
envelope with no new columns.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.actor import Actor
from wifi_shepard.config import build_config
from wifi_shepard.db import Database


@pytest.mark.asyncio
async def test_ac_5_inactivity_kick_records_traffic_evidence(temp_db_path):
    mac = "aa:bb:cc:dd:ee:10"
    config = build_config(dry_run=False)
    db = Database(temp_db_path)
    await db.connect()
    try:
        actor = Actor(config=config, controller=FakeController(), db=db)
        ctx = {
            "trigger": "inactivity",
            "window_bytes": 123,
            "min_bytes_per_window": 1000,
            "window_samples": 3,
        }
        await actor.handle(make_client(mac=mac), ctx)
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT rationale FROM kick_events WHERE mac = ?", (mac,))
            (raw,) = await cur.fetchone()
    finally:
        await db.close()

    r = json.loads(raw)
    assert r["trigger"] == "inactivity"
    assert r["window_bytes"] == 123
    assert r["min_bytes_per_window"] == 1000
    assert r["window_samples"] == 3


class _FakeFlaggingDetector:
    """Flags one MAC and exposes near-threshold standings, like the real detector
    after an over-threshold sustained poll — without the two-cycle sustain setup."""

    def __init__(self, mac, standings):
        self._mac = mac
        self._standings = standings
        self.source = None

    async def observe(self, clients):
        return [self._mac]

    def standings(self):
        return list(self._standings)

    def update_config(self, config):  # pragma: no cover - SIGHUP retune, unused here
        pass


@pytest.mark.asyncio
async def test_ac_5_dns_kick_records_domain_count_threshold(temp_db_path):
    from wifi_shepard.scanner import Scanner

    mac = "aa:bb:cc:dd:ee:11"
    ip = "10.0.0.9"
    config = build_config(
        dry_run=False,
        dns_thrash={"same_domain_queries_max": 20, "window_minutes": 60, "sustain_windows": 2},
        dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
    )
    standings = [
        {"mac": mac, "domain": "mqtt.example.com", "count": 42, "threshold": 20, "over_since": 1.0},
        # a quieter domain for the same MAC — the enrichment must pick the loudest.
        {"mac": mac, "domain": "ntp.example.com", "count": 5, "threshold": 20, "over_since": None},
    ]
    detector = _FakeFlaggingDetector(mac, standings)
    client = make_client(mac=mac, ip=ip)

    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(
            controller=FakeController(clients=[client]),
            db=db,
            config=config,
            dns_detector=detector,
        )
        await scanner._run_dns_thrash([client])
        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT rationale FROM kick_events WHERE mac = ? AND trigger = 'dns_thrash'",
                (mac,),
            )
            row = await cur.fetchone()
    finally:
        await db.close()

    assert row is not None, "AC-5: a flagged DNS MAC must produce a dns_thrash kick row"
    r = json.loads(row[0])
    assert r["trigger"] == "dns_thrash"
    assert r["domain"] == "mqtt.example.com", (
        f"AC-5: the scanner must enrich with the loudest domain; got {r.get('domain')!r}"
    )
    assert r["query_count"] == 42
    assert r["threshold"] == 20

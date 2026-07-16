"""ADR-0012 AC-3: near-threshold standings snapshot + contender persistence.

After observe(), the detector's standings() reflects the live per-(MAC, domain)
count/threshold/over_since. The scanner persists only *contenders* — count >=
ceil(0.5 * threshold), top-N capped — so a below-band domain produces no row.
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, FakeDnsSource, make_client
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.dns_sources import DnsQuery
from wifi_shepard.dns_thrash import DnsThrashDetector
from wifi_shepard.scanner import Scanner

_MAC = "aa:bb:cc:dd:ee:01"
_IP = "10.0.0.5"
_NOW = 1_000_000.0
_CONTENDER = "mqtt-us-4.meross.com"  # 15 queries, threshold 20 -> band is 10, contender
_QUIET = "ntp.example.com"  # 5 queries, below the 10 band


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _config():
    return build_config(
        dns_thrash={"same_domain_queries_max": 20, "window_minutes": 60, "sustain_windows": 2},
        dns_sources=[{"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}],
    )


@pytest.mark.asyncio
async def test_ac_3_standings_and_contender_persistence(temp_db_path):
    db = Database(temp_db_path)
    await db.connect()
    try:
        src = FakeDnsSource()
        src.queries = [DnsQuery(ts=_NOW - i, client_ip=_IP, domain=_CONTENDER) for i in range(15)]
        src.queries += [DnsQuery(ts=_NOW - i, client_ip=_IP, domain=_QUIET) for i in range(5)]
        detector = DnsThrashDetector(_config(), src, now_fn=_Clock(_NOW))
        client = make_client(mac=_MAC, ip=_IP)
        scanner = Scanner(
            controller=FakeController(clients=[client]),
            db=db,
            config=_config(),
            dns_detector=detector,
        )

        await scanner._run_dns_thrash([client])

        standings = {(s["domain"]): s for s in detector.standings()}

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT mac, domain, query_count, threshold FROM dns_thrash_observations"
            )
            persisted = await cur.fetchall()
    finally:
        await db.close()

    # standings() reflects live counts for BOTH domains
    assert _CONTENDER in standings and standings[_CONTENDER]["count"] == 15, (
        f"AC-3: standings must reflect the contender's live count of 15; got {standings}"
    )
    assert standings[_CONTENDER]["threshold"] == 20, "AC-3: standings must carry the threshold"
    assert _QUIET in standings and standings[_QUIET]["count"] == 5, (
        "AC-3: standings must include the below-band domain with its real count"
    )

    # Only the contender is persisted
    persisted_domains = {row[1] for row in persisted}
    assert _CONTENDER in persisted_domains, "AC-3: the contender (15 >= band 10) must be persisted"
    assert _QUIET not in persisted_domains, (
        f"AC-3: the below-band domain (5 < band 10) must NOT be persisted; got {persisted_domains}"
    )
    contender_row = next(r for r in persisted if r[1] == _CONTENDER)
    assert contender_row[0] == _MAC and contender_row[2] == 15 and contender_row[3] == 20


@pytest.mark.asyncio
async def test_ac_3_contenders_are_top_n_capped(temp_db_path):
    """AC-3's flood mitigation: a poll with more than the cap of contenders must
    persist only the top-N by count (removing the slice would fail this)."""
    from wifi_shepard.scanner import _DNS_OBSERVATION_TOP_N

    db = Database(temp_db_path)
    await db.connect()
    try:
        src = FakeDnsSource()
        # 25 distinct domains, each above the band (10) — counts 10..34.
        src.queries = []
        for i in range(25):
            count = 10 + i
            src.queries += [
                DnsQuery(ts=_NOW - j, client_ip=_IP, domain=f"d{i}.example.com")
                for j in range(count)
            ]
        detector = DnsThrashDetector(_config(), src, now_fn=_Clock(_NOW))
        client = make_client(mac=_MAC, ip=_IP)
        scanner = Scanner(
            controller=FakeController(clients=[client]),
            db=db,
            config=_config(),
            dns_detector=detector,
        )

        await scanner._run_dns_thrash([client])

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute("SELECT query_count FROM dns_thrash_observations")
            counts = sorted(row[0] for row in await cur.fetchall())
    finally:
        await db.close()

    assert len(counts) == _DNS_OBSERVATION_TOP_N, (
        f"25 contenders must be capped at {_DNS_OBSERVATION_TOP_N}; got {len(counts)}"
    )
    # Top-N by count: the 20 highest of 10..34 are 15..34, so the smallest kept is 15.
    assert min(counts) == 15, f"the cap must keep the highest-count contenders; got min {counts[0]}"

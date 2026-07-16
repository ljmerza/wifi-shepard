"""ADR-0012 AC-2: per-poll Pi-hole source health is persisted.

Each scan cycle with DNS enabled writes one dns_source_samples row per instance —
a healthy poll records ok=1 with the fetched query_count; a failing instance
records ok=0 with its error, and the healthy instance's row is still written the
same cycle (one dead Pi-hole must not suppress the other's heartbeat).
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.scanner import Scanner


class _StubSource:
    """Exposes the per-instance last-poll status the scanner persists."""

    def __init__(self, status):
        self._status = status

    def last_poll_status(self):
        return list(self._status)


class _StubDetector:
    """observe() flags nothing (the quiet case) but the source reports a poll —
    the whole point of the heartbeat is to prove liveness when nothing thrashes."""

    def __init__(self, source):
        self.source = source

    async def observe(self, clients):
        return []

    def standings(self):
        return []


@pytest.mark.asyncio
async def test_ac_2_source_health_row_per_instance(temp_db_path):
    db = Database(temp_db_path)
    await db.connect()
    try:
        source = _StubSource(
            [
                {"name": "gym", "ok": True, "query_count": 1200, "error": None},
                {"name": "bonus", "ok": False, "query_count": 0, "error": "HTTP 500"},
            ]
        )
        scanner = Scanner(
            controller=FakeController(),
            db=db,
            config=build_config(
                dns_sources=[
                    {"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}
                ],
                dns_thrash={"same_domain_queries_max": 20},
            ),
            dns_detector=_StubDetector(source),
        )
        await scanner._run_dns_thrash([make_client(mac="aa:bb:cc:dd:ee:01", ip="10.0.0.5")])

        async with aiosqlite.connect(temp_db_path) as conn:
            cur = await conn.execute(
                "SELECT source_name, ok, query_count, error FROM dns_source_samples "
                "ORDER BY source_name"
            )
            rows = await cur.fetchall()
    finally:
        await db.close()

    by_name = {r[0]: r for r in rows}
    assert set(by_name) == {"bonus", "gym"}, (
        f"AC-2: one row per instance must be written each cycle; got {by_name}"
    )
    assert by_name["gym"][1] == 1 and by_name["gym"][2] == 1200, (
        f"AC-2: healthy poll must record ok=1 + query_count; got {by_name['gym']}"
    )
    assert by_name["bonus"][1] == 0 and by_name["bonus"][3] == "HTTP 500", (
        f"AC-2: failing instance must record ok=0 + error, not suppress others; "
        f"got {by_name['bonus']}"
    )

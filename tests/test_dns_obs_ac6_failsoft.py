"""ADR-0012 AC-6: observability persistence is fail-soft.

A DB write error while persisting DNS observability must be logged and swallowed
— the scan cycle completes, the detector's flags still route through Actor.handle
(the RF/remediation path is untouched), and nothing propagates out of the loop.
"""

from __future__ import annotations

import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.scanner import Scanner

_MAC = "aa:bb:cc:dd:ee:07"


class _FailingPersistDB(Database):
    """Real DB, except persisting a source-health row blows up."""

    attempted: bool = False

    async def insert_dns_source_sample(self, **kwargs):
        self.attempted = True
        raise RuntimeError("disk full")


class _StubSource:
    def last_poll_status(self):
        return [{"name": "gym", "ok": True, "query_count": 10, "error": None}]


class _FlaggingDetector:
    def __init__(self):
        self.source = _StubSource()

    async def observe(self, clients):
        return [_MAC]

    def standings(self):
        return []


@pytest.mark.asyncio
async def test_ac_6_persist_failure_is_swallowed_and_kick_still_fires(temp_db_path):
    db = _FailingPersistDB(temp_db_path)
    await db.connect()
    controller = FakeController(clients=[make_client(mac=_MAC)])
    try:
        scanner = Scanner(
            controller=controller,
            db=db,
            config=build_config(
                dry_run=False,
                dns_sources=[
                    {"type": "pihole", "password": "pw", "instances": [{"url": "http://x"}]}
                ],
                dns_thrash={"same_domain_queries_max": 20},
            ),
            dns_detector=_FlaggingDetector(),
        )

        # Must NOT raise, even though source-health persistence throws.
        await scanner._run_dns_thrash([make_client(mac=_MAC)])
    finally:
        await db.close()

    assert db.attempted, (
        "AC-6: the scan cycle must actually attempt to persist source health "
        "(otherwise the fail-soft guarantee is untested)"
    )
    assert _MAC in controller.force_reconnect_calls, (
        "AC-6: a persistence failure must not stop the flagged MAC from being kicked "
        f"(RF path untouched); force_reconnect_calls={controller.force_reconnect_calls}"
    )

"""ADR-0012 AC-5: DNS off => no observability writes, trigger defaults to 'rf'.

With no DNS detector wired (feature off), a normal scan cycle writes zero
dns_source_samples / dns_thrash_observations rows, and a kick recorded without an
explicit trigger defaults to 'rf' — the feature is opt-in with no behavior change.
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.conftest import FakeController, make_client
from wifi_shepard.config import build_config
from wifi_shepard.db import Database
from wifi_shepard.scanner import Scanner


@pytest.mark.asyncio
async def test_ac_5_feature_off_writes_no_dns_rows(temp_db_path):
    db = Database(temp_db_path)
    await db.connect()
    try:
        scanner = Scanner(
            controller=FakeController(clients=[make_client(mac="aa:bb:cc:dd:ee:09")]),
            db=db,
            config=build_config(),  # no dns_sources
            dns_detector=None,  # feature off
        )
        await scanner.run_once()

        async with aiosqlite.connect(temp_db_path) as conn:
            (src_count,) = await (
                await conn.execute("SELECT COUNT(*) FROM dns_source_samples")
            ).fetchone()
            (obs_count,) = await (
                await conn.execute("SELECT COUNT(*) FROM dns_thrash_observations")
            ).fetchone()

        assert src_count == 0, f"AC-5: DNS off must write no source-health rows; got {src_count}"
        assert obs_count == 0, f"AC-5: DNS off must write no observation rows; got {obs_count}"

        # A kick recorded without a trigger defaults to 'rf'.
        await db.insert_kick(mac="aa:bb:cc:dd:ee:09", dry_run=False)
        async with aiosqlite.connect(temp_db_path) as conn:
            (trig,) = await (
                await conn.execute("SELECT trigger FROM kick_events LIMIT 1")
            ).fetchone()
        assert trig == "rf", f"AC-5: kick trigger must default to 'rf'; got {trig!r}"
    finally:
        await db.close()
